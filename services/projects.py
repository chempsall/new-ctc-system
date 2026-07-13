"""
services/projects.py
Everything that knows about project identity: placeholder numbers,
the timestamp-suffix scheme, and get-or-create semantics.

This module is the single owner of the suffix scheme. The frontend
should never need to parse project numbers — API responses should use
display_number() / is_suffixed() to send display-ready fields.
"""

import re

PLACEHOLDER_PATTERNS = {"xxxxxxxx", "12345678", "00000000", "tbc", "tbd", "n/a", ""}

# Matches the timestamp suffix appended by get_or_create_project,
# e.g. "9081_20260706T09054112345"
PLACEHOLDER_SUFFIX = re.compile(r"_\d{8}T\d+$")


def is_placeholder(s: str) -> bool:
    """True if the string is a known placeholder project/task number."""
    if not s:
        return True
    c = s.lower().strip()
    if c in PLACEHOLDER_PATTERNS:
        return True
    if all(ch == "x" for ch in c) or all(ch == "0" for ch in c):
        return True
    return False


def is_suffixed(s: str) -> bool:
    """True if the string carries the collision-avoidance timestamp suffix."""
    return bool(s and PLACEHOLDER_SUFFIX.search(s))


def display_number(s: str):
    """
    The user-facing form of a project/task number:
    suffix stripped, or None if nothing remains but a placeholder.
    """
    if not s:
        return None
    clean = PLACEHOLDER_SUFFIX.sub("", s)
    return None if is_placeholder(clean) else clean


def get_or_create_project(cursor, data: dict, now: str) -> int:
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
    if not is_placeholder(proj_num) and task_order:
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
