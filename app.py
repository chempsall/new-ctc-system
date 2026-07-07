"""
app.py
Resource Forecast — Flask application entry point.

All configuration comes from config.py (which reads from .env and
environment variables). Nothing is hardcoded here.

To start the development server:
    python app.py
"""

import json
import logging
import secrets
from datetime import datetime, timezone, timedelta
from functools import wraps
from logging.handlers import TimedRotatingFileHandler
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

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def _setup_logging():
    log_dir = Path(config.BASE_DIR) / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / "app.log"

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Rotating handler — new file each day, keep 28 days
    fh = TimedRotatingFileHandler(
        log_file, when="midnight", backupCount=28,
        encoding="utf-8", utc=True
    )
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)

    # Console handler for dev
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    ch.setLevel(logging.INFO)

    root = logging.getLogger("resource_forecast")
    root.setLevel(logging.DEBUG)
    root.addHandler(fh)
    root.addHandler(ch)
    return root

logger = _setup_logging()

from imports import staff_list as staff_import
from imports import par_import

app = Flask(__name__)
app.secret_key = config.SECRET_KEY


@app.errorhandler(404)
def handle_404(e):
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(Exception)
def handle_exception(e):
    """Return JSON for all unhandled exceptions."""
    import traceback
    logger.error(f"Unhandled exception: {e}\n{traceback.format_exc()}")
    if config.FLASK_DEBUG:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500
    return jsonify({"error": "An unexpected error occurred"}), 500



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

def _relink_pending_rtcs(conn=None):
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

        if not proj_num or _is_placeholder(proj_num):
            continue

        match = c.execute("""
            SELECT project_id FROM projects
            WHERE project_number = ? AND task_order_number = ?
            AND project_status = 'Active'
        """, (proj_num, task_num)).fetchone()

        if match:
            c.execute("""
                UPDATE rtcs SET project_id = ?, last_updated_at = ?,
                               auto_linked = 1
                WHERE rtc_id = ?
            """, (match["project_id"], now, rtc_id))
            linked += 1

    if linked:
        conn.commit()
        logger.info(f"Auto-relinked {linked} RTC(s) to Horizon")
    if close_after:
        conn.close()
    return linked


def _nightly_imports():
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

    relinked = _relink_pending_rtcs()
    if relinked:
        logger.info(f"Re-linked {relinked} pending RTC(s) to Horizon")

    summary_module.build()
    logger.info("Summary cache rebuilt")
    logger.info("Nightly import complete")


# ---------------------------------------------------------------------------
# DASHBOARD ROUTES
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/rtc/<int:rtc_id>")
def rtc_editor(rtc_id):
    """Serves the RTC editing screen."""
    return render_template("rtc.html", rtc_id=rtc_id)



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
    Called by the RTC modal when project details are entered.

    Returns one of three shapes:
      { "match_type": "full", ...fields }   — exact project+task match found
      { "match_type": "project_only", ...fields, "task_name": null }
                                            — project found, task order unknown
      {}                                    — project number not found at all
    """
    project_number    = request.args.get("project_number", "").strip()
    task_order_number = request.args.get("task_order_number", "").strip()

    if not project_number or not task_order_number:
        return jsonify({})

    conn = database.get_connection()

    # Try exact match first
    row = conn.execute("""
        SELECT project_number, task_order_number, project_name, task_name,
               project_organisation, project_customer, project_director,
               project_manager, project_status, project_type,
               task_start_date, task_end_date
        FROM projects
        WHERE project_number = ? AND task_order_number = ?
        AND project_status NOT IN ('Placeholder', 'Pending')
    """, (project_number, task_order_number)).fetchone()

    if row:
        conn.close()
        result = dict(row)
        result["match_type"] = "full"
        return jsonify(result)

    # Try project-number-only match — known project, unknown task order
    row = conn.execute("""
        SELECT project_number, project_name,
               project_organisation, project_customer, project_director,
               project_manager, project_status, project_type
        FROM projects
        WHERE project_number = ?
        AND project_status NOT IN ('Placeholder', 'Pending')
        ORDER BY last_imported DESC
        LIMIT 1
    """, (project_number,)).fetchone()

    conn.close()

    if row:
        result = dict(row)
        result["match_type"]       = "project_only"
        result["task_order_number"] = task_order_number
        result["task_name"]        = None   # unknown — must be entered manually
        result["task_start_date"]  = None
        result["task_end_date"]    = None
        return jsonify(result)

    return jsonify({})


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
            p.project_customer,
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
                AND a.period_start >= ?
            ), 0) AS future_days
        FROM rtcs r
        JOIN projects p ON p.project_id = r.project_id
        WHERE 1=1
        AND (? = '1' OR r.is_archived = 0)
    """, (current_period, current_period, archived)).fetchall()

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
            if q not in (row["project_number"]   or "").lower() and \
               q not in (row["project_name"]     or "").lower() and \
               q not in (row["task_name"]        or "").lower() and \
               q not in (row["project_customer"] or "").lower() and \
               q not in (row["project_director"] or "").lower() and \
               q not in (row["project_manager"]  or "").lower() and \
               q not in (row["department"]       or "").lower():
                continue

        # Compute status
        last_opened = row["last_opened"]
        future_days = row["future_days"]
        if row["is_archived"]:
            status = "archived"
        elif future_days == 0:
            status = "awaiting_archiving"
        else:
            # Has future allocations — check review recency
            grace_cutoff = (now.date() - timedelta(days=7)).isoformat()
            month_start  = now.date().replace(day=1).isoformat()
            if last_opened and (last_opened[:10] >= month_start or last_opened[:10] >= grace_cutoff):
                status = "current"
            elif last_opened and last_opened[:10] >= thirty_days_ago:
                status = "due_review"
            else:
                status = "overdue_review"

        row["status"] = status
        result.append(row)

    # Sort: current_month_days descending, then project_name ascending
    result.sort(key=lambda r: (-r["future_days"], r["project_name"] or ""))
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

    summary_module.mark_dirty()
    logger.info(f"RTC {rtc_id}: staff {pid} added by {user}")
    return jsonify({"status": "ok", "periods_seeded": len(periods)})


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

    summary_module.mark_dirty()
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

# Fetch all allocations for this RTC
    alloc_rows = c.execute("""
        SELECT a.horizon_person_number, a.period_start, a.days,
               s.name, s.job_title, s.job_function, s.availability
        FROM allocations a
        JOIN staff s ON s.horizon_person_number = a.horizon_person_number
        WHERE a.rtc_id = ?
        ORDER BY s.name, a.period_start
    """, (rtc_id,)).fetchall()

    # Fetch reporting periods actually in use for this RTC
    # Based on the max period_start in allocations, minimum 12 months
    max_period = c.execute("""
        SELECT MAX(period_start) FROM allocations WHERE rtc_id = ?
    """, (rtc_id,)).fetchone()[0]

    if max_period:
        periods = c.execute("""
            SELECT period_start, label, working_days
            FROM reporting_periods
            WHERE period_start >= ? AND period_start <= ?
            ORDER BY period_start
        """, (rtc["start_date"], max_period)).fetchall()
    else:
        periods = c.execute("""
            SELECT period_start, label, working_days
            FROM reporting_periods
            WHERE period_start >= ?
            ORDER BY period_start LIMIT 12
        """, (rtc["start_date"],)).fetchall()

    period_starts = [p["period_start"] for p in periods]


    conn.close()

    # Build person-keyed structure with allocations
    people = {}
    for row in alloc_rows:
        pid = row["horizon_person_number"]
        if pid not in people:
            people[pid] = {
                "horizon_person_number": pid,
                "name":         row["name"],
                "job_title":    row["job_title"],
                "job_function": row["job_function"],
                "allocations":  {},
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
    
    # Update editable project fields on Placeholder/Pending rows only
    PROJECT_EDITABLE = ["project_name", "task_name", "project_customer",
                        "project_director", "project_manager"]
    proj_updates = {k: data[k] for k in PROJECT_EDITABLE if k in data}
    if proj_updates:
        proj = c.execute("""
            SELECT p.project_id, p.project_status
            FROM rtcs r
            JOIN projects p ON p.project_id = r.project_id
            WHERE r.rtc_id = ?
        """, (rtc_id,)).fetchone()
        if proj and proj["project_status"] in ("Placeholder", "Pending"):
            set_clause = ", ".join(f"{k} = ?" for k in proj_updates)
            c.execute(f"UPDATE projects SET {set_clause} WHERE project_id = ?",
                      list(proj_updates.values()) + [proj["project_id"]])

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

        # Validate days — must be a non-negative number
        try:
            days = float(days)
        except (TypeError, ValueError):
            continue
        if days < 0:
            days = 0

        # Validate period_start exists in reporting_periods
        valid_period = c.execute(
            "SELECT 1 FROM reporting_periods WHERE period_start = ?", (period,)
        ).fetchone()
        if not valid_period:
            continue

        # Guard: only update allocations for staff who are members of this RTC.
        # Prevents a removed person being silently reinstated via a PATCH.
        is_member = c.execute("""
            SELECT 1 FROM allocations
            WHERE rtc_id = ? AND horizon_person_number = ?
            LIMIT 1
        """, (rtc_id, pid)).fetchone()
        if not is_member:
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

    summary_module.mark_dirty()
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

    # For generics, allow multiple instances by creating a unique suffixed ID
    if pid.startswith('GENERIC-'):
        existing_count = c.execute("""
            SELECT COUNT(DISTINCT horizon_person_number) FROM allocations
            WHERE rtc_id = ? AND horizon_person_number LIKE ?
        """, (rtc_id, pid + '%')).fetchone()[0]
        if existing_count > 0:
            pid = f"{pid}_{existing_count + 1}"
            original_pid = str(data["horizon_person_number"]).strip()
            orig = c.execute(
                "SELECT * FROM staff WHERE horizon_person_number = ?", (original_pid,)
            ).fetchone()
            if orig:
                c.execute("""
                    INSERT OR IGNORE INTO staff
                        (horizon_person_number, name, job_title, job_family,
                         job_function, department, availability, last_imported)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'seeded')
                """, (pid, orig["name"], orig["job_title"], orig["job_family"],
                      orig["job_function"], orig["department"], orig["availability"]))

    # Get the periods already in use for this RTC
    # (match existing staff's allocation range, minimum 12)
    existing_end = c.execute("""
        SELECT MAX(period_start) FROM allocations WHERE rtc_id = ?
    """, (rtc_id,)).fetchone()[0]

    if existing_end:
        periods = c.execute("""
            SELECT period_start FROM reporting_periods
            WHERE period_start >= ? AND period_start <= ?
            ORDER BY period_start
        """, (rtc["start_date"], existing_end)).fetchall()
    else:
        periods = c.execute("""
            SELECT period_start FROM reporting_periods
            WHERE period_start >= ?
            ORDER BY period_start LIMIT 12
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
        except Exception as e:
            app.logger.error(f"Failed to insert allocation for {pid}: {e}")

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


@app.route("/api/rtcs/<int:rtc_id>/staff/<person_id>/replace", methods=["POST"])
def api_replace_rtc_staff(rtc_id, person_id):
    """
    Replaces a staff member (typically a generic placeholder) with a real person.
    Copies the allocation values from the old person to the new one,
    then deletes the old person's rows.
    Body: { "new_horizon_person_number": "..." }
    """
    data = request.get_json(silent=True, force=True)
    if not data or "new_horizon_person_number" not in data:
        return jsonify({"error": "new_horizon_person_number required"}), 400

    new_pid = str(data["new_horizon_person_number"]).strip()
    user    = get_current_user()
    now     = datetime.now(timezone.utc).isoformat()
    conn    = database.get_connection()
    c       = conn.cursor()

    # Confirm new person exists in staff
    if not c.execute(
        "SELECT 1 FROM staff WHERE horizon_person_number = ?", (new_pid,)
    ).fetchone():
        conn.close()
        return jsonify({"error": f"Staff member {new_pid} not found"}), 404

    # Get the existing allocations for the old person on this RTC
    old_allocs = c.execute("""
        SELECT period_start, days FROM allocations
        WHERE rtc_id = ? AND horizon_person_number = ?
    """, (rtc_id, person_id)).fetchall()

    if not old_allocs:
        conn.close()
        return jsonify({"error": "Person not found on this RTC"}), 404

    # Copy allocations to new person
    for row in old_allocs:
        c.execute("""
            INSERT INTO allocations
                (horizon_person_number, rtc_id, period_start, days, last_updated)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(horizon_person_number, rtc_id, period_start)
            DO UPDATE SET days = excluded.days, last_updated = excluded.last_updated
        """, (new_pid, rtc_id, row["period_start"], row["days"], now))

    # Delete old person's rows
    c.execute(
        "DELETE FROM allocations WHERE rtc_id = ? AND horizon_person_number = ?",
        (rtc_id, person_id)
    )

    c.execute("""
        UPDATE rtcs SET last_updated_by = ?, last_updated_at = ?
        WHERE rtc_id = ?
    """, (user, now, rtc_id))

    conn.commit()
    conn.close()
    summary_module.mark_dirty()
    return jsonify({"status": "ok", "replaced": person_id, "with": new_pid})


@app.route("/api/rtcs/<int:rtc_id>/extend", methods=["POST"])
def api_extend_rtc(rtc_id):
    """
    Extends an RTC by 12 more months.
    Adds zero-allocation rows for all current staff for the next 12 periods
    beyond the current last period.
    """
    user = get_current_user()
    now  = datetime.now(timezone.utc).isoformat()
    conn = database.get_connection()
    c    = conn.cursor()

    # Get current staff on this RTC
    staff_rows = c.execute("""
        SELECT DISTINCT horizon_person_number FROM allocations
        WHERE rtc_id = ?
    """, (rtc_id,)).fetchall()
    if not staff_rows:
        conn.close()
        return jsonify({"error": "No staff on this RTC"}), 400

    # Find the current last period
    last_period = c.execute("""
        SELECT MAX(period_start) FROM allocations WHERE rtc_id = ?
    """, (rtc_id,)).fetchone()[0]
    if not last_period:
        conn.close()
        return jsonify({"error": "No existing periods found"}), 400

    # Get the next 12 periods after the current last one
    new_periods = c.execute("""
        SELECT period_start FROM reporting_periods
        WHERE period_start > ?
        ORDER BY period_start LIMIT 12
    """, (last_period,)).fetchall()
    if not new_periods:
        conn.close()
        return jsonify({"error": "No further reporting periods available"}), 400

    # Insert zero-allocation rows for all staff for all new periods
    added = 0
    for person in staff_rows:
        pid = person["horizon_person_number"]
        for p in new_periods:
            c.execute("""
                INSERT OR IGNORE INTO allocations
                    (horizon_person_number, rtc_id, period_start, days, last_updated)
                VALUES (?, ?, ?, 0, ?)
            """, (pid, rtc_id, p["period_start"], now))
            added += c.rowcount

    c.execute("""
        UPDATE rtcs SET last_updated_by = ?, last_updated_at = ?
        WHERE rtc_id = ?
    """, (user, now, rtc_id))

    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "periods_added": len(new_periods), "rows_added": added})

@app.route("/api/rtcs/<int:rtc_id>/opened", methods=["POST"])
def api_rtc_opened(rtc_id):
    """Records that a user has opened this RTC for editing."""
    user = get_current_user()
    now  = datetime.now(timezone.utc).isoformat()
    conn = database.get_connection()
    conn.execute("""
        UPDATE rtcs SET last_opened_by = ?, last_opened = ?
        WHERE rtc_id = ?
    """, (user, now, rtc_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

@app.route("/api/rtcs/<int:rtc_id>/clear-auto-link", methods=["POST"])
def api_clear_auto_link(rtc_id):
    """Clears the auto_linked flag after user has confirmed the link."""
    conn = database.get_connection()
    conn.execute("UPDATE rtcs SET auto_linked = 0 WHERE rtc_id = ?", (rtc_id,))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


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

    auto_linked = bool(rtc["auto_linked"]) if "auto_linked" in rtc.keys() else False
    if rtc["project_status"] not in ("Placeholder", "Pending"):
        conn.close()
        return jsonify({"is_placeholder": False, "match": None, "auto_linked": auto_linked})

    # Look for a real PAR record with a similar project name
    match = None
    
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
    summary_module.mark_dirty()
    return jsonify({"status": "ok", "project_id": real_project["project_id"]})


# ---------------------------------------------------------------------------
# ADMIN ROUTES
# ---------------------------------------------------------------------------

@app.route("/admin/rtcs/<int:rtc_id>", methods=["DELETE"])
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


@app.route("/admin")
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
    logger.info(f"Admin: staff import completed — {result.get('rows_processed',0)} rows processed, "
                f"{result.get('rows_inserted',0)} inserted, {result.get('rows_updated',0)} updated")
    return jsonify(result)


@app.route("/admin/import/par", methods=["POST"])
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


@app.route("/admin/relink-pending", methods=["POST"])
@require_admin
def admin_relink_pending():
    """Manually triggers re-linking of pending RTCs to Horizon."""
    linked = _relink_pending_rtcs()
    summary_module.mark_dirty()
    logger.info(f"Admin: re-link pending RTCs — {linked} linked")
    return jsonify({"status": "ok", "linked": linked})


@app.route("/admin/rebuild-summary", methods=["POST"])
@require_admin
def admin_rebuild_summary():
    summary_module.build()
    logger.info("Admin: summary cache rebuilt manually")
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
            "rtc_id":        row["rtc_id"],
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


@app.route("/admin/log")
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

        # Not in DB yet — always make task order unique to prevent collisions
        # between two RTCs using the same project number and unrecognised task order.
        # The PAR import will create the real row when it appears.
        suffix     = now.replace(":", "").replace("-", "").replace(".", "")[:20]
        task_order = f"{task_order}_{suffix}"

        cursor.execute("""
            INSERT INTO projects (
                project_number, task_order_number,
                project_name, task_name,
                project_customer, project_director, project_manager,
                project_status, last_imported
            ) VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(project_number, task_order_number) DO UPDATE SET
                last_imported = excluded.last_imported
        """, (
            proj_num, task_order,
            data.get("project_name", "No Horizon Record Found"),
            data.get("task_name",    "No Horizon Record Found"),
            data.get("project_customer", None),
            data.get("project_director", None),
            data.get("project_manager", None),
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
    summary_module.start_worker()

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
