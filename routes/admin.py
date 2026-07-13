"""
routes/admin.py
Admin page and all /admin/* endpoints. Every state-changing route is
protected by require_admin (bearer token).
"""

import json
import logging
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

from dateutil.relativedelta import relativedelta
from flask import Blueprint, jsonify, request, render_template

import config
import database
import summary as summary_module
from imports import staff_list as staff_import
from imports import par_import
from services.identity import get_current_user, require_admin
from services.jobs import relink_pending_rtcs
from services.special_rtcs import run_special_rtc_maintenance

logger = logging.getLogger("resource_forecast.admin")

admin_bp = Blueprint("admin", __name__)


@admin_bp.route("/admin")
def admin_index():
    return render_template("admin.html")


@admin_bp.route("/admin/rtcs/<int:rtc_id>", methods=["DELETE"])
@require_admin
def admin_delete_rtc(rtc_id):
    """Permanently deletes an RTC and all its allocations."""
    conn = database.get_connection()
    c    = conn.cursor()
    rtc = c.execute("SELECT project_id FROM rtcs WHERE rtc_id = ?", (rtc_id,)).fetchone()
    if not rtc:
        conn.close()
        return jsonify({"error": f"RTC {rtc_id} not found"}), 404
    project_id = rtc["project_id"]
    c.execute("DELETE FROM allocations WHERE rtc_id = ?", (rtc_id,))
    alloc_count = c.rowcount
    c.execute("DELETE FROM rtcs WHERE rtc_id = ?", (rtc_id,))
    # Clean up orphaned placeholder project
    other_refs = c.execute("SELECT COUNT(*) FROM rtcs WHERE project_id = ?", (project_id,)).fetchone()[0]
    if other_refs == 0:
        proj = c.execute("SELECT project_status FROM projects WHERE project_id = ?", (project_id,)).fetchone()
        if proj and proj["project_status"] == "Placeholder":
            c.execute("DELETE FROM projects WHERE project_id = ?", (project_id,))
    conn.commit()
    conn.close()
    summary_module.mark_dirty()
    logger.info(f"Admin: RTC {rtc_id} deleted ({alloc_count} allocation rows removed)")
    return jsonify({"status": "ok", "allocations_removed": alloc_count})


@admin_bp.route("/admin/import/staff", methods=["POST"])
@require_admin
def admin_import_staff():
    path = (request.json or {}).get("file_path") or str(config.STAFF_LIST_PATH)
    if not path or not Path(path).exists():
        return jsonify({"error": f"File not found: {path}"}), 400
    result = staff_import.run(path)
    summary_module.build()
    logger.info(f"Admin: staff import completed — {result.get('rows_processed',0)} rows processed, "
                f"{result.get('rows_inserted',0)} inserted, {result.get('rows_updated',0)} updated")
    return jsonify(result)


@admin_bp.route("/admin/import/par", methods=["POST"])
@require_admin
def admin_import_par():
    path = (request.json or {}).get("file_path") or str(config.PAR_ACTUALS_PATH)
    if not path or not Path(path).exists():
        return jsonify({"error": f"File not found: {path}"}), 400
    result = par_import.run(path)
    summary_module.build()
    logger.info(f"Admin: PAR import completed — {result.get('rows_processed',0)} rows processed, "
                f"{result.get('rows_inserted',0)} inserted, {result.get('rows_updated',0)} updated")
    return jsonify(result)


@admin_bp.route("/admin/import-log")
@require_admin
def admin_import_log():
    conn = database.get_connection()
    rows = conn.execute("""
        SELECT import_type, filename, started_at, completed_at,
               rows_processed, rows_inserted, rows_updated, errors
        FROM import_log ORDER BY started_at DESC LIMIT 100
    """).fetchall()
    conn.close()
    result = []
    for r in rows:
        row = dict(r)
        row["errors"] = json.loads(row["errors"] or "[]")
        result.append(row)
    return jsonify(result)


@admin_bp.route("/admin/import-ctc", methods=["POST"])
@require_admin
def admin_import_ctc():
    """
    Imports a single CTC Excel file's data as a new RTC.
    Data is pre-parsed by the browser using SheetJS and sent as JSON.
    """
    data     = request.json or {}
    user     = get_current_user()
    now      = datetime.now(timezone.utc).isoformat()
    conn     = database.get_connection()
    c        = conn.cursor()

    proj_num   = (data.get("projNum") or "00000000").strip()
    task_num   = (data.get("taskNum") or "000").strip()
    dept       = (data.get("dept") or "").strip()
    pd_raw     = (data.get("pdRaw") or "").strip()
    pm_raw     = (data.get("pmRaw") or "").strip()
    staff_list = data.get("staff", [])
    first_alloc = data.get("firstAlloc", datetime.now(timezone.utc).date().replace(day=1).isoformat())
    last_alloc  = data.get("lastAlloc", first_alloc)

    NO_HORIZON = "No Horizon Record Found"
    if not dept or NO_HORIZON in dept:
        dept = "UK010117-UK-BSV-Services London"
    pd_clean = None if not pd_raw or NO_HORIZON in pd_raw else pd_raw
    pm_clean = None if not pm_raw or NO_HORIZON in pm_raw else pm_raw

    # Look up project — exact match only
    proj_row = c.execute("""
        SELECT project_id, project_name FROM projects
        WHERE project_number = ? AND task_order_number = ?
        AND project_status = 'Active'
    """, (proj_num, task_num)).fetchone()

    if proj_row:
        project_id = proj_row["project_id"]
        # Check for existing RTC
        existing = c.execute(
            "SELECT rtc_id FROM rtcs WHERE project_id = ?", (project_id,)
        ).fetchone()
        if existing:
            conn.close()
            return jsonify({"error": f"An RTC already exists for {proj_num}/{task_num}"}), 409
    else:
        # Create Pending project row
        suffix = datetime.now().strftime("%Y%m%dT%H%M%S%f")[:20]
        unique_task = f"{task_num}_{suffix}" if task_num not in {"000", "", "tbc", "tbd"} else f"PLACEHOLDER_{suffix}"
        c.execute("""
            INSERT INTO projects
                (project_number, task_order_number, project_name, task_name,
                 project_customer, project_director, project_manager,
                 project_status, last_imported)
            VALUES (?, ?, ?, '', NULL, ?, ?, 'Pending', ?)
        """, (proj_num, unique_task,
              f"{proj_num} / {task_num}",
              pd_clean, pm_clean, now))
        project_id = c.lastrowid

    # Create RTC
    file_name = data.get("fileName", "")
    # Check if this filename has been imported before
    if file_name:
        existing_import = c.execute(
            "SELECT rtc_id FROM rtcs WHERE source_file = ?", (file_name,)
        ).fetchone()
        if existing_import:
            conn.close()
            return jsonify({"error": f"This file has already been imported ({file_name})"}), 409

    c.execute("""
        INSERT INTO rtcs
            (project_id, department, start_date,
             created_by, created_at, last_updated_by, last_updated_at,
             is_archived, auto_linked, source_file)
        VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, ?)
    """, (project_id, dept, first_alloc, user, now, user, now, file_name))
    rtc_id = c.lastrowid

    # Ensure reporting periods cover the full range + 12 months
    _start  = date.fromisoformat(first_alloc)
    _end    = date.fromisoformat(last_alloc)
    target  = _start + relativedelta(months=11)
    while target < _end:
        target += relativedelta(months=12)
    database.ensure_periods_through(conn, target)

    # Get all periods from first_alloc to target
    periods = c.execute("""
        SELECT period_start FROM reporting_periods
        WHERE period_start >= ? AND period_start <= ?
        ORDER BY period_start
    """, (first_alloc, target.isoformat())).fetchall()
    period_starts = [p["period_start"] for p in periods]

    # Add staff and allocations
    staff_added   = 0
    staff_skipped = 0
    rows_added    = 0
    skipped_names = []

    for s in staff_list:
        name = s.get("name", "").strip()
        if not name:
            continue
        
        skip_reason = None

        # Ladder 1: exact name
        staff_row = c.execute("""
            SELECT horizon_person_number FROM staff
            WHERE name = ? AND (end_date IS NULL OR end_date > ?)
        """, (name, first_alloc)).fetchone()

        if not staff_row:
            # Ladder 2: case/whitespace-normalised exact
            staff_row = c.execute("""
                SELECT horizon_person_number FROM staff
                WHERE LOWER(TRIM(name)) = LOWER(TRIM(?))
                AND (end_date IS NULL OR end_date > ?)
            """, (name, first_alloc)).fetchone()

        if not staff_row:
            # Ladder 3: surname + first initial prefix
            parts = name.split(",")
            if len(parts) >= 2:
                surname   = parts[0].strip()
                first_ini = parts[1].strip()[:1]
                if surname and first_ini:
                    matches = c.execute("""
                        SELECT horizon_person_number FROM staff
                        WHERE LOWER(name) LIKE LOWER(?)
                        AND (end_date IS NULL OR end_date > ?)
                    """, (f"{surname}, {first_ini}%", first_alloc)).fetchall()
                    if len(matches) == 1:
                        staff_row = matches[0]
                    elif len(matches) > 1:
                        skip_reason = "ambiguous"

        if not staff_row:
            staff_skipped += 1
            skipped_names.append({"name": name,
                                  "reason": skip_reason or "not found"})
            continue

        pid    = staff_row["horizon_person_number"]
        allocs = s.get("allocs", {})
        staff_added += 1

        for period in period_starts:
            try:
                days = float(allocs.get(period, 0) or 0)
            except (ValueError, TypeError):
                days = 0.0
            c.execute("""
                INSERT OR IGNORE INTO allocations
                    (horizon_person_number, rtc_id, period_start, days, last_updated)
                VALUES (?, ?, ?, ?, ?)
            """, (pid, rtc_id, period, days, now))
            if days > 0:
                rows_added += 1

    conn.commit()
    conn.close()
    summary_module.mark_dirty()
    logger.info(f"Admin: CTC import — RTC {rtc_id} created for {proj_num}/{task_num}, "
                f"{staff_added} staff, {rows_added} allocation rows")
    return jsonify({"status": "ok", "rtc_id": rtc_id,
                    "staff_added": staff_added, "staff_skipped": staff_skipped,
                    "skipped_names": skipped_names,
                    "rows_added": rows_added})


@admin_bp.route("/admin/special-rtcs", methods=["POST"])
@require_admin
def admin_special_rtcs():
    """Manually triggers special RTC maintenance."""
    run_special_rtc_maintenance()
    summary_module.mark_dirty()
    return jsonify({"status": "ok"})


@admin_bp.route("/admin/refresh-linked", methods=["POST"])
@require_admin
def admin_refresh_linked():
    """
    Re-syncs each linked RTC's department from the authoritative PAR
    project_organisation. (Project name/task/customer/PD/PM live on the
    shared projects row and are already refreshed by the PAR import.)
    """
    conn = database.get_connection()
    c    = conn.cursor()
    now  = datetime.now(timezone.utc).isoformat()

    # Find all RTCs linked to Active projects
    rows = c.execute("""
        SELECT r.rtc_id, r.department,
               p.project_id, p.project_name, p.task_name,
               p.project_customer, p.project_director,
               p.project_manager, p.project_organisation
        FROM rtcs r
        JOIN projects p ON p.project_id = r.project_id
        WHERE p.project_status = 'Active'
        AND r.is_archived = 0
    """).fetchall()

    updated = 0
    for row in rows:
        new_dept = row["project_organisation"] or row["department"]
        if new_dept == row["department"]:
            continue  # no change — skip to avoid clobbering last_updated_at
        c.execute("""
            UPDATE rtcs SET department = ?, last_updated_at = ?
            WHERE rtc_id = ?
        """, (new_dept, now, row["rtc_id"]))
        updated += 1

    conn.commit()
    conn.close()
    summary_module.mark_dirty()
    logger.info(f"Admin: refreshed PAR data for {updated} linked RTCs")
    return jsonify({"status": "ok", "updated": updated})


@admin_bp.route("/admin/relink-pending", methods=["POST"])
@require_admin
def admin_relink_pending():
    """Manually triggers re-linking of pending RTCs to Horizon."""
    linked = relink_pending_rtcs()
    summary_module.mark_dirty()
    logger.info(f"Admin: re-link pending RTCs — {linked} linked")
    return jsonify({"status": "ok", "linked": linked})


@admin_bp.route("/admin/rebuild-summary", methods=["POST"])
@require_admin
def admin_rebuild_summary():
    summary_module.build()
    logger.info("Admin: summary cache rebuilt manually")
    return jsonify({"status": "ok",
                    "rebuilt_at": datetime.now(timezone.utc).isoformat()})


@admin_bp.route("/admin/run-cleanup", methods=["POST"])
@require_admin
def admin_run_cleanup():
    """
    Archives RTCs that have no future allocations and no allocations
    last calendar month. Data is preserved — RTCs are never deleted,
    just hidden from the default list view.
    """
    now         = datetime.now(timezone.utc)
    today       = now.date().isoformat()

    conn = database.get_connection()
    c    = conn.cursor()

    # Archive RTCs with no future allocations AND no allocations last calendar month
    last_month_start = (now.date().replace(day=1) - timedelta(days=1)).replace(day=1).isoformat()
    last_month_end   = now.date().replace(day=1).isoformat()
    eligible = c.execute("""
        SELECT r.rtc_id, p.project_number, p.project_name,
               r.last_opened, r.department
        FROM rtcs r
        JOIN projects p ON p.project_id = r.project_id
        WHERE r.is_archived = 0
        AND p.project_number NOT IN ('ID-06', 'ID-04', 'IDUK-01')
        AND COALESCE((
            SELECT SUM(a.days)
            FROM allocations a
            WHERE a.rtc_id = r.rtc_id AND a.period_start >= ?
        ), 0) = 0
        AND COALESCE((
            SELECT SUM(a.days)
            FROM allocations a
            WHERE a.rtc_id = r.rtc_id
            AND a.period_start >= ? AND a.period_start < ?
        ), 0) = 0
    """, (today, last_month_start, last_month_end)).fetchall()

    archived = []
    for row in eligible:
        c.execute(
            "UPDATE rtcs SET is_archived = 1 WHERE rtc_id = ?",
            (row["rtc_id"],)
        )
        archived.append({
            "rtc_id":         row["rtc_id"],
            "project_number": row["project_number"],
            "project_name":   row["project_name"],
            "last_opened":    row["last_opened"],
        })

    conn.commit()
    conn.close()

    if archived:
        summary_module.build()
        logger.info(f"Admin: cleanup archived {len(archived)} RTC(s)")
    else:
        logger.info("Admin: cleanup — no RTCs eligible for archiving")

    return jsonify({
        "archived_count": len(archived),
        "archived":       archived,
    })


@admin_bp.route("/admin/log")
@require_admin
def admin_log():
    """Returns the last N lines of the application log."""
    n = int(request.args.get("n", 500))
    errors_only = request.args.get("errors_only", "0") == "1"
    log_dir  = Path(config.BASE_DIR) / "logs"
    log_file = log_dir / "app.log"
    if not log_file.exists():
        return jsonify({"lines": [], "total": 0})
    try:
        with open(log_file, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        if errors_only:
            lines = [l for l in lines if " ERROR " in l or " WARNING " in l]
        lines = [l.rstrip() for l in lines[-n:]]
        return jsonify({"lines": lines, "total": len(lines)})
    except Exception as e:
        logger.error(f"Failed to read log: {e}")
        return jsonify({"error": str(e)}), 500


@admin_bp.route("/admin/config")
@require_admin
def admin_config():
    """Returns non-sensitive config summary for diagnostics."""
    return jsonify({
        "environment":       config.ENV,
        "base_dir":          str(config.BASE_DIR),
        "flask_host":        config.FLASK_HOST,
        "flask_port":        config.FLASK_PORT,
        "staff_list_path":   str(config.STAFF_LIST_PATH),
        "par_path":          str(config.PAR_ACTUALS_PATH),
        "par_sharepoint":    config.PAR_USE_SHAREPOINT,
        "scheduler":         f"{config.SCHEDULER_HOUR:02d}:{config.SCHEDULER_MINUTE:02d}",
        "forecast_months":   config.FORECAST_HORIZON_MONTHS,
        "staff_list_exists": Path(config.STAFF_LIST_PATH).exists(),
        "current_user":      get_current_user(),
    })
