"""
services/audit.py
Who changed what.

This is deliberately separate from the application log. app.log is
diagnostic — errors, job timings, sign-ins — and rotates every 28 days, so it
cannot answer "who deleted that RTC last quarter". Audit entries live in a
table so they are durable, queryable by person or by RTC, and can be shown
next to the RTC itself as well as in the admin area.

Recording an event must never break the action that caused it: a failure here
is logged and swallowed. An audit trail that can take the app down is worse
than one with an occasional gap.
"""

import logging
from datetime import datetime, timezone

import database
from services.identity import get_current_user, get_current_person_number

logger = logging.getLogger("resource_forecast.audit")


# Actions. Kept as constants so the vocabulary stays consistent and a typo
# cannot silently create a new category that nothing filters on.
RTC_CREATED        = "RTC created"
RTC_DELETED        = "RTC deleted"
RTC_ARCHIVED       = "RTC archived"
RTC_RESTORED       = "RTC restored"
RTC_LINKED         = "Linked to Horizon"
RTC_CONVERTED      = "Converted to live project"
RTC_DETAILS_EDITED = "Details edited"
ALLOCATIONS_EDITED = "Allocations changed"
NOTES_EDITED       = "Notes edited"
ADMIN_ACTION       = "Admin action"


def describe_rtc(conn, rtc_id):
    """
    A human label for an RTC: "UK0041867/9081 — Riverside Bridge".

    Captured at the time of the event and stored with it, because an RTC's
    project number can change (an opportunity converting to a live project),
    and history should read as it was, not as things later became.
    """
    if not rtc_id:
        return None
    try:
        row = conn.execute("""
            SELECT p.project_number, p.task_order_number, p.project_name
            FROM rtcs r
            JOIN projects p ON p.project_id = r.project_id
            WHERE r.rtc_id = %s
        """, (rtc_id,)).fetchone()
    except Exception:
        return None
    if not row:
        return None
    number = "/".join(x for x in (row["project_number"], row["task_order_number"]) if x)
    name   = row["project_name"] or ""
    return f"{number} — {name}".strip(" —") or None


def record(action, rtc_id=None, rtc_description=None, detail=None, conn=None):
    """
    Writes one audit entry.

    Pass an existing connection when the caller is mid-transaction so the entry
    commits with the change it describes; otherwise a connection is opened and
    committed here.
    """
    own_conn = conn is None
    try:
        if own_conn:
            conn = database.get_connection()
        if rtc_description is None and rtc_id:
            rtc_description = describe_rtc(conn, rtc_id)
        conn.execute("""
            INSERT INTO audit_log
                (occurred_at, person_name, person_number, action,
                 rtc_id, rtc_description, detail)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (datetime.now(timezone.utc).isoformat(),
              get_current_user(), get_current_person_number(),
              action, rtc_id, rtc_description, detail))
        if own_conn:
            conn.commit()
    except Exception as e:
        # Never let auditing break the thing being audited.
        logger.error(f"Failed to record audit entry ({action}): {e}")
    finally:
        if own_conn and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def summarise_allocation_changes(changes):
    """
    One readable line for a batch of allocation edits.

    The grid saves many cells at once, so an entry per cell would bury
    everything else. "3 people, 12 periods, net +45.0 days" answers the
    question people actually ask without the noise.
    """
    if not changes:
        return None
    people  = {c["person"] for c in changes}
    net     = sum(c["days"] - c["was"] for c in changes)
    sign    = "+" if net >= 0 else ""
    return (f"{len(people)} {'person' if len(people) == 1 else 'people'}, "
            f"{len(changes)} {'period' if len(changes) == 1 else 'periods'}, "
            f"net {sign}{round(net, 1)} days")