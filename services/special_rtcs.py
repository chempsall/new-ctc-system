"""
services/special_rtcs.py
Special (non-project) RTCs: Annual Leave & Public Holidays, Training,
and Day Release & Study Leave. One RTC per department per type,
created and maintained by the nightly job.

Also owns the bank-holidays cache (fetched from gov.uk).
"""

import logging
from datetime import datetime, timezone, date
from dateutil.relativedelta import relativedelta

import database

logger = logging.getLogger("resource_forecast.special_rtcs")


SPECIAL_RTC_CONFIGS = [
    {
        "project_number": "ID-06",
        "task_order":     "AL-PH",
        "name":           "Annual Leave & Public Holidays",
        "has_bank_holidays": True,
        "grades":         None,
    },
    {
        "project_number": "ID-04",
        "task_order":     "TRAINING",
        "name":           "Training Received",
        "has_bank_holidays": False,
        "grades":         None,
    },
    {
        "project_number": "IDUK-01",
        "task_order":     "DAYRELEASE",
        "name":           "Day Release & Study Leave",
        "has_bank_holidays": False,
        "grades":         ["P0"],
    },
]

SPECIAL_PROJECT_NUMBERS = {c["project_number"] for c in SPECIAL_RTC_CONFIGS}


def ensure_special_projects(c, now):
    for cfg in SPECIAL_RTC_CONFIGS:
        existing = c.execute("""
            SELECT project_id FROM projects
            WHERE project_number = ? AND task_order_number = ?
        """, (cfg["project_number"], cfg["task_order"])).fetchone()
        if not existing:
            c.execute("""
                INSERT INTO projects
                    (project_number, task_order_number, project_name, task_name,
                     project_status, project_type, last_imported)
                VALUES (?, ?, ?, ?, 'Active', 'UK Indirect', ?)
            """, (cfg["project_number"], cfg["task_order"],
                  cfg["name"], cfg["name"], now))


def get_bank_holidays(c, period_start):
    """Number of cached bank holidays falling in the given month."""
    year, month, _ = period_start.split("-")
    rows = c.execute("""
        SELECT COUNT(*) FROM bank_holidays
        WHERE date LIKE ?
    """, (f"{year}-{month}-%",)).fetchone()
    return rows[0] if rows else 0


def fetch_bank_holidays(c, now):
    """Refresh the bank_holidays cache from gov.uk. Failures are logged,
    not raised — the existing cache remains valid."""
    try:
        from imports import bank_holidays as bh_import
        holidays = bh_import.fetch()
        for date_iso, days in holidays.items():
            c.execute("""
                INSERT OR REPLACE INTO bank_holidays (date, days, last_updated)
                VALUES (?, ?, ?)
            """, (date_iso, days, now))
        logger.info(f"Bank holidays: cached {len(holidays)} entries")
    except Exception as e:
        logger.error(f"Failed to fetch bank holidays: {e}")


def run_special_rtc_maintenance():
    """
    Maintains special RTCs for all departments.
    Creates missing RTCs, adds new staff, pre-fills bank holidays,
    removes allocation rows older than 12 months.
    """
    conn = database.get_connection()
    c    = conn.cursor()
    now  = datetime.now(timezone.utc).isoformat()

    today          = date.today()
    current_period = today.replace(day=1).isoformat()
    cutoff_period  = (today - relativedelta(months=12)).replace(day=1).isoformat()
    future_end     = (today + relativedelta(months=11)).replace(day=1)

    fetch_bank_holidays(c, now)
    ensure_special_projects(c, now)

    departments = [r["department"] for r in c.execute("""
        SELECT DISTINCT department FROM staff
        WHERE department IS NOT NULL
        AND department != '_GENERIC'
        AND (end_date IS NULL OR end_date > ?)
    """, (current_period,)).fetchall()]

    rtcs_created   = 0
    staff_added    = 0
    periods_filled = 0

    for dept in departments:
        for cfg in SPECIAL_RTC_CONFIGS:
            proj = c.execute("""
                SELECT project_id FROM projects
                WHERE project_number = ? AND task_order_number = ?
            """, (cfg["project_number"], cfg["task_order"])).fetchone()
            if not proj:
                continue
            project_id = proj["project_id"]

            rtc = c.execute("""
                SELECT rtc_id FROM rtcs
                WHERE project_id = ? AND department = ? AND is_archived = 0
            """, (project_id, dept)).fetchone()

            if not rtc:
                database.ensure_periods_through(conn, future_end)
                c.execute("""
                    INSERT INTO rtcs
                        (project_id, department, start_date,
                         created_by, created_at, last_updated_by, last_updated_at,
                         is_archived, auto_linked)
                    VALUES (?, ?, ?, 'System', ?, 'System', ?, 0, 0)
                """, (project_id, dept, current_period, now, now))
                rtc_id = c.lastrowid
                rtcs_created += 1
            else:
                rtc_id = rtc["rtc_id"]
                database.ensure_periods_through(conn, future_end)

            grade_filter = ""
            params = [dept, current_period]
            if cfg["grades"]:
                placeholders = ",".join("?" * len(cfg["grades"]))
                grade_filter = f"AND SUBSTR(job_title, 1, 2) IN ({placeholders})"
                params.extend(cfg["grades"])

            staff_rows = c.execute(f"""
                SELECT horizon_person_number FROM staff
                WHERE department = ?
                AND (end_date IS NULL OR end_date > ?)
                AND NOT horizon_person_number LIKE 'GENERIC-%'
                {grade_filter}
            """, params).fetchall()

            periods = c.execute("""
                SELECT period_start FROM reporting_periods
                WHERE period_start >= ? AND period_start <= ?
                ORDER BY period_start
            """, (current_period, future_end.isoformat())).fetchall()
            period_list = [p["period_start"] for p in periods]

            # Bank-holiday defaults are identical for every person —
            # compute once per period, not once per (person, period).
            bh_by_period = {
                period: (get_bank_holidays(c, period) if cfg["has_bank_holidays"] else 0)
                for period in period_list
            }

            for sr in staff_rows:
                pid = sr["horizon_person_number"]
                for period in period_list:
                    days = bh_by_period[period]
                    c.execute("""
                        INSERT OR IGNORE INTO allocations
                            (horizon_person_number, rtc_id, period_start, days, last_updated)
                        VALUES (?, ?, ?, ?, ?)
                    """, (pid, rtc_id, period, days, now))
                    if c.rowcount:
                        staff_added += 1
                        if days > 0:
                            periods_filled += 1

            c.execute("""
                DELETE FROM allocations
                WHERE rtc_id = ? AND period_start < ?
            """, (rtc_id, cutoff_period))

    conn.commit()
    conn.close()
    logger.info(f"Special RTCs: {rtcs_created} created, {staff_added} rows added, "
                f"{periods_filled} bank holiday periods filled")
