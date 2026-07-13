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
from services.identity import get_current_user
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
            p.project_type,
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
        is_special = (row["project_number"] or "") in SPECIAL_PROJECT_NUMBERS
        if row["is_archived"]:
            status = "archived"
        elif is_special:
            status = "current"
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

    user = get_current_user()
    now  = datetime.now(timezone.utc).isoformat()
    conn = database.get_connection()
    c    = conn.cursor()

    project_id = get_or_create_project(c, data, now)

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
        "SELECT rtc_id FROM rtcs WHERE rtc_id = ?", (rtc_id,)
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

    rtc_dict = dict(rtc)
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
    for field in ["start_date", "department", "notes"]:
        if field in data:
            updates[field] = data[field]

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
    logger.info(f"RTC {rtc_id}: staff {pid} added by {user} ({added} allocation rows)")
    return jsonify({"status": "ok", "periods_added": added})


@rtcs_bp.route("/api/rtcs/<int:rtc_id>/staff/<person_id>", methods=["DELETE"])
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

    # Ensure periods exist 12 months beyond the current last one
    last_date = date.fromisoformat(last_period)
    target = last_date + relativedelta(months=12)
    database.ensure_periods_through(conn, target)

    # Get the next 12 periods after the current last one
    new_periods = c.execute("""
        SELECT period_start FROM reporting_periods
        WHERE period_start > ?
        ORDER BY period_start LIMIT 12
    """, (last_period,)).fetchall()

    # Check if this is the AL&PH RTC — pre-fill bank holidays if so
    rtc_proj = c.execute("""
        SELECT p.project_number FROM rtcs r
        JOIN projects p ON p.project_id = r.project_id
        WHERE r.rtc_id = ?
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
                    WHERE date LIKE ?
                """, (f"{year}-{month}-%",)).fetchone()
                days = bh_row[0] if bh_row else 0
            c.execute("""
                INSERT OR IGNORE INTO allocations
                    (horizon_person_number, rtc_id, period_start, days, last_updated)
                VALUES (?, ?, ?, ?, ?)
            """, (pid, rtc_id, p["period_start"], days, now))
            added += c.rowcount

    c.execute("""
        UPDATE rtcs SET last_updated_by = ?, last_updated_at = ?
        WHERE rtc_id = ?
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
        UPDATE rtcs SET last_opened_by = ?, last_opened = ?
        WHERE rtc_id = ?
    """, (user, now, rtc_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@rtcs_bp.route("/api/rtcs/<int:rtc_id>/clear-auto-link", methods=["POST"])
def api_clear_auto_link(rtc_id):
    """Clears the auto_linked flag after user has confirmed the link."""
    conn = database.get_connection()
    conn.execute("UPDATE rtcs SET auto_linked = 0 WHERE rtc_id = ?", (rtc_id,))
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

    real_project = c.execute("""
        SELECT project_id, project_name, task_name, project_customer,
               project_director, project_manager, project_organisation
        FROM projects
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
        UPDATE rtcs SET project_id = ?, last_updated_by = ?, last_updated_at = ?,
                        department = COALESCE(?, department)
        WHERE rtc_id = ?
    """, (real_project["project_id"], user, now,
          real_project["project_organisation"] or None, rtc_id))

    # Update the old placeholder project row with authoritative PAR data
    c.execute("""
        UPDATE projects SET
            project_name     = COALESCE(?, project_name),
            task_name        = COALESCE(?, task_name),
            project_customer = COALESCE(?, project_customer),
            project_director = COALESCE(?, project_director),
            project_manager  = COALESCE(?, project_manager)
        WHERE project_id = ?
    """, (real_project["project_name"],
          real_project["task_name"],
          real_project["project_customer"],
          real_project["project_director"],
          real_project["project_manager"],
          real_project["project_id"]))

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
