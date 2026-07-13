"""
routes/dashboard.py
Page routes and the read-only data endpoints the dashboard uses:
summary, staff, project lookup.
"""

import json
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request, render_template, Response

import database
import summary as summary_module

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/")
def index():
    return render_template("index.html")


@dashboard_bp.route("/rtc/<int:rtc_id>")
def rtc_editor(rtc_id):
    """Serves the RTC editing screen."""
    return render_template("rtc.html", rtc_id=rtc_id)


@dashboard_bp.route("/api/summary")
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

    etag = cached["generated_at"]
    if request.headers.get("If-None-Match") == etag:
        return "", 304
    response = Response(payload, mimetype="application/json")
    response.headers["X-Generated-At"] = etag
    response.headers["ETag"] = etag
    return response


# NOTE: /api/offices, /api/departments, /api/teams and /api/job-functions
# have no remaining callers in the frontend (the dashboard builds these
# lists from the summary payload). They are retained here unchanged for
# one release in case anything external calls them; see the review notes
# for the recommendation to delete them.

@dashboard_bp.route("/api/offices")
@dashboard_bp.route("/api/departments")
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


@dashboard_bp.route("/api/teams")
@dashboard_bp.route("/api/job-functions")
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


@dashboard_bp.route("/api/staff")
def api_staff():
    """Staff list for RTC staff picker."""
    department = request.args.get("office") or request.args.get("department")
    today = datetime.now(timezone.utc).date().isoformat()
    conn = database.get_connection()
    if department:
        rows = conn.execute("""
            SELECT horizon_person_number, name, job_title,
                   job_function, department
            FROM staff
            WHERE department = ? AND (end_date IS NULL OR end_date > ?)
            AND horizon_person_number NOT GLOB 'GENERIC-*_*'
            ORDER BY name
        """, (department, today)).fetchall()
    else:
        rows = conn.execute("""
            SELECT horizon_person_number, name, job_title,
                   job_function, department
            FROM staff
            WHERE (end_date IS NULL OR end_date > ?)
            AND horizon_person_number NOT GLOB 'GENERIC-*_*'
            ORDER BY name
        """, (today,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@dashboard_bp.route("/api/project")
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
        result["match_type"]        = "project_only"
        result["task_order_number"] = task_order_number
        result["task_name"]         = None   # unknown — must be entered manually
        result["task_start_date"]   = None
        result["task_end_date"]     = None
        return jsonify(result)

    return jsonify({})
