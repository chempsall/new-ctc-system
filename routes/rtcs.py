"""
routes/rtcs.py
All /api/rtcs endpoints: list, create, duplicate, detail, update,
staff management, extend, opened-tracking, and Horizon linking.
"""

import logging
from datetime import datetime, timezone, timedelta, date

from dateutil.relativedelta import relativedelta
from flask import Blueprint, jsonify, request

import database
import summary as summary_module
from services import audit
from services.identity import get_current_user


def _leaver_locked_from(end_date):
    """
    First period a leaver may no longer be booked into: the month after the one
    they left. The leaving month itself stays editable because they worked part
    of it, and earlier months are historical fact. Returns None for current
    staff (no end date, or one still in the future).
    """
    if not end_date:
        return None
    try:
        ed = datetime.strptime(str(end_date)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    if ed >= date.today():
        return None
    return (ed.replace(day=1) + relativedelta(months=1)).isoformat()


def _names(value):
    """Lower-cased individual names from a ";"-separated PAR person field."""
    return [n.strip().lower() for n in (value or "").split(";") if n.strip()]
from services.projects import get_or_create_project
from services.special_rtcs import SPECIAL_PROJECT_NUMBERS
from services.projects import display_number, is_suffixed, is_placeholder

logger = logging.getLogger("resource_forecast.rtcs")

rtcs_bp = Blueprint("rtcs", __name__)


@rtcs_bp.route("/api/rtcs")
def api_rtcs():
    """
    Returns the list of RTCs for the front page.

    Query params:
      department  — filter by cost centre
      pm          — filter by project manager (partial match)
      pd          — filter by project director (partial match)
      search      — free text across project number and name
      archived    — "1" to include archived RTCs (default: exclude)

    Sorted by future days descending, then project name.
    """
    conn = database.get_connection()
    now  = datetime.now(timezone.utc)
    current_period  = now.date().replace(day=1).isoformat()
    period_label = request.args.get("period", "").strip()
    _MONTHS = {"Jan":1,"Feb":2,"Mar":3,"Apr":4,"May":5,"Jun":6,
               "Jul":7,"Aug":8,"Sep":9,"Oct":10,"Nov":11,"Dec":12}
    if period_label:
        try:
            mon, yr = period_label.split("-")
            selected_period = date(int(yr), _MONTHS[mon], 1).isoformat()
        except (ValueError, KeyError):
            logger.warning(f"api_rtcs: unparseable period label {period_label!r} — using current month")
            selected_period = current_period
    else:
        selected_period = current_period
    next_period     = (now.date().replace(day=1) + relativedelta(months=1)).isoformat()
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
            r.last_edited_by,
            r.last_edited_at,
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
            p.project_type,
            COALESCE((
                SELECT SUM(a.days)
                FROM allocations a
                WHERE a.rtc_id = r.rtc_id
                AND a.period_start = %s
            ), 0) AS current_month_days,
            COALESCE((
                SELECT SUM(a.days)
                FROM allocations a
                WHERE a.rtc_id = r.rtc_id
                AND a.period_start >= %s
            ), 0) AS future_days
        FROM rtcs r
        JOIN projects p ON p.project_id = r.project_id
        WHERE 1=1
        AND (%s = '1' OR r.is_archived = 0)
    """, (selected_period, next_period, archived)).fetchall()

    conn.close()

    # Apply filters in Python (simpler than building dynamic SQL)
    result = []
    for r in rows:
        row = dict(r)
        if dept   and row["department"] != dept:           continue
        # A PAR project can name several directors or managers, separated by
        # ";". Compare against each name in turn rather than as a substring of
        # the whole value, so "Smith, A" does not also match "Smith, Andrew".
        if pm and pm.lower() not in _names(row["project_manager"]):     continue
        if pd_arg and pd_arg.lower() not in _names(row["project_director"]): continue
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
        is_special = (row["project_number"] or "") in SPECIAL_PROJECT_NUMBERS
        if row["is_archived"]:
            status = "archived"
        elif is_special:
            status = "current"
        elif future_days == 0 and (row["current_month_days"] or 0) == 0:
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
        # Compute horizon_status from project_type
        ptype = (row["project_type"] or "").strip()
        pstat = (row["project_status"] or "").strip().lower()
        if pstat == "active" and ptype == "UK Direct":
            row["horizon_status"] = "linked"
        elif pstat == "active" and ptype == "UK Opportunity":
            row["horizon_status"] = "opportunity"
        elif pstat == "active":
            row["horizon_status"] = "other"
        else:
            row["horizon_status"] = "norecord"
        # Add server-side display fields (§4.4) so frontend never parses project numbers
        proj_num_val = row["project_number"] or ""
        task_num_val = row["task_order_number"] or ""
        row["display_project_number"] = display_number(proj_num_val)
        row["display_task_order"]     = display_number(task_num_val)
        row["is_placeholder_number"]  = (is_suffixed(proj_num_val) or
                                         is_placeholder(proj_num_val))
        result.append(row)

    # Sort: future_days descending, then project_name ascending
    result.sort(key=lambda r: (-r["future_days"], r["project_name"] or ""))
    return jsonify(result)


@rtcs_bp.route("/api/rtcs", methods=["POST"])
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

    # Normalise and reject blank/whitespace-only identifiers. The form trims,
    # but the API must not rely on that.
    data["project_number"]    = (data.get("project_number")    or "").strip()
    data["task_order_number"] = (data.get("task_order_number") or "").strip()
    if not data["project_number"] or not data["task_order_number"]:
        return jsonify({
            "error": "Project number and task order number cannot be blank."}), 400

    # Indirect/special RTCs (AL&PH, Training, Day Release) are created and
    # maintained by the nightly job, one per person. A hand-made one would sit
    # alongside them and be treated as a real project.
    if data["project_number"] in SPECIAL_PROJECT_NUMBERS:
        return jsonify({
            "error": (f"{data['project_number']} is an indirect RTC maintained "
                      "automatically by the system and cannot be created here.")}), 400

    # A forecast starting more than a year out is almost always a typo.
    try:
        _start = datetime.strptime(data["start_date"][:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return jsonify({"error": "Start date is not a valid date."}), 400
    if _start > date.today() + relativedelta(months=12):
        return jsonify({
            "error": "Start month cannot be more than 12 months in the future."}), 400

    user = get_current_user()
    now  = datetime.now(timezone.utc).isoformat()
    conn = database.get_connection()
    c    = conn.cursor()

    project_id = get_or_create_project(c, data, now)

    # One PAR project/task = one RTC, archived or not. Without this, two people
    # forecasting the same task would both succeed and the days would be counted
    # twice; and an archived RTC still holds that project's history, so a second
    # would split it. Adding allocations to an archived RTC reactivates it.
    clash = c.execute("""
        SELECT rtc_id, is_archived FROM rtcs
        WHERE project_id = %s
        ORDER BY is_archived, rtc_id
        LIMIT 1
    """, (project_id,)).fetchone()
    if clash:
        conn.rollback()
        conn.close()
        archived = bool(clash["is_archived"])
        return jsonify({
            "error": ("An archived RTC already exists for that project and task "
                      "order. Open it and add allocations to reactivate it."
                      if archived else
                      "An RTC already exists for that project and task order."),
            "existing_rtc_id": clash["rtc_id"],
            "existing_rtc_archived": archived,
        }), 409

    c.execute("""
        INSERT INTO rtcs (project_id, department, start_date,
                          created_by, created_at,
                          last_updated_by, last_updated_at,
                          last_opened_by, last_opened,
                          is_archived)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,0)
        RETURNING rtc_id
    """, (project_id, data["department"], data["start_date"],
          user, now, user, now, user, now))

    rtc_id = c.fetchone()["rtc_id"]
    audit.record(audit.RTC_CREATED, rtc_id=rtc_id,
                 detail=f"Start month {data['start_date'][:7]}", conn=conn)
    conn.commit()
    conn.close()

    summary_module.mark_dirty()
    # rtc_id restored to the response — the modal uses it to select the
    # newly created RTC (regression fix; see review §regressions).
    return jsonify({"status": "ok", "rtc_id": rtc_id}), 201


@rtcs_bp.route("/api/rtcs/<int:rtc_id>/duplicate", methods=["POST"])
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
        "SELECT rtc_id FROM rtcs WHERE rtc_id = %s", (rtc_id,)
    ).fetchone()
    if not source:
        conn.close()
        return jsonify({"error": f"RTC {rtc_id} not found"}), 404

    project_id = get_or_create_project(c, data, now)

    c.execute("""
        INSERT INTO rtcs (project_id, department, start_date,
                          created_by, created_at,
                          last_updated_by, last_updated_at,
                          last_opened_by, last_opened,
                          is_archived)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,0)
        RETURNING rtc_id
    """, (project_id, data["department"], data["start_date"],
          user, now, user, now, user, now))

    new_rtc_id = c.fetchone()["rtc_id"]

    # Copy the distinct set of people who appear in the source RTC,
    # but create NO allocation rows — they start from zero in the new RTC.
    staff_members = c.execute("""
        SELECT DISTINCT horizon_person_number
        FROM allocations
        WHERE rtc_id = %s
    """, (rtc_id,)).fetchall()

    # Insert zero-allocation rows for the new RTC's start month only,
    # so the staff appear in the editor ready to be allocated.
    for s in staff_members:
        c.execute("""
            INSERT INTO allocations
                (horizon_person_number, rtc_id, period_start, days, last_updated)
            VALUES (%s, %s, %s, 0, %s)
            ON CONFLICT DO NOTHING
        """, (s["horizon_person_number"], new_rtc_id, data["start_date"], now))

    conn.commit()
    conn.close()

    summary_module.mark_dirty()
    return jsonify({"rtc_id": new_rtc_id, "staff_copied": len(staff_members)}), 201


@rtcs_bp.route("/api/rtcs/<int:rtc_id>")
def api_get_rtc(rtc_id):
    """
    Returns full RTC detail including all allocations.
    (Read-only: last_opened is recorded via POST /api/rtcs/<id>/opened.)
    """
    conn = database.get_connection()
    c    = conn.cursor()

    rtc = c.execute("""
        SELECT r.*, p.project_number, p.task_order_number, p.project_name,
               p.task_name, p.project_organisation, p.project_customer,
               p.project_director, p.project_manager, p.project_status,
               p.project_type
        FROM rtcs r
        JOIN projects p ON p.project_id = r.project_id
        WHERE r.rtc_id = %s
    """, (rtc_id,)).fetchone()

    if not rtc:
        conn.close()
        return jsonify({"error": f"RTC {rtc_id} not found"}), 404

    # Fetch all allocations for this RTC
    alloc_rows = c.execute("""
        SELECT a.horizon_person_number, a.period_start, a.days,
               s.name, s.job_title, s.job_function, s.availability, s.end_date
        FROM allocations a
        JOIN staff s ON s.horizon_person_number = a.horizon_person_number
        WHERE a.rtc_id = %s
        ORDER BY s.name, a.period_start
    """, (rtc_id,)).fetchall()

    # Fetch reporting periods actually in use for this RTC
    # Based on the max period_start in allocations, minimum 12 months
    max_period = c.execute("""
        SELECT MAX(period_start) AS m FROM allocations WHERE rtc_id = %s
    """, (rtc_id,)).fetchone()["m"]

    if max_period:
        periods = c.execute("""
            SELECT period_start, label, working_days
            FROM reporting_periods
            WHERE period_start >= %s AND period_start <= %s
            ORDER BY period_start
        """, (rtc["start_date"], max_period)).fetchall()
    else:
        periods = c.execute("""
            SELECT period_start, label, working_days
            FROM reporting_periods
            WHERE period_start >= %s
            ORDER BY period_start LIMIT 12
        """, (rtc["start_date"],)).fetchall()

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
                # A leaver stays on the RTC for audit, but no further time may
                # be booked for them after the month they left.
                "end_date":     row["end_date"],
                "locked_from":  _leaver_locked_from(row["end_date"]),
                "allocations":  {},
            }
        people[pid]["allocations"][row["period_start"]] = row["days"]

    rtc_dict = dict(rtc)
    # Derive horizon_status so the editor can offer 'Convert to a Live Project'
    # on opportunities (same rule as the list endpoint).
    _ptype = (rtc["project_type"] or "").strip()
    _pstat = (rtc["project_status"] or "").strip().lower()
    if _pstat == "active" and _ptype == "UK Direct":
        rtc_dict["horizon_status"] = "linked"
    elif _pstat == "active" and _ptype == "UK Opportunity":
        rtc_dict["horizon_status"] = "opportunity"
    elif _pstat == "active":
        rtc_dict["horizon_status"] = "other"
    else:
        rtc_dict["horizon_status"] = "norecord"
    rtc_dict["display_project_number"] = display_number(rtc["project_number"] or "")
    rtc_dict["display_task_order"]     = display_number(rtc["task_order_number"] or "")
    rtc_dict["is_placeholder_number"]  = (is_suffixed(rtc["project_number"] or "") or
                                           is_placeholder(rtc["project_number"] or ""))
    return jsonify({
        "rtc":            rtc_dict,
        "periods":        [dict(p) for p in periods],
        "staff":          list(people.values()),
        "server_period":  datetime.now(timezone.utc).date().replace(day=1).isoformat(),
    })


@rtcs_bp.route("/api/rtcs/<int:rtc_id>", methods=["PATCH", "POST"])
def api_update_rtc(rtc_id):
    """
    Updates RTC allocations and/or project details.
    Accepts partial updates — only provided fields are changed.

    POST is accepted as an alias for PATCH solely so that
    navigator.sendBeacon (which can only POST) can flush unsaved
    cells on page unload.

    Body may contain:
      allocations: [{horizon_person_number, period_start, days}, ...]
      project_number, task_order_number (triggers re-linking to projects table)
      start_date, department, notes
    """
    data = request.get_json(silent=True, force=True)
    if not data:
        return jsonify({"error": "No JSON body"}), 400

    rejected_leavers = []
    alloc_changes    = []
    user = get_current_user()
    now  = datetime.now(timezone.utc).isoformat()
    conn = database.get_connection()
    c    = conn.cursor()

    rtc = c.execute(
        "SELECT rtc_id FROM rtcs WHERE rtc_id = %s", (rtc_id,)
    ).fetchone()
    if not rtc:
        conn.close()
        return jsonify({"error": f"RTC {rtc_id} not found"}), 404

    # Update scalar fields if provided
    updates = {}
    for field in ["start_date", "department", "notes"]:
        if field in data:
            updates[field] = data[field]
    # Record last editor when meaningful fields change
    meaningful = {"start_date", "department", "notes", "project_number", "task_order_number"}
    if any(f in data for f in meaningful):
        updates["last_edited_by"] = user
        updates["last_edited_at"] = now

    if "project_number" in data and "task_order_number" in data:
        project_id = get_or_create_project(c, data, now)
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
            WHERE r.rtc_id = %s
        """, (rtc_id,)).fetchone()
        if proj and proj["project_status"] in ("Placeholder", "Pending"):
            set_clause = ", ".join(f"{k} = %s" for k in proj_updates)
            c.execute(f"UPDATE projects SET {set_clause} WHERE project_id = %s",
                      list(proj_updates.values()) + [proj["project_id"]])

    if updates:
        updates["last_updated_by"] = user
        updates["last_updated_at"] = now
        set_clause = ", ".join(f"{k} = %s" for k in updates)
        c.execute(f"UPDATE rtcs SET {set_clause} WHERE rtc_id = %s",
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
            "SELECT 1 FROM reporting_periods WHERE period_start = %s", (period,)
        ).fetchone()
        if not valid_period:
            continue

        # Guard: only update allocations for staff who are members of this RTC.
        # Prevents a removed person being silently reinstated via a PATCH.
        is_member = c.execute("""
            SELECT 1 FROM allocations
            WHERE rtc_id = %s AND horizon_person_number = %s
            LIMIT 1
        """, (rtc_id, pid)).fetchone()
        if not is_member:
            continue

        # Guard: a leaver keeps their historical rows for audit, but no further
        # time may be booked for them after the month they left.
        locked_from = _leaver_locked_from(
            (c.execute("SELECT end_date FROM staff WHERE horizon_person_number = %s",
                       (pid,)).fetchone() or {}).get("end_date"))
        if locked_from and period >= locked_from:
            rejected_leavers.append(pid)
            continue

        # Capture what it was, so the audit entry can state the net effect
        # rather than just that "something changed".
        prev = c.execute("""
            SELECT days FROM allocations
            WHERE horizon_person_number = %s AND rtc_id = %s AND period_start = %s
        """, (pid, rtc_id, period)).fetchone()
        was = float(prev["days"]) if prev else 0.0
        if float(days) != was:
            alloc_changes.append({"person": pid, "period": period,
                                  "was": was, "days": float(days)})

        c.execute("""
            INSERT INTO allocations
                (horizon_person_number, rtc_id, period_start, days, last_updated)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT(horizon_person_number, rtc_id, period_start)
            DO UPDATE SET days = excluded.days, last_updated = excluded.last_updated
        """, (pid, rtc_id, period, days, now))
        alloc_count += 1

    # Update last_updated_by and last_edited_by on the RTC itself
    c.execute("""
        UPDATE rtcs SET last_updated_by = %s, last_updated_at = %s,
                        last_edited_by = %s, last_edited_at = %s
        WHERE rtc_id = %s
    """, (user, now, user, now, rtc_id))

    # Auto-unarchive if allocations have been added to an archived RTC
    c.execute(
        "UPDATE rtcs SET is_archived = 0 WHERE rtc_id = %s AND is_archived = 1",
        (rtc_id,)
    )

    # Recorded on the same connection, before the commit, so the audit entry
    # and the change it describes land together or not at all.
    if alloc_changes:
        audit.record(audit.ALLOCATIONS_EDITED, rtc_id=rtc_id,
                     detail=audit.summarise_allocation_changes(alloc_changes),
                     conn=conn)

    conn.commit()
    conn.close()

    summary_module.mark_dirty()

    if rejected_leavers:
        logger.warning(
            f"RTC {rtc_id}: refused allocations for leaver(s) "
            f"{', '.join(sorted(set(rejected_leavers)))} — no time may be booked "
            f"after the month they left")
    return jsonify({
        "status": "ok",
        "allocations_updated": alloc_count,
        "rejected_leavers": sorted(set(rejected_leavers)),
    })


@rtcs_bp.route("/api/rtcs/<int:rtc_id>/staff", methods=["POST"])
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
        "SELECT rtc_id, start_date FROM rtcs WHERE rtc_id = %s", (rtc_id,)
    ).fetchone()
    if not rtc:
        conn.close()
        return jsonify({"error": f"RTC {rtc_id} not found"}), 404

    # Refuse to add staff to special RTCs — managed by nightly job
    special_check = c.execute("""
        SELECT p.project_number FROM rtcs r
        JOIN projects p ON p.project_id = r.project_id
        WHERE r.rtc_id = %s
    """, (rtc_id,)).fetchone()
    if special_check and special_check["project_number"] in ("ID-06", "ID-04", "IDUK-01"):
        conn.close()
        return jsonify({"error": "Staff on special RTCs are managed automatically by a nightly import"}), 400

    # Confirm person exists in staff
    if not c.execute(
        "SELECT 1 FROM staff WHERE horizon_person_number = %s", (pid,)
    ).fetchone():
        conn.close()
        return jsonify({"error": f"Staff member {pid} not found"}), 404

    # For generics, allow multiple instances by creating a unique suffixed ID
    if pid.startswith('GENERIC-'):
        existing_count = c.execute("""
            SELECT COUNT(DISTINCT horizon_person_number) AS n FROM allocations
            WHERE rtc_id = %s AND horizon_person_number LIKE %s
        """, (rtc_id, pid + '%')).fetchone()["n"]
        if existing_count > 0:
            pid = f"{pid}_{existing_count + 1}"
            original_pid = str(data["horizon_person_number"]).strip()
            orig = c.execute(
                "SELECT * FROM staff WHERE horizon_person_number = %s", (original_pid,)
            ).fetchone()
            if orig:
                c.execute("""
                    INSERT INTO staff
                        (horizon_person_number, name, job_title,
                         job_function, department, availability, last_imported)
                    VALUES (%s, %s, %s, %s, %s, %s, 'seeded')
                    ON CONFLICT DO NOTHING
                """, (pid, orig["name"], orig["job_title"],
                      orig["job_function"], orig["department"], orig["availability"]))

    # Get the periods already in use for this RTC
    # (match existing staff's allocation range, minimum 12)
    existing_end = c.execute("""
        SELECT MAX(period_start) AS m FROM allocations WHERE rtc_id = %s
    """, (rtc_id,)).fetchone()["m"]

    if existing_end:
        periods = c.execute("""
            SELECT period_start FROM reporting_periods
            WHERE period_start >= %s AND period_start <= %s
            ORDER BY period_start
        """, (rtc["start_date"], existing_end)).fetchall()
    else:
        periods = c.execute("""
            SELECT period_start FROM reporting_periods
            WHERE period_start >= %s
            ORDER BY period_start LIMIT 12
        """, (rtc["start_date"],)).fetchall()

    added = 0
    for p in periods:
        c.execute("""
            INSERT INTO allocations
                (horizon_person_number, rtc_id, period_start, days, last_updated)
            VALUES (%s, %s, %s, 0, %s)
            ON CONFLICT DO NOTHING
        """, (pid, rtc_id, p["period_start"], now))
        added += c.rowcount

    c.execute("""
        UPDATE rtcs SET last_updated_by = %s, last_updated_at = %s
        WHERE rtc_id = %s
    """, (user, now, rtc_id))

    conn.commit()
    conn.close()
    logger.info(f"RTC {rtc_id}: staff {pid} added by {user} ({added} allocation rows)")
    return jsonify({"status": "ok", "periods_added": added})


@rtcs_bp.route("/api/rtcs/<int:rtc_id>/staff/<person_id>", methods=["DELETE"])
def api_remove_rtc_staff(rtc_id, person_id):
    """Removes a staff member from an RTC (deletes all their allocation rows)."""
    user = get_current_user()
    now  = datetime.now(timezone.utc).isoformat()
    conn = database.get_connection()
    c    = conn.cursor()

    # Refuse to modify staff on special RTCs
    _sp = c.execute("""
        SELECT p.project_number FROM rtcs r
        JOIN projects p ON p.project_id = r.project_id WHERE r.rtc_id = %s
    """, (rtc_id,)).fetchone()
    if _sp and _sp["project_number"] in SPECIAL_PROJECT_NUMBERS:
        conn.close()
        return jsonify({"error": "Staff on special RTCs are managed automatically"}), 400

    c.execute("""
        DELETE FROM allocations
        WHERE rtc_id = %s AND horizon_person_number = %s
    """, (rtc_id, person_id))
    deleted = c.rowcount

    c.execute("""
        UPDATE rtcs SET last_updated_by = %s, last_updated_at = %s
        WHERE rtc_id = %s
    """, (user, now, rtc_id))

    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "rows_deleted": deleted})


@rtcs_bp.route("/api/rtcs/<int:rtc_id>/staff/<person_id>/replace", methods=["POST"])
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

    # Refuse to modify staff on special RTCs
    _conn_tmp = database.get_connection()
    _sp = _conn_tmp.execute("""
        SELECT p.project_number FROM rtcs r
        JOIN projects p ON p.project_id = r.project_id WHERE r.rtc_id = %s
    """, (rtc_id,)).fetchone()
    _conn_tmp.close()
    if _sp and _sp["project_number"] in SPECIAL_PROJECT_NUMBERS:
        return jsonify({"error": "Staff on special RTCs are managed automatically"}), 400

    new_pid = str(data["new_horizon_person_number"]).strip()
    user    = get_current_user()
    now     = datetime.now(timezone.utc).isoformat()
    conn    = database.get_connection()
    c       = conn.cursor()

    # Confirm new person exists in staff
    if not c.execute(
        "SELECT 1 FROM staff WHERE horizon_person_number = %s", (new_pid,)
    ).fetchone():
        conn.close()
        return jsonify({"error": f"Staff member {new_pid} not found"}), 404

    # Get the existing allocations for the old person on this RTC
    old_allocs = c.execute("""
        SELECT period_start, days FROM allocations
        WHERE rtc_id = %s AND horizon_person_number = %s
    """, (rtc_id, person_id)).fetchall()

    if not old_allocs:
        conn.close()
        return jsonify({"error": "Person not found on this RTC"}), 404

    # Copy allocations to new person
    for row in old_allocs:
        c.execute("""
            INSERT INTO allocations
                (horizon_person_number, rtc_id, period_start, days, last_updated)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT(horizon_person_number, rtc_id, period_start)
            DO UPDATE SET days = excluded.days, last_updated = excluded.last_updated
        """, (new_pid, rtc_id, row["period_start"], row["days"], now))

    # Delete old person's rows
    c.execute(
        "DELETE FROM allocations WHERE rtc_id = %s AND horizon_person_number = %s",
        (rtc_id, person_id)
    )

    c.execute("""
        UPDATE rtcs SET last_updated_by = %s, last_updated_at = %s
        WHERE rtc_id = %s
    """, (user, now, rtc_id))

    conn.commit()
    conn.close()
    summary_module.mark_dirty()
    return jsonify({"status": "ok", "replaced": person_id, "with": new_pid})


@rtcs_bp.route("/api/rtcs/<int:rtc_id>/extend", methods=["POST"])
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
        WHERE rtc_id = %s
    """, (rtc_id,)).fetchall()
    if not staff_rows:
        conn.close()
        return jsonify({"error": "No staff on this RTC"}), 400

    # Find the current last period
    last_period = c.execute("""
        SELECT MAX(period_start) AS m FROM allocations WHERE rtc_id = %s
    """, (rtc_id,)).fetchone()["m"]
    if not last_period:
        conn.close()
        return jsonify({"error": "No existing periods found"}), 400

    # Cap: refuse to extend beyond 60 months from today
    today       = date.today().replace(day=1)
    max_period  = (today + relativedelta(months=60)).isoformat()
    last_date   = date.fromisoformat(last_period)
    if last_period >= max_period:
        conn.close()
        return jsonify({"error": "Cannot extend beyond 5 years from today"}), 400

    # Refuse to extend special RTCs
    special_check = c.execute("""
        SELECT p.project_number FROM rtcs r
        JOIN projects p ON p.project_id = r.project_id
        WHERE r.rtc_id = %s
    """, (rtc_id,)).fetchone()
    if special_check and special_check["project_number"] in ("ID-06", "ID-04", "IDUK-01"):
        conn.close()
        return jsonify({"error": "Special RTCs are extended automatically once per month"}), 400

    # Ensure periods exist 12 months beyond the current last one
    target = last_date + relativedelta(months=12)
    database.ensure_periods_through(conn, target)

    # Get the next 12 periods after the current last one
    new_periods = c.execute("""
        SELECT period_start FROM reporting_periods
        WHERE period_start > %s
        ORDER BY period_start LIMIT 12
    """, (last_period,)).fetchall()

    # Check if this is the AL&PH RTC — pre-fill bank holidays if so
    rtc_proj = c.execute("""
        SELECT p.project_number FROM rtcs r
        JOIN projects p ON p.project_id = r.project_id
        WHERE r.rtc_id = %s
    """, (rtc_id,)).fetchone()
    is_alph = rtc_proj and rtc_proj["project_number"] == "ID-06"

    # Insert allocation rows for all staff for all new periods
    added = 0
    for person in staff_rows:
        pid = person["horizon_person_number"]
        for p in new_periods:
            days = 0
            if is_alph:
                # Pre-fill bank holidays from cache
                year, month, _ = p["period_start"].split("-")
                bh_row = c.execute("""
                    SELECT COUNT(*) FROM bank_holidays
                    WHERE date LIKE %s
                """, (f"{year}-{month}-%",)).fetchone()
                days = bh_row[0] if bh_row else 0
                if p["period_start"][5:7] == "12":
                    days += 3
            c.execute("""
                INSERT INTO allocations
                    (horizon_person_number, rtc_id, period_start, days, last_updated)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
            """, (pid, rtc_id, p["period_start"], days, now))
            added += c.rowcount

    c.execute("""
        UPDATE rtcs SET last_updated_by = %s, last_updated_at = %s
        WHERE rtc_id = %s
    """, (user, now, rtc_id))

    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "periods_added": len(new_periods), "rows_added": added})


@rtcs_bp.route("/api/rtcs/<int:rtc_id>/opened", methods=["POST"])
def api_rtc_opened(rtc_id):
    """Records that a user has opened this RTC for editing."""
    user = get_current_user()
    now  = datetime.now(timezone.utc).isoformat()
    conn = database.get_connection()
    conn.execute("""
        UPDATE rtcs SET last_opened_by = %s, last_opened = %s
        WHERE rtc_id = %s
    """, (user, now, rtc_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@rtcs_bp.route("/api/rtcs/<int:rtc_id>/clear-auto-link", methods=["POST"])
def api_clear_auto_link(rtc_id):
    """Clears the auto_linked flag after user has confirmed the link."""
    conn = database.get_connection()
    conn.execute("UPDATE rtcs SET auto_linked = 0 WHERE rtc_id = %s", (rtc_id,))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@rtcs_bp.route("/api/rtcs/<int:rtc_id>/check-horizon")
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
        WHERE r.rtc_id = %s
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


@rtcs_bp.route("/api/rtcs/<int:rtc_id>/link-horizon", methods=["POST"])
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

    # Same validation as the preview (and the same already-linked guard the
    # conversion path uses), so a mistyped number cannot attach this RTC to a
    # project another RTC already owns.
    real_project, err = _target_project(
        c, rtc_id, proj_num, task_order, require_live=False)
    if err:
        conn.close()
        return err

    rtc = c.execute(
        "SELECT project_id FROM rtcs WHERE rtc_id = %s", (rtc_id,)
    ).fetchone()
    was_desc = audit.describe_rtc(conn, rtc_id)   # identity before linking

    old_project_id = rtc["project_id"]
    now  = datetime.now(timezone.utc).isoformat()
    user = get_current_user()

    c.execute("""
        UPDATE rtcs SET project_id = %s, last_updated_by = %s, last_updated_at = %s,
                        department = COALESCE(%s, department)
        WHERE rtc_id = %s
    """, (real_project["project_id"], user, now,
          real_project["project_organisation"] or None, rtc_id))

    # Update the old placeholder project row with authoritative PAR data
    c.execute("""
        UPDATE projects SET
            project_name     = COALESCE(%s, project_name),
            task_name        = COALESCE(%s, task_name),
            project_customer = COALESCE(%s, project_customer),
            project_director = COALESCE(%s, project_director),
            project_manager  = COALESCE(%s, project_manager)
        WHERE project_id = %s
    """, (real_project["project_name"],
          real_project["task_name"],
          real_project["project_customer"],
          real_project["project_director"],
          real_project["project_manager"],
          real_project["project_id"]))

    audit.record(audit.RTC_LINKED, rtc_id=rtc_id,
                 rtc_description=f"{proj_num}/{task_order} — "
                                 f"{real_project['project_name'] or ''}".strip(" —"),
                 detail=f"Linked from placeholder {was_desc}", conn=conn)

    # Clean up orphaned placeholder project row
    other_refs = c.execute(
        "SELECT COUNT(*) AS n FROM rtcs WHERE project_id = %s", (old_project_id,)
    ).fetchone()["n"]
    if other_refs == 0:
        c.execute("DELETE FROM projects WHERE project_id = %s", (old_project_id,))

    conn.commit()
    conn.close()
    summary_module.mark_dirty()
    return jsonify({"status": "ok", "project_id": real_project["project_id"]})


@rtcs_bp.route("/api/rtcs/<int:rtc_id>/link-horizon/preview", methods=["POST"])
def api_link_horizon_preview(rtc_id):
    """
    Dry run for linking a placeholder RTC to a Horizon record. Applies the same
    rules as the real link but changes nothing, returning the target's details
    so the PM can confirm the numbers before committing. An opportunity is a
    valid target here (unlike conversion), since placeholder -> opportunity ->
    live is the normal progression.
    """
    data = request.get_json(silent=True, force=True) or {}
    conn = database.get_connection()
    c    = conn.cursor()
    target, err = _target_project(
        c, rtc_id,
        (data.get("project_number")    or "").strip(),
        (data.get("task_order_number") or "").strip(),
        require_live=False)
    conn.close()
    if err:
        return err
    return jsonify({
        "status": "ok",
        "project": {
            "project_number":    target["project_number"],
            "task_order_number": target["task_order_number"],
            "project_name":      target["project_name"],
            "task_name":         target["task_name"],
            "project_customer":  target["project_customer"],
            "project_director":  target["project_director"],
            "project_manager":   target["project_manager"],
            "project_type":      target["project_type"],
        },
    })


@rtcs_bp.route("/api/rtcs/<int:rtc_id>/convert-to-live/preview", methods=["POST"])
def api_convert_to_live_preview(rtc_id):
    """
    Dry run for a conversion. Applies exactly the same rules as the real
    conversion but changes nothing — returns the target project's details so
    the PM can confirm they have typed the right numbers before committing.
    """
    data = request.get_json(silent=True, force=True) or {}
    proj_num   = (data.get("project_number")    or "").strip()
    task_order = (data.get("task_order_number") or "").strip()

    conn = database.get_connection()
    c    = conn.cursor()
    target, err = _target_project(c, rtc_id, proj_num, task_order, require_live=True)
    if err:
        conn.close()
        return err
    conn.close()
    return jsonify({
        "status": "ok",
        "project": {
            "project_number":       target["project_number"],
            "task_order_number":    target["task_order_number"],
            "project_name":         target["project_name"],
            "task_name":            target["task_name"],
            "project_customer":     target["project_customer"],
            "project_director":     target["project_director"],
            "project_manager":      target["project_manager"],
            "project_organisation": target["project_organisation"],
            "project_type":         target["project_type"],
        },
    })


def _target_project(c, rtc_id, proj_num, task_order, require_live=True):
    """
    Shared validation for placeholder linking and opportunity conversion.
    Returns (target_row, None) when permitted, else (None, flask_response).

    require_live=True  (convert-to-live): the target must be a genuine live
                       project; an opportunity is refused.
    require_live=False (placeholder link): any real PAR row is acceptable,
                       including an opportunity, since placeholder ->
                       opportunity -> live is the normal progression.

    Both paths reject a target already linked to a different RTC, so a mistyped
    number cannot silently attach this RTC to someone else's project.
    """
    if not proj_num or not task_order:
        return None, (jsonify({
            "error": "Project Number and Task Order Number are required."}), 400)

    rtc = c.execute("SELECT project_id FROM rtcs WHERE rtc_id = %s", (rtc_id,)).fetchone()
    if not rtc:
        return None, (jsonify({"error": f"RTC {rtc_id} not found"}), 404)

    target = c.execute("""
        SELECT project_id, project_number, task_order_number, project_name,
               task_name, project_customer, project_director, project_manager,
               project_type, project_status, project_organisation
        FROM projects
        WHERE project_number = %s AND task_order_number = %s
          AND project_status != 'Placeholder'
    """, (proj_num, task_order)).fetchone()

    # Rule 1 — must exist in current data
    if not target:
        return None, (jsonify({"error":
            "No project with that Project Number and Task Order was found in "
            "the latest Horizon data. If it has only just been created, it "
            "will be available after the next overnight refresh."}), 404)

    # Rule 2 — conversion only: must be a live project, not another opportunity
    if require_live:
        ptype = (target["project_type"]   or "").strip()
        pstat = (target["project_status"] or "").strip().lower()
        if not (pstat == "active" and ptype != "UK Opportunity"):
            return None, (jsonify({"error":
                "That project is an opportunity, not a live project. "
                "It cannot be the target of a conversion."}), 409)

    # Rule 3 — must not already be linked to another RTC
    if target["project_id"] != rtc["project_id"]:
        other = c.execute(
            "SELECT COUNT(*) AS n FROM rtcs WHERE project_id = %s",
            (target["project_id"],)).fetchone()["n"]
        if other > 0:
            return None, (jsonify({"error":
                "That project is already linked to an existing RTC."}), 409)

    return target, None


@rtcs_bp.route("/api/rtcs/<int:rtc_id>/convert-to-live", methods=["POST"])
def api_convert_to_live(rtc_id):
    """
    Converts an opportunity RTC to a live project.

    The PM supplies only the new Project Number + Task Order; every other
    field comes from the PAR data. Validation is shared with the preview
    endpoint (see _conversion_target) and re-run here, so a stale preview
    cannot be used to force through a conversion that is no longer valid.
    On success the RTC is re-pointed to the live project; its allocations
    (keyed to rtc_id) follow automatically. The old opportunity project row
    is left intact — unlike placeholder linking, it is real PAR data.
    """
    data = request.get_json(silent=True, force=True) or {}
    proj_num   = (data.get("project_number")    or "").strip()
    task_order = (data.get("task_order_number") or "").strip()

    conn = database.get_connection()
    c    = conn.cursor()
    target, err = _target_project(c, rtc_id, proj_num, task_order, require_live=True)
    if err:
        conn.close()
        return err

    now  = datetime.now(timezone.utc).isoformat()
    user = get_current_user()
    was  = audit.describe_rtc(conn, rtc_id)      # identity before the change
    c.execute("""
        UPDATE rtcs SET project_id = %s, last_updated_by = %s, last_updated_at = %s,
                        department = COALESCE(%s, department)
        WHERE rtc_id = %s
    """, (target["project_id"], user, now,
          target["project_organisation"] or None, rtc_id))

    audit.record(audit.RTC_CONVERTED, rtc_id=rtc_id,
                 detail=f"From {was} to {proj_num}/{task_order}", conn=conn)
    conn.commit()
    conn.close()
    summary_module.mark_dirty()
    return jsonify({"status": "ok", "project_id": target["project_id"]})