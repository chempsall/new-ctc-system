"""
services/jobs.py
Scheduled work: the nightly import pipeline and the pending-RTC
re-link pass. Callable from the scheduler and from admin routes.
"""

import logging
from datetime import datetime, timezone, date
from pathlib import Path

from dateutil.relativedelta import relativedelta

import config
import database
import summary as summary_module
from imports import staff_list as staff_import
from imports import par_import
from services.projects import is_placeholder
from services.special_rtcs import run_special_rtc_maintenance

logger = logging.getLogger("resource_forecast.jobs")


# ── Grade → generic mapping ───────────────────────────────────────────────────

GRADE_TO_GENERIC = {
    "P7": "GENERIC-UK-DIRECTOR",
    "P6": "GENERIC-UK-TECHNICAL-DIRECTOR",
    "P5": "GENERIC-UK-ASSOCIATE-DIRECTOR",
    "P4": "GENERIC-UK-PRINCIPAL-ENGINEER",
    "P3": "GENERIC-UK-SENIOR-ENGINEER",
    "P2": "GENERIC-UK-ENGINEER",
    "P1": "GENERIC-UK-GRADUATE-ENGINEER",
    "P0": "GENERIC-UK-UNDERGRADUATE-ENGINEER",
    "T4": "GENERIC-UK-SENIOR-TECHNICIAN",
    "T3": "GENERIC-UK-EXPERIENCED-TECHNICIAN",
    "T2": "GENERIC-UK-INTERMEDIATE-TECHNICIAN",
    "T1": "GENERIC-UK-ASSISTANT-TECHNICIAN",
    "T0": "GENERIC-UK-TECHNICIAN-IN-TRAINING",
}

SPECIAL_PROJECT_NUMBERS_LEAVER = {"ID-06", "ID-04", "IDUK-01"}


def _grade_code(job_title: str) -> str | None:
    """Extract grade code (e.g. 'P3') from a job title string."""
    import re
    m = re.match(r"^([PT]\d)", job_title or "")
    return m.group(1) if m else None


def _get_or_create_suffixed_generic(c, base_generic_id: str, rtc_id: int, now: str) -> str:
    """
    Returns the horizon_person_number of a generic slot on this RTC.
    Uses the base generic if available, otherwise creates a suffixed copy.
    """
    # Check if base generic already has a row on this RTC
    existing = c.execute("""
        SELECT horizon_person_number FROM allocations
        WHERE rtc_id = ? AND horizon_person_number = ?
        LIMIT 1
    """, (rtc_id, base_generic_id)).fetchone()
    if existing:
        return base_generic_id

    # Check for any suffixed copy of this generic on this RTC
    suffixed = c.execute("""
        SELECT horizon_person_number FROM allocations
        WHERE rtc_id = ? AND horizon_person_number LIKE ?
        LIMIT 1
    """, (rtc_id, f"{base_generic_id}_%")).fetchone()
    if suffixed:
        return suffixed["horizon_person_number"]

    # Create a new suffixed generic staff row
    suffix     = now.replace(":", "").replace("-", "").replace(".", "")[:20]
    new_pid    = f"{base_generic_id}_{suffix}"
    # Copy the base generic's staff record
    base_row = c.execute("""
        SELECT name, job_title, job_family, job_function
        FROM staff WHERE horizon_person_number = ?
    """, (base_generic_id,)).fetchone()
    if base_row:
        c.execute("""
            INSERT OR IGNORE INTO staff
                (horizon_person_number, name, job_title, job_family,
                 job_function, department, availability, import_source)
            VALUES (?, ?, ?, ?, ?, '_GENERIC', 1.0, 'seeded')
        """, (new_pid, base_row["name"], base_row["job_title"],
              base_row["job_family"], base_row["job_function"]))
    return new_pid


def process_leavers():
    """
    For each staff member whose end_date has passed:
    - On special RTCs: zero out their future allocation rows (no replacement)
    - On regular RTCs: transfer future days to the grade-equivalent generic,
      then zero out the leaver's future rows
    Leaves past allocation rows completely untouched.
    """
    import database
    from datetime import datetime, timezone, date as _date

    conn   = database.get_connection()
    c      = conn.cursor()
    now    = datetime.now(timezone.utc).isoformat()
    today  = _date.today().replace(day=1).isoformat()

    logger = logging.getLogger("resource_forecast")

    # Find leavers with future allocations
    leavers = c.execute("""
        SELECT DISTINCT s.horizon_person_number, s.job_title, s.name
        FROM staff s
        JOIN allocations a ON a.horizon_person_number = s.horizon_person_number
        WHERE s.end_date IS NOT NULL
        AND s.end_date < ?
        AND a.period_start >= ?
        AND a.days > 0
    """, (today, today)).fetchall()

    if not leavers:
        conn.close()
        return

    transferred = 0
    zeroed      = 0

    for leaver in leavers:
        pid        = leaver["horizon_person_number"]
        job_title  = leaver["job_title"] or ""
        grade      = _grade_code(job_title)
        base_gid   = GRADE_TO_GENERIC.get(grade) if grade else None

        # Get all RTCs this leaver has future allocations on
        rtcs = c.execute("""
            SELECT DISTINCT a.rtc_id, p.project_number
            FROM allocations a
            JOIN rtcs r ON r.rtc_id = a.rtc_id
            JOIN projects p ON p.project_id = r.project_id
            WHERE a.horizon_person_number = ?
            AND a.period_start >= ?
            AND a.days > 0
        """, (pid, today)).fetchall()

        for rtc_row in rtcs:
            rtc_id     = rtc_row["rtc_id"]
            is_special = rtc_row["project_number"] in SPECIAL_PROJECT_NUMBERS_LEAVER

            if is_special:
                # Just zero out future rows — no replacement
                c.execute("""
                    UPDATE allocations SET days = 0, last_updated = ?
                    WHERE horizon_person_number = ? AND rtc_id = ?
                    AND period_start >= ?
                """, (now, pid, rtc_id, today))
                zeroed += c.rowcount
            elif base_gid:
                # Transfer days to generic, then zero leaver rows
                gid = _get_or_create_suffixed_generic(c, base_gid, rtc_id, now)

                # Get the leaver's future periods and days
                future_rows = c.execute("""
                    SELECT period_start, days FROM allocations
                    WHERE horizon_person_number = ? AND rtc_id = ?
                    AND period_start >= ? AND days > 0
                """, (pid, rtc_id, today)).fetchall()

                for row in future_rows:
                    period = row["period_start"]
                    days   = row["days"]
                    # Add to generic (INSERT OR IGNORE then UPDATE)
                    c.execute("""
                        INSERT OR IGNORE INTO allocations
                            (horizon_person_number, rtc_id, period_start, days, last_updated)
                        VALUES (?, ?, ?, 0, ?)
                    """, (gid, rtc_id, period, now))
                    c.execute("""
                        UPDATE allocations
                        SET days = days + ?, last_updated = ?
                        WHERE horizon_person_number = ? AND rtc_id = ? AND period_start = ?
                    """, (days, now, gid, rtc_id, period))
                    transferred += 1

                # Zero out leaver's future rows
                c.execute("""
                    UPDATE allocations SET days = 0, last_updated = ?
                    WHERE horizon_person_number = ? AND rtc_id = ?
                    AND period_start >= ?
                """, (now, pid, rtc_id, today))
                zeroed += c.rowcount
            else:
                # Unknown grade — just zero out, log a warning
                logger.warning(f"Leaver {leaver['name']} ({pid}) has unknown grade "
                               f"{grade!r} — zeroing future rows without replacement")
                c.execute("""
                    UPDATE allocations SET days = 0, last_updated = ?
                    WHERE horizon_person_number = ? AND rtc_id = ?
                    AND period_start >= ?
                """, (now, pid, rtc_id, today))
                zeroed += c.rowcount

    conn.commit()
    conn.close()
    logger.info(f"Leavers: {len(leavers)} processed, "
                f"{transferred} periods transferred to generics, "
                f"{zeroed} rows zeroed")

def relink_pending_rtcs(conn=None):
    """
    Checks all Pending/Placeholder RTCs against current PAR data.
    If a real project+task match is found, links the RTC automatically.
    Returns a count of RTCs linked.
    """
    close_after = conn is None
    if conn is None:
        conn = database.get_connection()
    c = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()

    pending = c.execute("""
        SELECT r.rtc_id, p.project_number, p.task_order_number
        FROM rtcs r
        JOIN projects p ON p.project_id = r.project_id
        WHERE p.project_status IN ('Placeholder', 'Pending')
        AND r.is_archived = 0
    """).fetchall()

    linked = 0
    for row in pending:
        rtc_id   = row["rtc_id"]
        proj_num = (row["project_number"] or "").split("_")[0].strip()
        task_num = (row["task_order_number"] or "").split("_")[0].strip()

        if not proj_num or is_placeholder(proj_num):
            continue

        match = c.execute("""
            SELECT project_id FROM projects
            WHERE project_number = ? AND task_order_number = ?
            AND project_status = 'Active'
        """, (proj_num, task_num)).fetchone()

        if match:
            # Store old project_id for orphan cleanup
            old_proj_row = c.execute(
                "SELECT project_id FROM rtcs WHERE rtc_id = ?", (rtc_id,)
            ).fetchone()
            old_project_id = old_proj_row["project_id"] if old_proj_row else None
            c.execute("""
                UPDATE rtcs SET project_id = ?, last_updated_at = ?,
                               auto_linked = 1
                WHERE rtc_id = ?
            """, (match["project_id"], now, rtc_id))
            linked += 1
            # Delete orphan Pending project row if nothing else references it
            if old_project_id and old_project_id != match["project_id"]:
                other_refs = c.execute(
                    "SELECT COUNT(*) FROM rtcs WHERE project_id = ?",
                    (old_project_id,)
                ).fetchone()[0]
                if other_refs == 0:
                    c.execute(
                        "DELETE FROM projects WHERE project_id = ? AND project_status = 'Pending'",
                        (old_project_id,)
                    )

    if linked:
        conn.commit()
        logger.info(f"Auto-relinked {linked} RTC(s) to Horizon")
    if close_after:
        conn.close()
    return linked


def nightly_imports():
    """
    Runs at the configured time (default midnight).
    Re-imports staff and PAR data then rebuilds the summary cache.
    """
    logger.info("Nightly import starting")

    if config.STAFF_LIST_PATH and Path(config.STAFF_LIST_PATH).exists():
        r = staff_import.run(str(config.STAFF_LIST_PATH))
        logger.info(f"Staff list: {r['rows_processed']} rows, "
                    f"{r['rows_inserted']} inserted, {r['rows_updated']} updated")
    else:
        logger.warning(f"Staff list: path not found ({config.STAFF_LIST_PATH})")

    r = par_import.run()
    logger.info(f"PAR import: {r['rows_processed']} rows, "
                f"{r['rows_inserted']} inserted, {r['rows_updated']} updated")

    relinked = relink_pending_rtcs()
    if relinked:
        logger.info(f"Re-linked {relinked} pending RTC(s) to Horizon")

    run_special_rtc_maintenance()

    summary_module.build()
    logger.info("Summary cache rebuilt")

    # Ensure reporting periods stay 3 years ahead
    conn = database.get_connection()
    database.ensure_periods_through(conn, date.today() + relativedelta(years=3))
    conn.close()
    logger.info("Reporting periods extended through 3 years ahead")
    process_leavers()
    logger.info("Nightly import complete")
