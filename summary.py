import logging
"""
summary.py
Builds the pre-calculated summary JSON that the dashboard serves instantly.

Called after every import run and after every macro push.
The frontend receives one payload on page load and does all filtering
in JavaScript — zero additional server requests for any dashboard interaction.

Resourcing data only — this is a pure resourcing system, not a financial
one. Financial figures (rates, budgets, costs) have been removed from the
database entirely, not just from this module.
"""

import json
from datetime import datetime, timezone, date
from database import get_connection
import config
from services.projects import display_number, is_suffixed, is_placeholder


HORIZON_MONTHS = config.FORECAST_HORIZON_MONTHS

# ---------------------------------------------------------------------------
# Resourcing thresholds
# These determine the KPI status shown on the dashboard.
# Expressed as fractions of available capacity.
# ---------------------------------------------------------------------------
KPI_OVER_THRESHOLD  = 1.05   # above this = over-allocated (red)
KPI_UNDER_THRESHOLD = 0.95   # below this = under-resourced (green)
# between the two = fully allocated (amber)
# unavailable = no capacity at all

def _period_fte(start_date, end_date, period_start, period_end, availability):
    """
    Calculate FTE for a person in a given period, accounting for
    partial months due to joining or leaving.
    availability: fraction e.g. 0.8
    Returns a float between 0 and availability.
    """
    from datetime import date as _date

    def _parse(d):
        if isinstance(d, _date): return d
        if not d: return None
        return _date.fromisoformat(d)

    ps = _parse(period_start)
    pe = _parse(period_end)
    ss = _parse(start_date)
    se = _parse(end_date)

    # Clamp person's active range to the period
    effective_start = max(ps, ss) if ss else ps
    effective_end   = min(pe, se) if se else pe

    if effective_start > effective_end:
        return 0.0

    period_days    = (pe - ps).days + 1
    present_days   = (effective_end - effective_start).days + 1
    proportion     = present_days / period_days

    return round(availability * proportion, 3)

def _get_active_periods(conn, from_date=None):
    """
    Return the next HORIZON_MONTHS reporting periods from today (or from_date).
    Returns list of dicts with period_start, label, working_days.
    """
    if from_date is None:
        from_date = date.today().replace(day=1).isoformat()

    rows = conn.execute("""
        SELECT period_start, period_end, working_days, label
        FROM reporting_periods
        WHERE period_start >= ?
        ORDER BY period_start
        LIMIT ?
    """, (from_date, HORIZON_MONTHS)).fetchall()

    return [dict(r) for r in rows]

def build() -> dict:
    """
    Build the full summary JSON.
    Returns the summary dict and also writes it to summary_cache table.
    """
    conn = get_connection()
    generated_at = datetime.now(timezone.utc).isoformat()

    periods = _get_active_periods(conn)
    period_starts = [p["period_start"] for p in periods]
    # -- Working days lookup -------------------------------------------------
    working_days = {p["label"]: p["working_days"] for p in periods}

    # -- Staff ---------------------------------------------------------------
    first_period = period_starts[0] if period_starts else date.today().isoformat()
    last_period  = period_starts[-1] if period_starts else date.today().isoformat()
    staff_query  = """
        SELECT * FROM staff
        WHERE (end_date IS NULL OR end_date > ?)
        AND   (start_date IS NULL OR start_date <= ?)
        ORDER BY horizon_person_number
    """
    
    staff_rows = conn.execute(staff_query, (first_period, last_period)).fetchall()

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
    # Join through rtcs to get project context.
    # A project is "linked" (fee-earning) if it has a real project_status
    # of "Active" from the PAR import — i.e. it exists in Horizon.
    alloc_rows = conn.execute("""
        SELECT a.horizon_person_number, r.project_id, a.period_start, a.days,
               p.project_status, r.department, r.rtc_id
        FROM allocations a
        JOIN rtcs r     ON r.rtc_id = a.rtc_id
        JOIN projects p ON p.project_id = r.project_id
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

        capacity     = {}
        allocated    = {}
        horizon_days = {}
        no_rec_days  = {}
        fte          = {}
        kpi          = {}

        for p in periods:
            ps    = p["period_start"]
            label = p["label"]
            wdays = p["working_days"]

            if pid.startswith("GENERIC-"):
                cap = None
            else:
                frac = avail_map.get(pid, {}).get(ps, s["availability"] or 1.0)
                proportion = _period_fte(
                    s["start_date"], s["end_date"],
                    ps, p["period_end"], 1.0
                )
                cap = round(wdays * frac * proportion, 2)

            alloc   = round(person_alloc.get(pid, {}).get(ps, 0), 2)
            h_days  = round(person_horizon.get(pid, {}).get(ps, 0), 2)
            nr_days = round(alloc - h_days, 2)

            capacity[label]     = cap
            allocated[label]    = alloc
            horizon_days[label] = h_days
            no_rec_days[label]  = nr_days
            fte[label]          = 0.0 if pid.startswith("GENERIC-") else _period_fte(
                s["start_date"], s["end_date"],
                ps, p["period_end"],
                avail_map.get(pid, {}).get(ps, s["availability"] or 1.0)
            )


            # KPI: compare allocated vs capacity
            if pid.startswith("GENERIC-"):
                kpi[label] = "none"
            elif cap == 0:
                kpi[label] = "unavailable"
            elif alloc > cap * KPI_OVER_THRESHOLD:
                kpi[label] = "over"
            elif alloc >= cap * KPI_UNDER_THRESHOLD:
                kpi[label] = "ok"
            else:
                kpi[label] = "under"
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
            "job_title":    s["job_title"] or "",
            "job_function": s["job_function"] or "",
            "department":   s["department"] or "",
            "availability": s["availability"],
            "capacity":     capacity,
            "allocated":    allocated,
            "horizon_days": horizon_days,
            "no_record_days": no_rec_days,
            "fte":            fte,
            "kpi":            kpi,
            "projects":     proj_entries
        })

    # Merge suffixed generic copies (e.g. GENERIC-UK-SENIOR-ENGINEER_2)
    # into their base generic record, summing allocations
    import re as _re
    base_generics = {}
    merged_list = []
    for person in staff_list:
        pid = person["id"]
        m = _re.match(r'^(GENERIC-.+?)_\d+$', pid)
        if m:
            base_pid = m.group(1)
            if base_pid in base_generics:
                base = base_generics[base_pid]
                for period in person["allocated"]:
                    base["allocated"][period]     = base["allocated"].get(period, 0) + person["allocated"].get(period, 0)
                    base["fte"][period]            = base["fte"].get(period, 0) + person["fte"].get(period, 0)
                    base["horizon_days"][period]   = base["horizon_days"].get(period, 0) + person["horizon_days"].get(period, 0)
                    base["no_record_days"][period] = base["no_record_days"].get(period, 0) + person["no_record_days"].get(period, 0)
                for p in person["projects"]:
                    existing = next((x for x in base["projects"] if x["project_id"] == p["project_id"]), None)
                    if existing:
                        for period, days in p["days"].items():
                            existing["days"][period] = existing["days"].get(period, 0) + days
                    else:
                        base["projects"].append(p)
        else:
            merged_list.append(person)
            if pid.startswith("GENERIC-"):
                base_generics[pid] = person

    staff_list = merged_list

    # -- Projects ------------------------------------------------------------
    # Query projects that have at least one RTC (i.e. are being worked on).
    proj_rows = conn.execute("""
        SELECT DISTINCT p.*, r.department, r.rtc_id,
               r.start_date, r.last_updated_at
        FROM projects p
        JOIN rtcs r ON r.project_id = p.project_id
        WHERE r.is_archived = 0
    """).fetchall()

    # Total allocated days per project per period (across all RTCs)
    proj_days_map = {}  # (project_id, rtc_id) -> period_start -> days
    for r in alloc_rows:
        key = (r["project_id"], r["rtc_id"])
        pd = proj_days_map.setdefault(key, {})
        pd[r["period_start"]] = pd.get(r["period_start"], 0) + (r["days"] or 0)

    projects_list = []
    for p in proj_rows:
        project_id = p["project_id"]
        proj_number = p["project_number"] or ""
        task_order  = p["task_order_number"] or ""

        # Days per period
        rtc_id_val = p["rtc_id"] if "rtc_id" in p.keys() else None
        days_key   = (project_id, rtc_id_val)
        period_days = {}
        future_days = 0.0
        today_ps    = date.today().replace(day=1).isoformat()
        for period in periods:
            ps    = period["period_start"]
            label = period["label"]
            d = round(proj_days_map.get(days_key, {}).get(ps, 0), 2)
            period_days[label] = d
            if ps >= today_ps:
                future_days += d

        # Horizon status based on project_status and project_type
        _ptype   = (p["project_type"] or "").strip()
        _pstat   = (p["project_status"] or "").strip().lower()
        _pnum    = (p["project_number"] or "").strip()
        _special = _pnum in {"ID-06", "ID-04", "IDUK-01"}
        if _special:
            horizon_status = "other"
        elif _pstat == "active" and _ptype == "UK Direct":
            horizon_status = "linked"
        elif _pstat == "active" and _ptype == "UK Opportunity":
            horizon_status = "opportunity"
        elif _pstat == "active":
            horizon_status = "other"
        else:
            horizon_status = "norecord"

        projects_list.append({
            "project_id":       project_id,
            "rtc_id":           p["rtc_id"] if "rtc_id" in p.keys() else None,
            "number":           proj_number,
            "task_order":       task_order,
            "display_project_number": display_number(proj_number),
            "display_task_order":     display_number(task_order),
            "is_placeholder_number":  (is_suffixed(proj_number) or
                                       is_placeholder(proj_number)),
            "name":             p["project_name"] or "No Horizon Record Found",
            "task_name":        p["task_name"] or "No Horizon Record Found",
            "organisation":     p["project_organisation"] or "No Horizon Record Found",
            "horizon_status":   horizon_status,
            "project_type":     p["project_type"] or "",
            "department":       p["department"] if "department" in p.keys() else "",
            "pm":               p["project_manager"],
            "director":         p["project_director"],
            "task_start_date":  p["task_start_date"],
            "task_end_date":    p["task_end_date"],
            "reporting_period": p["reporting_period"],
            "start_date":       p["start_date"] if "start_date" in p.keys() else None,
            "last_updated_at":  p["last_updated_at"] if "last_updated_at" in p.keys() else None,
            "last_imported":    p["last_imported"],
            "total_days":       period_days,
            "future_days":      round(future_days, 2),
        })

    # -- Assemble final payload ----------------------------------------------
    summary = {
        "generated_at":  generated_at,
        "periods":       [p["label"] for p in periods],
        "working_days":  working_days,
        "staff":         staff_list,
        "projects":      projects_list,
        "departments":   _get_departments(conn),
        "job_functions": _get_job_functions(conn),
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


def _get_departments(conn):
    rows = conn.execute("""
        SELECT DISTINCT department
        FROM staff
        WHERE department IS NOT NULL
        AND department != '_GENERIC'
        ORDER BY department
    """).fetchall()
    return [{"department": r["department"]} for r in rows]


def _get_job_functions(conn):
    rows = conn.execute("""
        SELECT DISTINCT job_function
        FROM staff
        WHERE job_function IS NOT NULL
        ORDER BY job_function
    """).fetchall()
    return [{"job_function": r["job_function"]} for r in rows]
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

import threading
import time

_dirty       = False
_dirty_lock  = threading.Lock()
_worker_started = False

def mark_dirty():
    """Signal that the summary needs rebuilding."""
    global _dirty
    with _dirty_lock:
        _dirty = True

def _rebuild_worker():
    """Background worker — rebuilds at most once every 3 seconds when dirty."""
    global _dirty
    while True:
        time.sleep(3)
        with _dirty_lock:
            if not _dirty:
                continue
            _dirty = False
        try:
            build()
        except Exception as e:
            logging.getLogger("resource_forecast").error(f"Summary rebuild error: {e}")

def start_worker():
    """Start the background rebuild worker (called once at app startup)."""
    global _worker_started
    if _worker_started:
        return
    _worker_started = True
    t = threading.Thread(target=_rebuild_worker, daemon=True)
    t.start()