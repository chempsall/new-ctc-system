"""
app.py
Resource Forecast — Flask application entry point.

All configuration comes from config.py (which reads from .env and
environment variables). Nothing is hardcoded here.

To start the development server:
    python app.py

To start with a specific environment:
    set RF_ENV=beta       (Windows)
    export RF_ENV=beta    (Mac/Linux)
    python app.py
"""

import os
import json
import secrets
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

# Load .env file if present (install with: pip install python-dotenv)
try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).parent / ".env"
    if _env_path.exists():
        load_dotenv(_env_path)
        print(f"Loaded configuration from {_env_path}")
    else:
        print("No .env file found — using environment variables and config.py defaults.")
        print(f"To create one: copy .env.template .env")
except ImportError:
    print("python-dotenv not installed — using environment variables only.")
    print("Install with: pip install python-dotenv")

import config

# Validate configuration before starting
try:
    config.validate()
except ValueError as e:
    print(f"\n{'='*60}")
    print("CONFIGURATION ERROR — cannot start the application")
    print('='*60)
    print(e)
    print('='*60)
    raise SystemExit(1)

from flask import Flask, jsonify, request, render_template, abort, Response
from apscheduler.schedulers.background import BackgroundScheduler

import database
import summary as summary_module
from imports import staff_list  as staff_import
from imports import par_import

app = Flask(__name__)
app.secret_key = config.SECRET_KEY


# ---------------------------------------------------------------------------
# AUTH
# ---------------------------------------------------------------------------

def require_admin(f):
    """Decorator: requires the admin bearer token in the Authorization header."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not config.ADMIN_TOKEN:
            abort(503, description=(
                "Admin token not configured. "
                "Set RF_ADMIN_TOKEN in your .env file."
            ))
        auth = request.headers.get("Authorization", "")
        if not secrets.compare_digest(auth, f"Bearer {config.ADMIN_TOKEN}"):
            abort(403)
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# SCHEDULED JOBS
# ---------------------------------------------------------------------------

def _nightly_imports():
    """
    Runs at the time configured in config.py (default midnight).
    Imports all three data sources then rebuilds the summary cache.
    Paths come from config — no hardcoding.
    """
    print(f"[{datetime.now(timezone.utc).isoformat()}] Starting nightly import")

    # Staff list import
    if config.STAFF_LIST_PATH and Path(config.STAFF_LIST_PATH).exists():
        r = staff_import.run(str(config.STAFF_LIST_PATH))
        print(f"  Staff list: {r['rows_processed']} rows, "
              f"{r['rows_inserted']} inserted, {r['rows_updated']} updated"
              + (f", {len(r['errors'])} errors" if r["errors"] else ""))
    else:
        print(f"  Staff list: path not found ({config.STAFF_LIST_PATH})")

    # PAR actuals — source is SharePoint or local UK_PAR file (see config)
    r = par_import.run()
    print(f"  PAR import: {r['rows_processed']} rows, "
          f"{r['rows_inserted']} inserted, {r['rows_updated']} updated"
          + (f", {len(r['errors'])} errors" if r["errors"] else ""))

    summary_module.build()
    print(f"  Summary cache rebuilt")
    print(f"[{datetime.now(timezone.utc).isoformat()}] Nightly import complete")


# ---------------------------------------------------------------------------
# DASHBOARD ROUTES
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/summary")
def api_summary():
    """
    Pre-built summary JSON — the only endpoint the dashboard calls on load.
    Financial statistics are included as computed figures. Rates never exposed.
    """
    cached = summary_module.get_cached()
    if not cached:
        summary_module.build()
        cached = summary_module.get_cached()
    if not cached:
        return jsonify({"error": "Summary not yet available"}), 503

    payload = (cached["payload"]
               if isinstance(cached["payload"], str)
               else json.dumps(cached["payload"]))

    response = Response(payload, mimetype="application/json")
    response.headers["X-Generated-At"] = cached["generated_at"]
    return response


@app.route("/api/offices")
@app.route("/api/departments")
def api_offices():
    """Returns distinct departments from staff. Supports /api/offices
    for macro backwards compatibility."""
    conn = database.get_connection()
    rows = conn.execute("""
        SELECT DISTINCT department
        FROM staff
        WHERE department IS NOT NULL
        ORDER BY department
    """).fetchall()
    conn.close()
    return jsonify([{"office_name": r["department"],
                     "department":  r["department"]} for r in rows])


@app.route("/api/teams")
@app.route("/api/job-functions")
def api_teams():
    """Returns distinct job functions (disciplines) from staff.
    Supports /api/teams for macro backwards compatibility."""
    department = request.args.get("office") or request.args.get("department")
    conn = database.get_connection()
    if department:
        rows = conn.execute("""
            SELECT DISTINCT job_function
            FROM staff
            WHERE job_function IS NOT NULL AND department = ?
            ORDER BY job_function
        """, (department,)).fetchall()
    else:
        rows = conn.execute("""
            SELECT DISTINCT job_function
            FROM staff
            WHERE job_function IS NOT NULL
            ORDER BY job_function
        """).fetchall()
    conn.close()
    return jsonify([{"team_name":    r["job_function"],
                     "job_function": r["job_function"]} for r in rows])


@app.route("/api/staff")
def api_staff():
    """Staff list for macro dropdowns. No financial data."""
    department = request.args.get("office") or request.args.get("department")
    today  = datetime.now(timezone.utc).date().isoformat()
    conn   = database.get_connection()
    if department:
        rows = conn.execute("""
            SELECT horizon_person_number, name, job_title, job_family,
                   job_function, department
            FROM staff
            WHERE department = ? AND (end_date IS NULL OR end_date > ?)
            ORDER BY name
        """, (department, today)).fetchall()
    else:
        rows = conn.execute("""
            SELECT horizon_person_number, name, job_title, job_family,
                   job_function, department
            FROM staff
            WHERE end_date IS NULL OR end_date > ?
            ORDER BY name
        """, (today,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])




# ---------------------------------------------------------------------------
# PROJECT LOOKUP ENDPOINT
# ---------------------------------------------------------------------------

@app.route("/api/project")
def api_project():
    """
    Returns project metadata for a given project_number + task_order_number.
    Called by the macro when project number and task order are entered.
    Returns project details from the PAR-populated projects table.
    Returns empty object {} if not found.
    """
    project_number    = request.args.get("project_number", "").strip()
    task_order_number = request.args.get("task_order_number", "").strip()

    if not project_number or not task_order_number:
        return jsonify({})

    conn = database.get_connection()
    row  = conn.execute("""
        SELECT project_number, task_order_number, project_name, task_name,
               project_organisation, project_customer, project_director,
               project_manager, project_status, project_type,
               task_start_date, task_end_date
        FROM projects
        WHERE project_number = ? AND task_order_number = ?
    """, (project_number, task_order_number)).fetchone()
    conn.close()

    if not row:
        return jsonify({})

    return jsonify(dict(row))

# ---------------------------------------------------------------------------
# MACRO PUSH ENDPOINT
# ---------------------------------------------------------------------------

@app.route("/api/push", methods=["POST"])
def api_push():

    ### TEST CODE
    print(f"Content-Type: {request.content_type}")
    print(f"Raw data: {request.data[:200]}")
    print(f"Data length: {len(request.data)}")



    """
    Receives allocation data pushed by the Excel macro on file save.

    The push identifies the project via project_number + task_order_number,
    looks it up in the projects table (populated by PAR import), then
    creates/updates a ctc_files row for this specific file, and upserts
    the allocation rows belonging to that ctc_file.

    If the project is not yet in the database (PAR not yet run, or project
    is genuinely new and pending Horizon setup), a minimal project row is
    created as a placeholder and will be enriched by the next PAR import.
    """
    data = request.get_json(silent=True, force=True)
    if not data:
        return jsonify({"error": "No JSON body"}), 400

    missing = [f for f in ["file_path", "ctc_start_date", "allocations"]
               if f not in data]
    if missing:
        return jsonify({"error": f"Missing fields: {', '.join(missing)}"}), 400

    file_path      = data["file_path"]
    file_name      = os.path.basename(file_path)
    # Department is derived from project_organisation (from PAR data)
    # not sent by the macro — it's the Horizon organisation that owns the project
    department     = data.get("project_organisation", "")
    project_number = data.get("project_number", "").strip()
    task_order     = data.get("task_order_number", "").strip()
    ctc_start_date = data.get("ctc_start_date")

    conn = database.get_connection()
    c    = conn.cursor()
    now  = datetime.now(timezone.utc).isoformat()
    warnings = []

    # ------------------------------------------------------------------
    # Step 1: Find or create the project row
    # ------------------------------------------------------------------
    project_row = None
    if not _is_placeholder(project_number) and task_order:
        project_row = c.execute("""
            SELECT project_id FROM projects
            WHERE project_number = ? AND task_order_number = ?
        """, (project_number, task_order)).fetchone()

    if project_row:
        project_id = project_row["project_id"]
    else:
        # Project not yet in database — create a placeholder.
        # The nightly PAR import will enrich this row when the Horizon
        # record becomes available.
        if _is_placeholder(project_number):
            warnings.append(
                f"Project number '{project_number}' looks like a placeholder. "
                "Please update it with the Horizon number when available."
            )
        c.execute("""
            INSERT INTO projects (
                project_number, task_order_number,
                project_name, task_name, project_status, last_imported
            ) VALUES (?,?,?,?,?,?)
            ON CONFLICT(project_number, task_order_number) DO UPDATE SET
                last_imported = excluded.last_imported
        """, (
            project_number or "UNKNOWN", task_order or "UNKNOWN",
            data.get("project_name", "No Horizon Record Found"),
            data.get("task_name",    "No Horizon Record Found"),
            "Pending",
            now
        ))
        project_id = c.execute("""
            SELECT project_id FROM projects
            WHERE project_number = ? AND task_order_number = ?
        """, (project_number or "UNKNOWN", task_order or "UNKNOWN")).fetchone()["project_id"]

    # ------------------------------------------------------------------
    # Step 2: Conflict detection
    # Two different files with same name and placeholder project number
    # ------------------------------------------------------------------
    conflict = False
    if _is_placeholder(project_number):
        dup = c.execute("""
            SELECT ctc_id FROM ctc_files
            WHERE file_path != ? AND file_path LIKE ?
            AND project_id IN (
                SELECT project_id FROM projects
                WHERE project_number = ?
            )
        """, (file_path, f"%{file_name}", project_number)).fetchone()
        if dup:
            conflict = True
            warnings.append(
                f"Conflict: '{file_name}' exists at another path with the "
                "same placeholder project number. Flagged for review."
            )

    # ------------------------------------------------------------------
    # Step 3: Upsert ctc_files row
    # ------------------------------------------------------------------
    existing_ctc = c.execute(
        "SELECT ctc_id, ctc_start_date FROM ctc_files WHERE file_path = ?",
        (file_path,)
    ).fetchone()

    start_date_changed = False
    previous_start     = None

    if existing_ctc:
        ctc_id = existing_ctc["ctc_id"]
        if (ctc_start_date and existing_ctc["ctc_start_date"]
                and ctc_start_date != existing_ctc["ctc_start_date"]):
            start_date_changed = True
            previous_start = existing_ctc["ctc_start_date"]
            warnings.append(
                f"CTC start date changed from {previous_start} to "
                f"{ctc_start_date}. Please verify allocations are correct."
            )
        c.execute("""
            UPDATE ctc_files SET
                project_id=?, department=?, ctc_start_date=?,
                conflict_flag=?, start_date_changed=?,
                previous_ctc_start_date = CASE WHEN ? THEN ? ELSE previous_ctc_start_date END,
                last_pushed=?, last_updated_by=?
            WHERE ctc_id=?
        """, (
            project_id, department, ctc_start_date,
            1 if conflict else 0,
            1 if start_date_changed else 0,
            start_date_changed, previous_start,
            now, data.get("last_updated_by", ""),
            ctc_id
        ))
    else:
        c.execute("""
            INSERT INTO ctc_files (
                project_id, department, ctc_start_date,
                file_path, conflict_flag, start_date_changed,
                last_pushed, last_updated_by
            ) VALUES (?,?,?,?,?,?,?,?)
        """, (
            project_id, department, ctc_start_date,
            file_path, 1 if conflict else 0, 0,
            now, data.get("last_updated_by", "")
        ))
        ctc_id = c.lastrowid

    # ------------------------------------------------------------------
    # Step 4: Upsert allocations (now keyed to ctc_id not project_id)
    # ------------------------------------------------------------------
    alloc_count = 0
    for alloc in data.get("allocations", []):
        person_id    = str(alloc.get("horizon_person_number", "")).strip()
        period_start = alloc.get("period_start")
        days         = alloc.get("days", 0)
        if not person_id or not period_start:
            continue
        if not c.execute(
            "SELECT 1 FROM staff WHERE horizon_person_number=?", (person_id,)
        ).fetchone():
            warnings.append(f"Person {person_id} not in staff list — skipped")
            continue
        c.execute("""
            INSERT INTO allocations
                (horizon_person_number, ctc_id, period_start, days, pushed_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(horizon_person_number, ctc_id, period_start)
            DO UPDATE SET days=excluded.days, pushed_at=excluded.pushed_at
        """, (person_id, ctc_id, period_start, days, now))
        alloc_count += 1

    c.execute("""
        INSERT INTO import_log
            (import_type, filename, started_at, completed_at,
             rows_processed, rows_inserted, rows_updated, errors)
        VALUES (?,?,?,?,?,?,?,?)
    """, ("xlsm_push", file_name, now, now, alloc_count, alloc_count, 0,
          json.dumps(warnings)))

    conn.commit()
    conn.close()

    import threading
    threading.Thread(target=summary_module.build, daemon=True).start()

    return jsonify({
        "status":   "ok",
        "ctc_id":   ctc_id,
        "project_id": project_id,
        "allocations": alloc_count,
        "warnings":    warnings
    })


# ---------------------------------------------------------------------------
# ADMIN ROUTES
# ---------------------------------------------------------------------------

@app.route("/admin")
@require_admin
def admin_index():
    return render_template("admin.html")


@app.route("/admin/import/staff", methods=["POST"])
@require_admin
def admin_import_staff():
    path = (request.json or {}).get("file_path") or str(config.STAFF_LIST_PATH)
    if not path or not Path(path).exists():
        return jsonify({"error": f"File not found: {path}"}), 400
    result = staff_import.run(path)
    summary_module.build()
    return jsonify(result)



@app.route("/admin/import/par", methods=["POST"])
@require_admin
def admin_import_par():
    path = (request.json or {}).get("file_path") or str(config.PAR_ACTUALS_PATH)
    if not path or not Path(path).exists():
        return jsonify({"error": f"File not found: {path}"}), 400
    result = par_import.run(path)
    summary_module.build()
    return jsonify(result)


@app.route("/admin/import-log")
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


@app.route("/admin/conflicts")
@require_admin
def admin_conflicts():
    conn = database.get_connection()
    rows = conn.execute("""
        SELECT cf.ctc_id, cf.file_path, cf.department,
               cf.last_pushed, p.project_number, p.task_order_number,
               p.project_name
        FROM ctc_files cf
        JOIN projects p ON p.project_id = cf.project_id
        WHERE cf.conflict_flag = 1
        ORDER BY cf.last_pushed DESC
    """).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/admin/start-date-changes")
@require_admin
def admin_start_date_changes():
    conn = database.get_connection()
    rows = conn.execute("""
        SELECT cf.ctc_id, cf.file_path, cf.ctc_start_date,
               cf.previous_ctc_start_date, cf.last_pushed,
               p.project_number, p.project_name
        FROM ctc_files cf
        JOIN projects p ON p.project_id = cf.project_id
        WHERE cf.start_date_changed = 1
        ORDER BY cf.last_pushed DESC
    """).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/admin/rebuild-summary", methods=["POST"])
@require_admin
def admin_rebuild_summary():
    summary_module.build()
    return jsonify({"status": "ok", "rebuilt_at": datetime.now(timezone.utc).isoformat()})


@app.route("/admin/clear-flag/conflict/<int:ctc_id>", methods=["POST"])
@require_admin
def admin_clear_conflict(ctc_id):
    conn = database.get_connection()
    conn.execute("UPDATE ctc_files SET conflict_flag=0 WHERE ctc_id=?", (ctc_id,))
    conn.commit()
    conn.close()
    summary_module.build()
    return jsonify({"status": "ok"})


@app.route("/admin/clear-flag/start-date/<int:ctc_id>", methods=["POST"])
@require_admin
def admin_clear_start_date_flag(ctc_id):
    conn = database.get_connection()
    conn.execute("""
        UPDATE ctc_files SET start_date_changed=0, previous_ctc_start_date=NULL
        WHERE ctc_id=?
    """, (ctc_id,))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@app.route("/admin/config")
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
    })


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

PLACEHOLDER_PATTERNS = {"xxxxxxxx", "12345678", "00000000", "tbc", "tbd", "n/a", ""}

def _is_placeholder(s: str) -> bool:
    if not s:
        return True
    c = s.lower().strip()
    if c in PLACEHOLDER_PATTERNS:
        return True
    if all(ch == "x" for ch in c) or all(ch == "0" for ch in c):
        return True
    return False

def _resolve_staff_id(cursor, name_string):
    if not name_string:
        return None
    clean = str(name_string).split("(")[0].strip()
    row = cursor.execute(
        "SELECT horizon_person_number FROM staff WHERE name=?", (clean,)
    ).fetchone()
    return row["horizon_person_number"] if row else None


# ---------------------------------------------------------------------------
# STARTUP
# ---------------------------------------------------------------------------

def create_app():
    database.initialise_database()

    scheduler = BackgroundScheduler()
    scheduler.add_job(
        _nightly_imports,
        trigger="cron",
        hour=config.SCHEDULER_HOUR,
        minute=config.SCHEDULER_MINUTE,
        id="nightly_imports",
        replace_existing=True
    )
    scheduler.start()
    return app


if __name__ == "__main__":
    print("\nResource Forecast")
    print("=" * 40)
    config.summary()
    print("=" * 40 + "\n")

    application = create_app()
    application.run(
        host=config.FLASK_HOST,
        port=config.FLASK_PORT,
        debug=config.FLASK_DEBUG,
        use_reloader=False
    )