"""
services/jobs.py
Scheduled work: the nightly import pipeline and the pending-RTC
re-link pass. Callable from the scheduler and from admin routes.
"""

import logging
from datetime import datetime, timezone, date, timedelta
from pathlib import Path

from dateutil.relativedelta import relativedelta

import config
import database
import summary as summary_module
from imports import staff_list as staff_import
from imports import par_import
from services.projects import is_placeholder
from services.special_rtcs import run_special_rtc_maintenance, SPECIAL_PROJECT_NUMBERS

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


def _grade_code(job_title: str) -> str | None:
    """Extract grade code (e.g. 'P3') from a job title string."""
    import re
    m = re.match(r"^([PT]\d)", job_title or "")
    return m.group(1) if m else None


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
    today        = _date.today().replace(day=1).isoformat()
    actual_today = _date.today().isoformat()

    logger = logging.getLogger("resource_forecast")

    # Find leavers with future allocations
    leavers = c.execute("""
        SELECT DISTINCT s.horizon_person_number, s.job_title, s.name
        FROM staff s
        JOIN allocations a ON a.horizon_person_number = s.horizon_person_number
        WHERE s.end_date IS NOT NULL
        AND s.end_date < %s
        AND a.period_start >= %s
        AND a.days > 0
    """, (actual_today, today)).fetchall()

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
            WHERE a.horizon_person_number = %s
            AND a.period_start >= %s
            AND a.days > 0
        """, (pid, today)).fetchall()

        for rtc_row in rtcs:
            rtc_id     = rtc_row["rtc_id"]
            is_special = rtc_row["project_number"] in SPECIAL_PROJECT_NUMBERS

            if is_special:
                # Just zero out future rows — no replacement
                c.execute("""
                    DELETE FROM allocations
                    WHERE horizon_person_number = %s AND rtc_id = %s
                    AND period_start >= %s
                """, (pid, rtc_id, today))
                zeroed += c.rowcount
            elif base_gid:
                # Transfer days to the base generic for this grade.
                # The insert-if-absent + additive UPDATE pair below handles
                # both cases: generic already on the RTC (days accumulate)
                # or not yet present (row created, then incremented).
                gid = base_gid

                # Get the leaver's future periods and days
                future_rows = c.execute("""
                    SELECT period_start, days FROM allocations
                    WHERE horizon_person_number = %s AND rtc_id = %s
                    AND period_start >= %s AND days > 0
                """, (pid, rtc_id, today)).fetchall()

                for row in future_rows:
                    period = row["period_start"]
                    days   = row["days"]
                    # Add to generic (insert-if-absent then UPDATE)
                    c.execute("""
                        INSERT INTO allocations
                            (horizon_person_number, rtc_id, period_start, days, last_updated)
                        VALUES (%s, %s, %s, 0, %s)
                        ON CONFLICT DO NOTHING
                    """, (gid, rtc_id, period, now))
                    c.execute("""
                        UPDATE allocations
                        SET days = days + %s, last_updated = %s
                        WHERE horizon_person_number = %s AND rtc_id = %s AND period_start = %s
                    """, (days, now, gid, rtc_id, period))
                    transferred += 1

                # Delete leaver's future rows
                c.execute("""
                    DELETE FROM allocations
                    WHERE horizon_person_number = %s AND rtc_id = %s
                    AND period_start >= %s
                """, (pid, rtc_id, today))
                zeroed += c.rowcount
            else:
                # Unknown grade — just zero out, log a warning
                logger.warning(f"Leaver {leaver['name']} ({pid}) has unknown grade "
                               f"{grade!r} — zeroing future rows without replacement")
                c.execute("""
                    UPDATE allocations SET days = 0, last_updated = %s
                    WHERE horizon_person_number = %s AND rtc_id = %s
                    AND period_start >= %s
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
        ORDER BY r.rtc_id
    """).fetchall()

    linked  = 0
    skipped = 0
    # Projects claimed during this run, so two pending RTCs matching the same
    # project cannot both link to it. Ordered by rtc_id above, so the RTC
    # created first wins and later ones are skipped.
    claimed = set()
    for row in pending:
        rtc_id   = row["rtc_id"]
        proj_num = (row["project_number"] or "").split("_")[0].strip()
        task_num = (row["task_order_number"] or "").split("_")[0].strip()

        if not proj_num or is_placeholder(proj_num):
            continue

        match = c.execute("""
            SELECT project_id FROM projects
            WHERE project_number = %s AND task_order_number = %s
            AND project_status = 'Active'
        """, (proj_num, task_num)).fetchone()

        if match:
            target_id = match["project_id"]

            # Guard: never attach this RTC to a project another RTC already
            # owns (mirrors the manual link/convert rules), and never let two
            # pending RTCs claim the same project in one run.
            already = c.execute(
                "SELECT COUNT(*) AS n FROM rtcs WHERE project_id = %s AND rtc_id != %s",
                (target_id, rtc_id)).fetchone()["n"]
            if already > 0 or target_id in claimed:
                skipped += 1
                logger.warning(
                    f"Auto-relink skipped: RTC {rtc_id} matches "
                    f"{proj_num}/{task_num} but that project is already linked "
                    f"to another RTC. Left unlinked for manual review.")
                continue
            claimed.add(target_id)

            # Store old project_id for orphan cleanup
            old_proj_row = c.execute(
                "SELECT project_id FROM rtcs WHERE rtc_id = %s", (rtc_id,)
            ).fetchone()
            old_project_id = old_proj_row["project_id"] if old_proj_row else None
            c.execute("""
                UPDATE rtcs SET project_id = %s, last_updated_at = %s,
                               auto_linked = 1
                WHERE rtc_id = %s
            """, (target_id, now, rtc_id))
            linked += 1
            # Delete orphan Pending project row if nothing else references it
            if old_project_id and old_project_id != target_id:
                other_refs = c.execute(
                    "SELECT COUNT(*) AS n FROM rtcs WHERE project_id = %s",
                    (old_project_id,)
                ).fetchone()["n"]
                if other_refs == 0:
                    c.execute(
                        "DELETE FROM projects WHERE project_id = %s "
                        "AND project_status IN ('Pending', 'Placeholder')",
                        (old_project_id,)
                    )

    if linked:
        conn.commit()
        logger.info(f"Auto-relinked {linked} RTC(s) to Horizon")
    if skipped:
        logger.warning(
            f"Auto-relink: {skipped} RTC(s) skipped because the matching "
            f"project is already linked to another RTC")
    if close_after:
        conn.close()
    return linked


def refresh_linked_rtcs(conn=None):
    """
    Re-syncs department (project_organisation) from PAR for all currently
    linked, non-archived RTCs. All other project attributes live on the
    shared projects row and are refreshed by the PAR import itself.
    Called from nightly_imports after the PAR import completes.
    """
    close = conn is None
    if conn is None:
        conn = database.get_connection()
    c   = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()

    rows = c.execute("""
        SELECT r.rtc_id, r.department, p.project_organisation
        FROM rtcs r
        JOIN projects p ON p.project_id = r.project_id
        WHERE p.project_status = 'Active'
        AND r.is_archived = 0
    """).fetchall()

    # PD, PM, customer, and names live on the shared projects row and are
    # refreshed directly by the PAR import; department is the only linked
    # attribute stored on the RTC itself.
    updated = 0
    for row in rows:
        new_dept = row["project_organisation"] or row["department"]
        if new_dept == row["department"]:
            continue
        c.execute("""
            UPDATE rtcs SET department = %s, last_updated_at = %s
            WHERE rtc_id = %s
        """, (new_dept, now, row["rtc_id"]))
        updated += c.rowcount

    conn.commit()
    if close:
        conn.close()
    if updated:
        logger.info(f"Refresh linked RTCs: {updated} updated from PAR")
    return updated


def archive_old_rtcs(conn=None):
    """
    Archives RTCs that have had zero allocations for the current month
    and the previous calendar month. Special RTCs are never archived.
    Called from nightly_imports and from the admin button.
    Returns count of archived RTCs.
    """
    close = conn is None
    if conn is None:
        conn = database.get_connection()
    c   = conn.cursor()
    now = datetime.now(timezone.utc)

    today          = now.date().replace(day=1).isoformat()
    last_month_start = (now.date().replace(day=1) - timedelta(days=1)).replace(day=1).isoformat()
    last_month_end   = now.date().replace(day=1).isoformat()

    special_ph = ",".join(["%s"] * len(SPECIAL_PROJECT_NUMBERS))
    eligible = c.execute(f"""
        SELECT r.rtc_id, p.project_number, p.project_name
        FROM rtcs r
        JOIN projects p ON p.project_id = r.project_id
        WHERE r.is_archived = 0
        AND p.project_number NOT IN ({special_ph})
        AND COALESCE((
            SELECT SUM(a.days) FROM allocations a
            WHERE a.rtc_id = r.rtc_id AND a.period_start >= %s
        ), 0) = 0
        AND COALESCE((
            SELECT SUM(a.days) FROM allocations a
            WHERE a.rtc_id = r.rtc_id
            AND a.period_start >= %s AND a.period_start < %s
        ), 0) = 0
    """, (*sorted(SPECIAL_PROJECT_NUMBERS),
          today, last_month_start, last_month_end)).fetchall()

    for row in eligible:
        c.execute("UPDATE rtcs SET is_archived = 1 WHERE rtc_id = %s", (row["rtc_id"],))
    count = len(eligible)

    conn.commit()
    if close:
        conn.close()
    if count:
        logger.info(f"Archive old RTCs: {count} archived")
    return count


def _run_step(name, fn, *args, **kwargs):
    """
    Runs one nightly step in isolation.

    Any exception is logged with its traceback and swallowed, so a single
    failing step cannot abort the ones that follow. Each step opens its own
    database connection, so a failure in one leaves the others unaffected.
    Returns (ok, result).
    """
    try:
        return True, fn(*args, **kwargs)
    except Exception:
        logger.exception(f"Nightly step failed: {name}")
        return False, None


def nightly_imports():
    """
    Runs at the configured time (default 08:00).
    Re-imports staff and PAR data then rebuilds the summary cache.

    Every step is isolated: if one fails it is logged with a traceback and the
    remainder still run, so (for example) a relink error cannot prevent the
    summary cache from being rebuilt. Any failures are summarised at the end.
    """
    logger.info("Nightly import starting")
    failed = []

    def step(name, fn, *args, **kwargs):
        ok, result = _run_step(name, fn, *args, **kwargs)
        if not ok:
            failed.append(name)
        return ok, result

    # --- Staff list -------------------------------------------------------
    if config.STAFF_LIST_PATH and Path(config.STAFF_LIST_PATH).exists():
        ok, r = step("staff import", staff_import.run, str(config.STAFF_LIST_PATH))
        if ok:
            logger.info(f"Staff list: {r['rows_processed']} rows, "
                        f"{r['rows_inserted']} inserted, {r['rows_updated']} updated")
    else:
        logger.warning(f"Staff list: path not found ({config.STAFF_LIST_PATH})")

    # --- PAR --------------------------------------------------------------
    ok, r = step("PAR import", par_import.run)
    if ok:
        logger.info(f"PAR import: {r['rows_processed']} rows, "
                    f"{r['rows_inserted']} inserted, {r['rows_updated']} updated")

    # --- Linking ----------------------------------------------------------
    step("refresh linked RTCs", refresh_linked_rtcs)

    ok, relinked = step("relink pending RTCs", relink_pending_rtcs)
    if ok and relinked:
        logger.info(f"Re-linked {relinked} pending RTC(s) to Horizon")

    # --- Maintenance ------------------------------------------------------
    step("special RTC maintenance", run_special_rtc_maintenance)
    step("process leavers", process_leavers)

    ok, archived = step("archive old RTCs", archive_old_rtcs)
    if ok and archived:
        logger.info(f"Nightly archive: {archived} RTC(s) archived")

    # --- Summary ----------------------------------------------------------
    ok, _ = step("summary rebuild", summary_module.build)
    if ok:
        logger.info("Summary cache rebuilt")

    # --- Reporting periods ------------------------------------------------
    def _extend_periods():
        conn = database.get_connection()
        try:
            database.ensure_periods_through(conn, date.today() + relativedelta(years=3))
        finally:
            conn.close()

    ok, _ = step("extend reporting periods", _extend_periods)
    if ok:
        logger.info("Reporting periods extended through 3 years ahead")

    if failed:
        logger.warning(
            f"Nightly import complete with {len(failed)} failed step(s): "
            f"{', '.join(failed)}. See the traceback(s) above.")
    else:
        logger.info("Nightly import complete")