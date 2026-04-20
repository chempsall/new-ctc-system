"""
summary.py
Builds the pre-calculated summary JSON that the dashboard serves instantly.

Called after every import run and after every macro push.
The frontend receives one payload on page load and does all filtering
in JavaScript — zero additional server requests for any dashboard interaction.

Financial statistics are calculated here using grade_rates (server-side only).
The rates themselves never appear in the output JSON — only computed results.
"""

import json
from datetime import datetime, timezone, date
from database import get_connection


HORIZON_MONTHS = 6   # How many future months to include in the summary


def _get_active_periods(conn, from_date=None):
    """
    Return the next HORIZON_MONTHS reporting periods from today (or from_date).
    Returns list of dicts with period_start, label, working_days, financial_year.
    """
    if from_date is None:
        from_date = date.today().replace(day=1).isoformat()

    rows = conn.execute("""
        SELECT period_start, period_end, working_days, label, financial_year
        FROM reporting_periods
        WHERE period_start >= ?
        ORDER BY period_start
        LIMIT ?
    """, (from_date, HORIZON_MONTHS)).fetchall()

    return [dict(r) for r in rows]


def _get_rates(conn):
    """Grade -> rate dict. Used only within this module, never serialised."""
    rows = conn.execute(
        "SELECT grade, raw_cost, burdened_cost FROM grade_rates"
    ).fetchall()
    return {r["grade"]: {"raw": r["raw_cost"], "burdened": r["burdened_cost"]}
            for r in rows}


def build(office: str = None) -> dict:
    """
    Build the full summary JSON.
    If office is specified, builds for that office only (future: multi-office).
    Returns the summary dict and also writes it to summary_cache table.
    """
    conn = get_connection()
    generated_at = datetime.now(timezone.utc).isoformat()

    periods = _get_active_periods(conn)
    period_starts = [p["period_start"] for p in periods]
    rates = _get_rates(conn)

    # -- Working days lookup -------------------------------------------------
    working_days = {p["label"]: p["working_days"] for p in periods}

    # -- Staff ---------------------------------------------------------------
    staff_query = "SELECT * FROM staff WHERE end_date IS NULL OR end_date > ?"
    if office:
        staff_query += " AND office = ?"
        staff_rows = conn.execute(staff_query, (date.today().isoformat(), office)).fetchall()
    else:
        staff_rows = conn.execute(staff_query, (date.today().isoformat(),)).fetchall()

    # Availability fractions per person per period
    avail_rows = conn.execute("""
        SELECT horizon_person_number, period_start, availability_fraction
        FROM staff_availability
        WHERE period_start IN ({})
    """.format(",".join("?" * len(period_starts))), period_starts).fetchall()

    avail_map = {}  # person_id -> {period_start -> fraction}
    for r in avail_rows:
        avail_map.setdefault(r["horizon_person_number"], {})[r["period_start"]] = \
            r["availability_fraction"]

    # Allocations per person per project per period.
    # Join through ctc_files to get project context.
    # A project is "linked" (fee-earning) if it has a real project_status
    # of "Active" from the PAR import — i.e. it exists in Horizon.
    alloc_rows = conn.execute("""
        SELECT a.horizon_person_number, cf.project_id, a.period_start, a.days,
               p.project_status, cf.office, cf.ctc_id
        FROM allocations a
        JOIN ctc_files cf ON cf.ctc_id = a.ctc_id
        JOIN projects p   ON p.project_id = cf.project_id
        WHERE a.period_start IN ({})
    """.format(",".join("?" * len(period_starts))), period_starts).fetchall()

    person_alloc     = {}  # person_id -> period_start -> total days
    person_horizon   = {}  # person_id -> period_start -> days on PAR-linked projects
    person_proj_days = {}  # person_id -> project_id -> period_start -> days

    for r in alloc_rows:
        pid  = r["horizon_person_number"]
        ps   = r["period_start"]
        days = r["days"] or 0

        person_alloc.setdefault(pid, {})
        person_alloc[pid][ps] = person_alloc[pid].get(ps, 0) + days

        # "Linked" means the project came from PAR with Active status
        if r["project_status"] and r["project_status"].lower() == "active":
            person_horizon.setdefault(pid, {})
            person_horizon[pid][ps] = person_horizon[pid].get(ps, 0) + days

        person_proj_days.setdefault(pid, {}).setdefault(r["project_id"], {})
        person_proj_days[pid][r["project_id"]][ps] = days

    # Build staff list for JSON
    staff_list = []
    for s in staff_rows:
        pid = s["horizon_person_number"]
        grade = s["technical_grade"]

        capacity     = {}
        allocated    = {}
        horizon_days = {}
        no_rec_days  = {}
        kpi          = {}

        for p in periods:
            ps    = p["period_start"]
            label = p["label"]
            wdays = p["working_days"]

            frac = avail_map.get(pid, {}).get(ps, s["availability"] or 1.0)
            cap  = round(wdays * frac, 2)

            alloc   = round(person_alloc.get(pid, {}).get(ps, 0), 2)
            h_days  = round(person_horizon.get(pid, {}).get(ps, 0), 2)
            nr_days = round(alloc - h_days, 2)

            capacity[label]     = cap
            allocated[label]    = alloc
            horizon_days[label] = h_days
            no_rec_days[label]  = nr_days

            # KPI: compare allocated vs capacity
            if cap == 0:
                kpi[label] = "unavailable"
            elif alloc > cap * 1.05:      # >105% = over
                kpi[label] = "over"
            elif alloc >= cap * 0.85:     # 85-105% = check
                kpi[label] = "check"
            else:
                kpi[label] = "ok"

        # Projects this person is on
        proj_entries = []
        for project_id, period_days in person_proj_days.get(pid, {}).items():
            proj_entries.append({
                "project_id": project_id,
                "days": {
                    p["label"]: round(period_days.get(p["period_start"], 0), 2)
                    for p in periods
                }
            })

        staff_list.append({
            "id":           pid,
            "name":         s["name"],
            "grade":        grade,
            "team":         s["staff_team"] or "",
            "discipline":   s["discipline"] or "",
            "office":       s["office"],
            "availability": s["availability"],
            "capacity":     capacity,
            "allocated":    allocated,
            "horizon_days": horizon_days,
            "no_record_days": no_rec_days,
            "kpi":          kpi,
            "projects":     proj_entries
        })

    # -- Projects ------------------------------------------------------------
    # Query projects that have at least one CTC file (i.e. are being worked on).
    # Join ctc_files to get office/team context.
    # Filter by office if specified — shows only projects this office is working on.
    if office:
        proj_rows = conn.execute("""
            SELECT DISTINCT p.*, cf.office, cf.staff_team, cf.ctc_id,
                   cf.ctc_start_date, cf.conflict_flag, cf.start_date_changed,
                   cf.last_pushed
            FROM projects p
            JOIN ctc_files cf ON cf.project_id = p.project_id
            WHERE cf.office = ?
        """, (office,)).fetchall()
    else:
        proj_rows = conn.execute("""
            SELECT DISTINCT p.*, cf.office, cf.staff_team, cf.ctc_id,
                   cf.ctc_start_date, cf.conflict_flag, cf.start_date_changed,
                   cf.last_pushed
            FROM projects p
            JOIN ctc_files cf ON cf.project_id = p.project_id
        """).fetchall()

    # Total allocated days per project per period (across all ctc_files)
    proj_days_map = {}  # project_id -> period_start -> days
    for r in alloc_rows:
        pd = proj_days_map.setdefault(r["project_id"], {})
        pd[r["period_start"]] = pd.get(r["period_start"], 0) + (r["days"] or 0)

    projects_list = []
    for p in proj_rows:
        project_id = p["project_id"]
        proj_number = p["project_number"] or ""
        task_order  = p["task_order_number"] or ""

        # Days per period
        period_days = {}
        for period in periods:
            ps    = period["period_start"]
            label = period["label"]
            period_days[label] = round(proj_days_map.get(project_id, {}).get(ps, 0), 2)

        # Financial statistics — calculated server-side, rates never exposed
        financials = _calculate_financials(
            conn, project_id, p, periods, rates, period_starts
        )

        # A project is "linked" if it came from PAR with Active status
        horizon_status = (
            "linked"
            if p["project_status"] and p["project_status"].lower() == "active"
            else "No Horizon Record Found"
        )

        projects_list.append({
            "project_id":         project_id,
            "ctc_id":             p["ctc_id"] if "ctc_id" in p.keys() else None,
            "number":             proj_number,
            "task_order":         task_order,
            "name":               p["project_name"] or "No Horizon Record Found",
            "task_name":          p["task_name"] or "No Horizon Record Found",
            "organisation":       p["project_organisation"] or "No Horizon Record Found",
            "horizon_status":     horizon_status,
            "project_type":       p["project_type"] or "",
            "team":               p["staff_team"] or "",
            "office":             p["office"] if "office" in p.keys() else "",
            "pm":                 p["project_manager"],
            "director":           p["project_director"],
            "task_start_date":    p["task_start_date"],
            "task_end_date":      p["task_end_date"],
            "reporting_period":   p["reporting_period"],
            "ctc_start_date":     p["ctc_start_date"] if "ctc_start_date" in p.keys() else None,
            "conflict_flag":      bool(p["conflict_flag"]) if "conflict_flag" in p.keys() else False,
            "start_date_changed": bool(p["start_date_changed"]) if "start_date_changed" in p.keys() else False,
            "last_pushed":        p["last_pushed"] if "last_pushed" in p.keys() else None,
            "last_imported":      p["last_imported"],
            "total_days":         period_days,
            "financials":         financials
        })

    # -- Conflict warnings ---------------------------------------------------
    conflicts = [p for p in projects_list if p["conflict_flag"]]

    # -- Assemble final payload ----------------------------------------------
    summary = {
        "generated_at":  generated_at,
        "periods":       [p["label"] for p in periods],
        "working_days":  working_days,
        "staff":         staff_list,
        "projects":      projects_list,
        "conflicts":     conflicts,
        "offices":       _get_offices(conn),
        "teams":         _get_teams(conn),
        "last_imports":  _get_last_imports(conn)
    }

    # Write to cache table (single row, always cache_id=1)
    payload_json = json.dumps(summary, default=str)
    conn.execute("""
        INSERT INTO summary_cache (cache_id, generated_at, payload)
        VALUES (1, ?, ?)
        ON CONFLICT(cache_id) DO UPDATE SET
            generated_at = excluded.generated_at,
            payload = excluded.payload
    """, (generated_at, payload_json))
    conn.commit()
    conn.close()

    return summary


def _calculate_financials(conn, project_id, project_row, periods, rates,
                          period_starts):
    """
    Calculate financial statistics for a project.
    Combines:
      - PAR figures already on the project row (budgets, actuals, funding)
      - Cost-to-complete calculated from future allocations x grade rates
    Rates are used server-side only and never appear in the output.
    """
    # Budget and actuals come directly from the project row (populated by PAR import)
    budget_dlm      = project_row["current_budget_dlm"] or 0
    budget_raw      = project_row["current_budget_raw_labor"] or 0
    budget_nr       = project_row["current_budget_nr"] or 0
    actual_itd_dlm  = project_row["actual_itd_dlm"]
    actual_itd_raw  = project_row["actual_itd_raw_labor"]
    funding_value   = project_row["funding_value"]

    # Cost-to-complete from future allocations x grade rates
    alloc_rows = conn.execute("""
        SELECT a.period_start, a.days, s.technical_grade
        FROM allocations a
        JOIN staff s ON s.horizon_person_number = a.horizon_person_number
        WHERE a.project_id = ?
        AND a.period_start IN ({})
    """.format(",".join("?" * len(period_starts))),
        [project_id] + period_starts
    ).fetchall()

    raw_ctc = burdened_ctc = 0.0
    today = __import__("datetime").date.today().isoformat()
    for r in alloc_rows:
        if r["period_start"] < today:
            continue
        rate = rates.get(r["technical_grade"], {"raw": 0, "burdened": 0})
        days = r["days"] or 0
        raw_ctc      += days * rate["raw"]
        burdened_ctc += days * rate["burdened"]

    # Variance: actual ITD DLM vs budget DLM
    variance = None
    if actual_itd_dlm is not None and budget_dlm:
        variance = round(actual_itd_dlm - budget_dlm, 4)

    # Remaining budget: funding value minus burdened cost to complete
    remaining = None
    if funding_value:
        remaining = round(funding_value - burdened_ctc, 2)

    return {
        "funding_value":             round(funding_value, 2) if funding_value else None,
        "current_budget_dlm":        budget_dlm,
        "current_budget_raw_labor":  round(budget_raw, 2) if budget_raw else None,
        "current_budget_nr":         round(budget_nr, 2) if budget_nr else None,
        "actual_itd_dlm":            actual_itd_dlm,
        "actual_itd_raw_labor":      round(actual_itd_raw, 2) if actual_itd_raw else None,
        "raw_cost_to_complete":      round(raw_ctc, 2),
        "burdened_cost_to_complete": round(burdened_ctc, 2),
        "remaining_budget":          remaining,
        "variance_vs_budget_dlm":    variance,
    }


def _staff_name(conn, horizon_person_number):
    if not horizon_person_number:
        return None
    row = conn.execute(
        "SELECT name FROM staff WHERE horizon_person_number = ?",
        (horizon_person_number,)
    ).fetchone()
    return row["name"] if row else None


def _get_offices(conn):
    rows = conn.execute(
        "SELECT office_name, office_code FROM offices WHERE active = 1"
    ).fetchall()
    return [{"name": r["office_name"], "code": r["office_code"]} for r in rows]


def _get_teams(conn):
    rows = conn.execute("""
        SELECT t.team_name, o.office_name
        FROM teams t
        JOIN offices o ON o.office_id = t.office_id
        WHERE t.active = 1
        ORDER BY o.office_name, t.team_name
    """).fetchall()
    return [{"team": r["team_name"], "office": r["office_name"]} for r in rows]


def _get_last_imports(conn):
    rows = conn.execute("""
        SELECT import_type, MAX(completed_at) as last_run, SUM(errors != '[]') as had_errors
        FROM import_log
        GROUP BY import_type
    """).fetchall()
    return {r["import_type"]: {
        "last_run": r["last_run"],
        "had_errors": bool(r["had_errors"])
    } for r in rows}


def get_cached() -> dict:
    """Return the cached summary JSON, or None if not yet built."""
    conn = get_connection()
    row = conn.execute(
        "SELECT payload, generated_at FROM summary_cache WHERE cache_id = 1"
    ).fetchone()
    conn.close()
    if row:
        return {"generated_at": row["generated_at"], "payload": json.loads(row["payload"])}
    return None