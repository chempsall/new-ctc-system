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
    logger.info("Nightly import complete")
