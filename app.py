"""
app.py
Resource Forecast — Flask application entry point.

All configuration comes from config.py (which reads from .env and
environment variables). Nothing is hardcoded here.

To start the development server:
    python app.py
"""

import json
import secrets
import threading
from datetime import datetime, timezone, date, timedelta
from functools import wraps
from pathlib import Path

try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).parent / ".env"
    if _env_path.exists():
        load_dotenv(_env_path)
        print(f"Loaded configuration from {_env_path}")
    else:
        print("No .env file found — using environment variables and config.py defaults.")
except ImportError:
    print("python-dotenv not installed — using environment variables only.")

import config

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
from imports import staff_list as staff_import
from imports import par_import

app = Flask(__name__)
app.secret_key = config.SECRET_KEY


# ---------------------------------------------------------------------------
# IDENTITY
# Lightweight placeholder — returns a hardcoded user name for now.
# Replace this single function when WSP corporate auth is available
# (Microsoft SSO or equivalent). Every part of the codebase that needs
# to know "who is doing this" calls get_current_user() and nothing else.
# ---------------------------------------------------------------------------

def get_current_user() -> str:
    """Returns the current user's display name."""
    # TODO: replace with real auth when corporate SSO is available
    return "Test User"


# ---------------------------------------------------------------------------
# AUTH (admin routes)
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
    Runs at the configured time (default midnight).
    Re-imports staff and PAR data then rebuilds the summary cache.
    """
    print(f"[{datetime.now(timezone.utc).isoformat()}] Starting nightly import")

    if config.STAFF_LIST_PATH and Path(config.STAFF_LIST_PATH).exists():
        r = staff_import.run(str(config.STAFF_LIST_PATH))
        print(f"  Staff list: {r['rows_processed']} rows, "
              f"{r['rows_inserted']} inserted, {r['rows_updated']} updated")
    else:
        print(f"  Staff list: path not found ({config.STAFF_LIST_PATH})")

    r = par_import.run()
    print(f"  PAR import: {r['rows_processed']} rows, "
          f"{r['rows_inserted']} inserted, {r['rows_updated']} updated")

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
    """Pre-built summary JSON — the only endpoint the dashboard calls on load."""
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
    """Returns distinct departments from staff."""
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
    """Returns distinct job functions from staff."""
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
    """Staff list for RTC staff picker."""
    department = request.args.get("office") or request.args.get("department")
    today = datetime.now(timezone.utc).date().isoformat()
    conn = database.get_connection()
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


@app.route("/api/project")
def api_project():
    """
    Returns project metadata for a given project_number + task_order_number.
    Called by the RTC editor when project details are entered.
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
# RTC API
# ---------------------------------------------------------------------------

@app.route("/api/rtcs")
def api_rtcs():
    """
    Returns the list of RTCs for the front page.

    Query params:
      department  — filter by cost centre
      pm          — filter by project manager (partial match)
      pd          — filter by project director (partial match)
      search      — free text across project number and name
      archived    — "1" to include archived RTCs (default: exclude)

    Sorted by current-month allocation hours descending, then project name.
    Slightly stale is acceptable — uses the same cached approach as summary.
    """
    conn = database.get_connection()
    now  = datetime.now(timezone.utc)
    today = now.date().isoformat()
    current_period = now.date().replace(day=1).isoformat()
    thirty_days_ago = (now.date() - timedelta(days=30)).isoformat()

    dept    = request.args.get("department", "").strip()
    pm      = request.args.get("pm", "").strip()
    pd_arg  = request.args.get("pd", "").strip()
    search  = request.args.get("search", "").strip()
    archived = request.args.get("archived", "0").strip()

    rows = conn.execute("""
        SELECT
            r.rtc_id,
            r.department,
            r.start_date,
            r.created_by,
            r.created_at,
            r.last_updated_by,
            r.last_updated_at,
            r.last_opened_by,
            r.last_opened,
            r.is_archived,
            p.project_id,
            p.project_number,
            p.task_order_number,
            p.project_name,
            p.task_name,
            p.project_director,
            p.project_manager,
            p.project_status,
            COALESCE((
                SELECT SUM(a.days)
                FROM allocations a
                WHERE a.rtc_id = r.rtc_id
                AND a.period_start = ?
            ), 0) AS current_month_days,
            COALESCE((
                SELECT SUM(a.days)
                FROM allocations a
                WHERE a.rtc_id = r.rtc_id
                AND a.period_start > ?
            ), 0) AS future_days
        FROM rtcs r
        JOIN projects p ON p.project_id = r.project_id
        WHERE 1=1
        AND (? = '1' OR r.is_archived = 0)
    """, (current_period, today, archived)).fetchall()

    conn.close()

    # Apply filters in Python (simpler than building dynamic SQL)
    result = []
    for r in rows:
        row = dict(r)
        if dept   and row["department"] != dept:           continue
        if pm     and pm.lower() not in (row["project_manager"] or "").lower():  continue
        if pd_arg and pd_arg.lower() not in (row["project_director"] or "").lower(): continue
        if search:
            q = search.lower()
            if q not in (row["project_number"] or "").lower() and \
               q not in (row["project_name"] or "").lower() and \
               q not in (row["task_name"] or "").lower():
                continue

        # Compute status
        last_opened = row["last_opened"]
        if row["is_archived"]:
            status = "archived"
        elif not last_opened or last_opened[:10] < thirty_days_ago:
            status = "needs_review"
        else:
            status = "active"

        row["status"] = status
        result.append(row)

    # Sort: current_month_days descending, then project_name ascending
    result.sort(key=lambda r: (-r["current_month_days"], r["project_name"] or ""))
    return jsonify(result)


@app.route("/api/rtcs", methods=["POST"])
def api_create_rtc():
    """
    Creates a new blank RTC.

    Required body fields:
      project_number, task_order_number, department, start_date

    The project must already exist in the projects table (from PAR import).
    If not found, a placeholder project row is created.
    """
    data = request.get_json(silent=True, force=True)
    if not data:
        return jsonify({"error": "No JSON body"}), 400

    missing = [f for f in ["project_number", "task_order_number",
                            "department", "start_date"] if f not in data]
    if missing:
        return jsonify({"error": f"Missing fields: {', '.join(missing)}"}), 400

    user = get_current_user()
    now  = datetime.now(timezone.utc).isoformat()
    conn = database.get_connection()
    c    = conn.cursor()

    project_id = _get_or_create_project(c, data, now)

    c.execute("""
        INSERT INTO rtcs (project_id, department, start_date,
                          created_by, created_at,
                          last_updated_by, last_updated_at,
                          last_opened_by, last_opened,
                          is_archived)
        VALUES (?,?,?,?,?,?,?,?,?,0)
    """, (project_id, data["department"], data["start_date"],
          user, now, user, now, user, now))

    rtc_id = c.lastrowid
    conn.commit()
    conn.close()

    threading.Thread(target=summary_module.build, daemon=True).start()
    return jsonify({"rtc_id": rtc_id}), 201


@app.route("/api/rtcs/<int:rtc_id>/duplicate", methods=["POST"])
def api_duplicate_rtc(rtc_id):
    """
    Creates a new RTC by duplicating the staff list from an existing one.
    Project details, start date, and allocations are NOT copied —
    everything except the staff list must be re-entered for the new RTC.

    Required body fields:
      project_number, task_order_number, department, start_date
    """
    data = request.get_json(silent=True, force=True)
    if not data:
        return jsonify({"error": "No JSON body"}), 400

    missing = [f for f in ["project_number", "task_order_number",
                            "department", "start_date"] if f not in data]
    if missing:
        return jsonify({"error": f"Missing fields: {', '.join(missing)}"}), 400

    user = get_current_user()
    now  = datetime.now(timezone.utc).isoformat()
    conn = database.get_connection()
    c    = conn.cursor()

    # Confirm the source RTC exists
    source = c.execute(
        "SELECT rtc_id FROM rtcs WHERE rtc_id = ?", (rtc_id,)
    ).fetchone()
    if not source:
        conn.close()
        return jsonify({"error": f"RTC {rtc_id} not found"}), 404

    project_id = _get_or_create_project(c, data, now)

    c.execute("""
        INSERT INTO rtcs (project_id, department, start_date,
                          created_by, created_at,
                          last_updated_by, last_updated_at,
                          last_opened_by, last_opened,
                          is_archived)
        VALUES (?,?,?,?,?,?,?,?,?,0)
    """, (project_id, data["department"], data["start_date"],
          user, now, user, now, user, now))

    new_rtc_id = c.lastrowid

    # Copy the distinct set of people who appear in the source RTC,
    # but create NO allocation rows — they start from zero in the new RTC.
    staff_members = c.execute("""
        SELECT DISTINCT horizon_person_number
        FROM allocations
        WHERE rtc_id = ?
    """, (rtc_id,)).fetchall()

    # Insert zero-allocation rows for the new RTC's start month only,
    # so the staff appear in the editor ready to be allocated.
    for s in staff_members:
        c.execute("""
            INSERT OR IGNORE INTO allocations
                (horizon_person_number, rtc_id, period_start, days, last_updated)
            VALUES (?, ?, ?, 0, ?)
        """, (s["horizon_person_number"], new_rtc_id, data["start_date"], now))

    conn.commit()
    conn.close()

    threading.Thread(target=summary_module.build, daemon=True).start()
    return jsonify({"rtc_id": new_rtc_id, "staff_copied": len(staff_members)}), 201


@app.route("/api/rtcs/<int:rtc_id>")
def api_get_rtc(rtc_id):
    """
    Returns full RTC detail including all allocations.
    Also updates last_opened and last_opened_by.
    Called when the editing screen loads.
    """
    user = get_current_user()
    now  = datetime.now(timezone.utc).isoformat()
    conn = database.get_connection()
    c    = conn.cursor()

    # Update last_opened
    c.execute("""
        UPDATE rtcs SET last_opened_by = ?, last_opened = ?
        WHERE rtc_id = ?
    """, (user, now, rtc_id))
    conn.commit()

    rtc = c.execute("""
        SELECT r.*, p.project_number, p.task_order_number, p.project_name,
               p.task_name, p.project_organisation, p.project_customer,
               p.project_director, p.project_manager, p.project_status
        FROM rtcs r
        JOIN projects p ON p.project_id = r.project_id
        WHERE r.rtc_id = ?
    """, (rtc_id,)).fetchone()

    if not rtc:
        conn.close()
        return jsonify({"error": f"RTC {rtc_id} not found"}), 404

    # Fetch all allocations, grouped by person
    alloc_rows = c.execute("""
        SELECT a.horizon_person_number, a.period_start, a.days,
               s.name, s.job_title, s.job_function
        FROM allocations a
        JOIN staff s ON s.horizon_person_number = a.horizon_person_number
        WHERE a.rtc_id = ?
        ORDER BY s.name, a.period_start
    """, (rtc_id,)).fetchall()

    # Fetch reporting periods from start_date forward (36 months)
    periods = c.execute("""
        SELECT period_start, label, working_days
        FROM reporting_periods
        WHERE period_start >= ?
        ORDER BY period_start
        LIMIT 36
    """, (rtc["start_date"],)).fetchall()

    conn.close()

    # Build person-keyed allocation structure
    people = {}
    for row in alloc_rows:
        pid = row["horizon_person_number"]
        if pid not in people:
            people[pid] = {
                "horizon_person_number": pid,
                "name":         row["name"],
                "job_title":    row["job_title"],
                "job_function": row["job_function"],
                "allocations":  {}
            }
        people[pid]["allocations"][row["period_start"]] = row["days"]

    return jsonify({
        "rtc":     dict(rtc),
        "periods": [dict(p) for p in periods],
        "staff":   list(people.values()),
    })


@app.route("/api/rtcs/<int:rtc_id>", methods=["PATCH"])
def api_update_rtc(rtc_id):
    """
    Updates RTC allocations and/or project details.
    Accepts partial updates — only provided fields are changed.

    Body may contain:
      allocations: [{horizon_person_number, period_start, days}, ...]
      project_number, task_order_number (triggers re-linking to projects table)
      start_date, department
    """
    data = request.get_json(silent=True, force=True)
    if not data:
        return jsonify({"error": "No JSON body"}), 400

    user = get_current_user()
    now  = datetime.now(timezone.utc).isoformat()
    conn = database.get_connection()
    c    = conn.cursor()

    rtc = c.execute(
        "SELECT rtc_id FROM rtcs WHERE rtc_id = ?", (rtc_id,)
    ).fetchone()
    if not rtc:
        conn.close()
        return jsonify({"error": f"RTC {rtc_id} not found"}), 404

    # Update scalar fields if provided
    updates = {}
    for field in ["start_date", "department"]:
        if field in data:
            updates[field] = data[field]

    if "project_number" in data and "task_order_number" in data:
        project_id = _get_or_create_project(c, data, now)
        updates["project_id"] = project_id

    if updates:
        updates["last_updated_by"] = user
        updates["last_updated_at"] = now
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        c.execute(f"UPDATE rtcs SET {set_clause} WHERE rtc_id = ?",
                  list(updates.values()) + [rtc_id])

    # Upsert allocations
    alloc_count = 0
    for alloc in data.get("allocations", []):
        pid    = str(alloc.get("horizon_person_number", "")).strip()
        period = alloc.get("period_start")
        days   = alloc.get("days", 0)
        if not pid or not period:
            continue
        c.execute("""
            INSERT INTO allocations
                (horizon_person_number, rtc_id, period_start, days, last_updated)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(horizon_person_number, rtc_id, period_start)
            DO UPDATE SET days = excluded.days, last_updated = excluded.last_updated
        """, (pid, rtc_id, period, days, now))
        alloc_count += 1

    # Update last_updated_by on the RTC itself
    c.execute("""
        UPDATE rtcs SET last_updated_by = ?, last_updated_at = ?
        WHERE rtc_id = ?
    """, (user, now, rtc_id))

    conn.commit()
    conn.close()

    threading.Thread(target=summary_module.build, daemon=True).start()
    return jsonify({"status": "ok", "allocations_updated": alloc_count})


@app.route("/api/rtcs/<int:rtc_id>/staff", methods=["POST"])
def api_add_rtc_staff(rtc_id):
    """
    Adds a staff member to an RTC (creates zero-allocation rows
    for the RTC's period range so they appear in the grid).
    """
    data = request.get_json(silent=True, force=True)
    if not data or "horizon_person_number" not in data:
        return jsonify({"error": "horizon_person_number required"}), 400

    pid  = str(data["horizon_person_number"]).strip()
    user = get_current_user()
    now  = datetime.now(timezone.utc).isoformat()
    conn = database.get_connection()
    c    = conn.cursor()

    rtc = c.execute(
        "SELECT rtc_id, start_date FROM rtcs WHERE rtc_id = ?", (rtc_id,)
    ).fetchone()
    if not rtc:
        conn.close()
        return jsonify({"error": f"RTC {rtc_id} not found"}), 404

    # Confirm person exists in staff
    if not c.execute(
        "SELECT 1 FROM staff WHERE horizon_person_number = ?", (pid,)
    ).fetchone():
        conn.close()
        return jsonify({"error": f"Staff member {pid} not found"}), 404

    # Get the periods for this RTC
    periods = c.execute("""
        SELECT period_start FROM reporting_periods
        WHERE period_start >= ?
        ORDER BY period_start LIMIT 36
    """, (rtc["start_date"],)).fetchall()

    added = 0
    for p in periods:
        try:
            c.execute("""
                INSERT OR IGNORE INTO allocations
                    (horizon_person_number, rtc_id, period_start, days, last_updated)
                VALUES (?, ?, ?, 0, ?)
            """, (pid, rtc_id, p["period_start"], now))
            added += c.rowcount
        except Exception:
            pass

    c.execute("""
        UPDATE rtcs SET last_updated_by = ?, last_updated_at = ?
        WHERE rtc_id = ?
    """, (user, now, rtc_id))

    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "periods_added": added})


@app.route("/api/rtcs/<int:rtc_id>/staff/<person_id>", methods=["DELETE"])
def api_remove_rtc_staff(rtc_id, person_id):
    """Removes a staff member from an RTC (deletes all their allocation rows)."""
    user = get_current_user()
    now  = datetime.now(timezone.utc).isoformat()
    conn = database.get_connection()
    c    = conn.cursor()

    c.execute("""
        DELETE FROM allocations
        WHERE rtc_id = ? AND horizon_person_number = ?
    """, (rtc_id, person_id))
    deleted = c.rowcount

    c.execute("""
        UPDATE rtcs SET last_updated_by = ?, last_updated_at = ?
        WHERE rtc_id = ?
    """, (user, now, rtc_id))

    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "rows_deleted": deleted})


@app.route("/api/rtcs/<int:rtc_id>/check-horizon")
def api_check_horizon(rtc_id):
    """
    Silently checks whether a placeholder RTC now has a matching PAR record.
    Called when the detail panel opens. Returns is_placeholder and a match
    if one is found, so the frontend can offer to link them.
    """
    conn = database.get_connection()
    rtc  = conn.execute("""
        SELECT r.rtc_id, p.project_status, p.project_name, p.project_id
        FROM rtcs r
        JOIN projects p ON p.project_id = r.project_id
        WHERE r.rtc_id = ?
    """, (rtc_id,)).fetchone()

    if not rtc:
        conn.close()
        return jsonify({"error": "Not found"}), 404

    if rtc["project_status"] != "Placeholder":
        conn.close()
        return jsonify({"is_placeholder": False, "match": None})

    # Look for a real PAR record with a similar project name
    stored_name = (rtc["project_name"] or "").strip()
    match = None
    if stored_name and stored_name != "Placeholder \u2014 awaiting Horizon record":
        row = conn.execute("""
            SELECT project_id, project_number, task_order_number,
                   project_name, task_name, project_manager, project_director
            FROM projects
            WHERE project_status = 'Active'
            AND LOWER(project_name) LIKE LOWER(?)
            LIMIT 1
        """, (f"%{stored_name[:30]}%",)).fetchone()
        if row:
            match = dict(row)

    conn.close()
    return jsonify({"is_placeholder": True, "match": match})


@app.route("/api/rtcs/<int:rtc_id>/link-horizon", methods=["POST"])
def api_link_horizon(rtc_id):
    """
    Links a placeholder RTC to a confirmed real Horizon project.
    Re-points the RTC's project_id and cleans up the placeholder row.
    """
    data = request.get_json(silent=True, force=True)
    if not data:
        return jsonify({"error": "No JSON body"}), 400

    proj_num   = data.get("project_number", "").strip()
    task_order = data.get("task_order_number", "").strip()
    if not proj_num or not task_order:
        return jsonify({"error": "project_number and task_order_number required"}), 400

    conn = database.get_connection()
    c    = conn.cursor()

    real_project = c.execute("""
        SELECT project_id FROM projects
        WHERE project_number = ? AND task_order_number = ?
        AND project_status != 'Placeholder'
    """, (proj_num, task_order)).fetchone()

    if not real_project:
        conn.close()
        return jsonify({"error": "Project not found in PAR data"}), 404

    rtc = c.execute(
        "SELECT project_id FROM rtcs WHERE rtc_id = ?", (rtc_id,)
    ).fetchone()
    if not rtc:
        conn.close()
        return jsonify({"error": "RTC not found"}), 404

    old_project_id = rtc["project_id"]
    now  = datetime.now(timezone.utc).isoformat()
    user = get_current_user()

    c.execute("""
        UPDATE rtcs SET project_id = ?, last_updated_by = ?, last_updated_at = ?
        WHERE rtc_id = ?
    """, (real_project["project_id"], user, now, rtc_id))

    # Clean up orphaned placeholder project row
    other_refs = c.execute(
        "SELECT COUNT(*) FROM rtcs WHERE project_id = ?", (old_project_id,)
    ).fetchone()[0]
    if other_refs == 0:
        c.execute("DELETE FROM projects WHERE project_id = ?", (old_project_id,))

    conn.commit()
    conn.close()
    threading.Thread(target=summary_module.build, daemon=True).start()
    return jsonify({"status": "ok", "project_id": real_project["project_id"]})


# ---------------------------------------------------------------------------
# ADMIN ROUTES
# ---------------------------------------------------------------------------

@app.route("/admin")
def admin_index():
    return render_template("admin.html")


@app.route("/admin/import/staff", methods=["POST"])
@require_admin
def admin_import_staff():
    from pathlib import Path
    path = (request.json or {}).get("file_path") or str(config.STAFF_LIST_PATH)
    if not path or not Path(path).exists():
        return jsonify({"error": f"File not found: {path}"}), 400
    result = staff_import.run(path)
    summary_module.build()
    return jsonify(result)


@app.route("/admin/import/par", methods=["POST"])
@require_admin
def admin_import_par():
    from pathlib import Path
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


@app.route("/admin/rebuild-summary", methods=["POST"])
@require_admin
def admin_rebuild_summary():
    summary_module.build()
    return jsonify({"status": "ok",
                    "rebuilt_at": datetime.now(timezone.utc).isoformat()})


@app.route("/admin/run-cleanup", methods=["POST"])
@require_admin
def admin_run_cleanup():
    """
    Archives RTCs that have no future allocations and haven't been
    opened in 30+ days. Data is preserved — RTCs are never deleted,
    just hidden from the default list view.
    """
    now         = datetime.now(timezone.utc)
    today       = now.date().isoformat()
    cutoff      = (now.date() - timedelta(days=30)).isoformat()

    conn = database.get_connection()
    c    = conn.cursor()

    # Find eligible RTCs: no future days AND last_opened before cutoff
    eligible = c.execute("""
        SELECT r.rtc_id, p.project_number, p.project_name,
               r.last_opened, r.department
        FROM rtcs r
        JOIN projects p ON p.project_id = r.project_id
        WHERE r.is_archived = 0
        AND (r.last_opened IS NULL OR r.last_opened < ?)
        AND COALESCE((
            SELECT SUM(a.days)
            FROM allocations a
            WHERE a.rtc_id = r.rtc_id AND a.period_start > ?
        ), 0) = 0
    """, (cutoff, today)).fetchall()

    archived = []
    for row in eligible:
        c.execute(
            "UPDATE rtcs SET is_archived = 1 WHERE rtc_id = ?",
            (row["rtc_id"],)
        )
        archived.append({
            "rtc_id":        row["rtc_id"],
            "project_number": row["project_number"],
            "project_name":   row["project_name"],
            "last_opened":    row["last_opened"],
        })

    conn.commit()
    conn.close()

    if archived:
        summary_module.build()

    return jsonify({
        "archived_count": len(archived),
        "archived":       archived,
    })


@app.route("/admin/config")
@require_admin
def admin_config():
    """Returns non-sensitive config summary for diagnostics."""
    from pathlib import Path
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


def _get_or_create_project(cursor, data: dict, now: str) -> int:
    """
    Looks up a project by project_number + task_order_number.

    Real project numbers: find or create a shared PAR row.
    Placeholder numbers (00000000, 12345678, etc.): always create a NEW
    unique project row per RTC, so two RTCs using the same placeholder
    never collide or share data. Keyed by a timestamp suffix.
    """
    proj_num   = data.get("project_number", "").strip()
    task_order = data.get("task_order_number", "").strip()

    # Real project number — look up the shared PAR row
    if not _is_placeholder(proj_num) and task_order:
        row = cursor.execute("""
            SELECT project_id FROM projects
            WHERE project_number = ? AND task_order_number = ?
        """, (proj_num, task_order)).fetchone()
        if row:
            return row["project_id"]

        # Not in DB yet — create a shared pending row the PAR import will enrich
        cursor.execute("""
            INSERT INTO projects (
                project_number, task_order_number,
                project_name, task_name, project_status, last_imported
            ) VALUES (?,?,?,?,?,?)
            ON CONFLICT(project_number, task_order_number) DO UPDATE SET
                last_imported = excluded.last_imported
        """, (
            proj_num, task_order,
            data.get("project_name", "No Horizon Record Found"),
            data.get("task_name",    "No Horizon Record Found"),
            "Pending", now
        ))
        row = cursor.execute("""
            SELECT project_id FROM projects
            WHERE project_number = ? AND task_order_number = ?
        """, (proj_num, task_order)).fetchone()
        return row["project_id"]

    # Placeholder number — create a unique row so RTCs never share placeholder data
    suffix       = now.replace(":", "").replace("-", "").replace(".", "")[:20]
    unique_proj  = f"{proj_num or 'PLACEHOLDER'}_{suffix}"
    unique_task  = f"{task_order or '000'}_{suffix}"

    cursor.execute("""
        INSERT INTO projects (
            project_number, task_order_number,
            project_name, task_name,
            project_customer,
            project_director, project_manager,
            project_status, last_imported
        ) VALUES (?,?,?,?,?,?,?,?,?)
    """, (
        unique_proj, unique_task,
        data.get("project_name",     "Placeholder \u2014 awaiting Horizon record"),
        data.get("task_name",        ""),
        data.get("project_customer", None),
        data.get("project_director", None),
        data.get("project_manager",  None),
        "Placeholder", now
    ))
    row = cursor.execute("""
        SELECT project_id FROM projects
        WHERE project_number = ? AND task_order_number = ?
    """, (unique_proj, unique_task)).fetchone()
    return row["project_id"]


# ---------------------------------------------------------------------------
# STARTUP
# ---------------------------------------------------------------------------

def create_app():
    database.initialise_database()
    summary_module.build()

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
