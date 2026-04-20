"""
imports/staff_list.py
Parses the UK010117_Staff_List.xlsx and upserts into the database.
Called by the scheduler at midnight and can be run manually.

Fields imported:
    Horizon Person Number, Name, Technical Grade, Staff Team, Discipline,
    Availability, Start Date, End Date, and the monthly availability columns.

Fields deliberately excluded (financial):
    Burdened Cost, Raw Cost, Utilisation Target
"""

import sqlite3
import json
import openpyxl
from datetime import datetime, timezone, date
import os
import sys


import sys, os; sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))); from database import get_connection

# Excel serial date origin
EXCEL_EPOCH = date(1899, 12, 30)

# Columns in the staff list that are NOT monthly availability fractions
NON_PERIOD_COLS = {
    "horizon person number", "name", "technical grade", "staff team",
    "discipline", "availability", "start date", "end date",
    "burdened cost", "raw cost", "utilisation target"
}


def excel_serial_to_date(serial):
    """Convert Excel serial date integer to Python date."""
    if not serial or not isinstance(serial, (int, float)):
        return None
    try:
        return (EXCEL_EPOCH + __import__("datetime").timedelta(days=int(serial))).isoformat()
    except Exception:
        return None


def is_period_column(header):
    """
    Returns True if a column header is a date/datetime representing a month.
    openpyxl returns these as datetime objects when the file is opened normally.
    Falls back to checking for Excel date serial integers.
    """
    if header is None:
        return False
    # openpyxl returns datetime objects for date-formatted cells
    if hasattr(header, "year"):
        return True
    try:
        val = int(float(str(header)))
        # Excel date serials for 2024-2030 range roughly 45000-48000
        return 44000 < val < 50000
    except (ValueError, TypeError):
        return False


def header_to_iso(header):
    """Convert a period column header (datetime or serial int) to ISO date string."""
    if hasattr(header, "year"):
        return header.date().isoformat()
    try:
        val = int(float(str(header)))
        return (EXCEL_EPOCH + __import__("datetime").timedelta(days=val)).isoformat()
    except Exception:
        return None


def run(file_path: str, office: str = "London - Chancery Lane") -> dict:
    """
    Parse the staff list file and upsert into the database.
    Returns a summary dict for the import log.
    """
    started_at = datetime.now(timezone.utc).isoformat()
    errors = []
    rows_processed = 0
    rows_inserted = 0
    rows_updated = 0

    try:
        wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
        ws = wb["Staff Details"]
    except Exception as e:
        return _log_result(file_path, started_at, 0, 0, 0, [str(e)])

    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return _log_result(file_path, started_at, 0, 0, 0, ["Sheet is empty"])

    # -- Parse header row ------------------------------------------------
    header_row = rows[0]

    def clean_header(h):
        if h is None:
            return ""
        s = str(h).strip()
        # openpyxl sometimes includes a leading apostrophe from Excel
        # text-prefixed cells
        return s.lstrip("'").strip()

    headers = [clean_header(h) for h in header_row]

    # Identify which column indices are monthly period columns.
    # IMPORTANT: use header_row (raw) for period detection, headers (cleaned)
    # for text column mapping — datetime objects must not be stringified first.
    period_cols = {}  # col_index -> ISO date string
    col_map = {}      # normalised header name -> col_index

    for i, raw_h in enumerate(header_row):
        if is_period_column(raw_h):
            iso = header_to_iso(raw_h)
            if iso:
                period_cols[i] = iso
        else:
            col_map[headers[i].lower()] = i

    conn = get_connection()
    c = conn.cursor()

    for row in rows[1:]:
        if not any(row):
            continue

        rows_processed += 1

        def get(field):
            idx = col_map.get(field.lower())
            if idx is None:
                return None
            val = row[idx]
            if val in ("", None):
                return None
            # Strip leading apostrophe from text-prefixed cells
            if isinstance(val, str):
                val = val.lstrip("'").strip()
            return val if val != "" else None

        horizon_id = get("horizon person number")
        name = get("name")

        # Skip the generic grade placeholder rows at the bottom
        # (those with no Horizon Person Number and a name like "UK Director")
        if not horizon_id:
            continue

        horizon_id = str(horizon_id).strip()
        if not horizon_id or not name:
            errors.append(f"Row {rows_processed}: missing ID or name, skipped")
            continue

        technical_grade  = get("technical grade") or ""
        staff_team       = get("staff team") or ""
        discipline       = get("discipline") or ""
        availability     = get("availability") or 1.0
        start_date_raw   = get("start date")
        end_date_raw     = get("end date")

        start_date = excel_serial_to_date(start_date_raw)
        end_date   = excel_serial_to_date(end_date_raw)

        now = datetime.now(timezone.utc).isoformat()

        # Upsert staff row
        existing = c.execute(
            "SELECT horizon_person_number FROM staff WHERE horizon_person_number = ?",
            (horizon_id,)
        ).fetchone()

        if existing:
            c.execute("""
                UPDATE staff SET
                    name = ?, technical_grade = ?, staff_team = ?,
                    discipline = ?, availability = ?, start_date = ?,
                    end_date = ?, office = ?, last_imported = ?
                WHERE horizon_person_number = ?
            """, (name, technical_grade, staff_team, discipline,
                  availability, start_date, end_date, office, now, horizon_id))
            rows_updated += 1
        else:
            c.execute("""
                INSERT INTO staff
                    (horizon_person_number, name, technical_grade, staff_team,
                     discipline, availability, start_date, end_date, office, last_imported)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (horizon_id, name, technical_grade, staff_team, discipline,
                  availability, start_date, end_date, office, now))
            rows_inserted += 1

        # Upsert monthly availability fractions
        for col_idx, period_iso in period_cols.items():
            fraction = row[col_idx]
            if fraction is None:
                fraction = 0.0
            try:
                fraction = float(fraction)
            except (ValueError, TypeError):
                fraction = 0.0

            c.execute("""
                INSERT INTO staff_availability
                    (horizon_person_number, period_start, availability_fraction)
                VALUES (?, ?, ?)
                ON CONFLICT(horizon_person_number, period_start)
                DO UPDATE SET availability_fraction = excluded.availability_fraction
            """, (horizon_id, period_iso, fraction))

    conn.commit()
    conn.close()

    return _log_result(
        file_path, started_at, rows_processed,
        rows_inserted, rows_updated, errors
    )


def _log_result(file_path, started_at, processed, inserted, updated, errors):
    completed_at = datetime.now(timezone.utc).isoformat()
    result = {
        "import_type":    "staff_list",
        "filename":       os.path.basename(file_path),
        "started_at":     started_at,
        "completed_at":   completed_at,
        "rows_processed": processed,
        "rows_inserted":  inserted,
        "rows_updated":   updated,
        "errors":         errors
    }
    _write_log(result)
    return result


def _write_log(result):
    try:
        conn = get_connection()
        conn.execute("""
            INSERT INTO import_log
                (import_type, filename, started_at, completed_at,
                 rows_processed, rows_inserted, rows_updated, errors)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            result["import_type"],
            result["filename"],
            result["started_at"],
            result["completed_at"],
            result["rows_processed"],
            result["rows_inserted"],
            result["rows_updated"],
            json.dumps(result["errors"])
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Warning: could not write import log: {e}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python staff_list.py <path_to_staff_list.xlsx>")
        sys.exit(1)
    result = run(sys.argv[1])
    print(f"Staff list import complete:")
    print(f"  Processed : {result['rows_processed']}")
    print(f"  Inserted  : {result['rows_inserted']}")
    print(f"  Updated   : {result['rows_updated']}")
    if result["errors"]:
        print(f"  Errors    : {len(result['errors'])}")
        for e in result["errors"]:
            print(f"    - {e}")