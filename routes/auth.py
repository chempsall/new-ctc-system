"""
routes/auth.py
Passwordless identity for alpha testing (Option A).

This is identification, not authentication: the user picks who they are
from the active staff list and a signed session cookie remembers it.
Acceptable for a trusted internal network during alpha; the login route
is the ONLY thing that changes when Entra ID SSO replaces it (Option B) —
the session shape and get_current_user() stay identical.
"""

import logging
from datetime import datetime, timezone

from flask import (Blueprint, render_template, request, redirect,
                   session, url_for)

import database

logger = logging.getLogger("resource_forecast.auth")

auth_bp = Blueprint("auth", __name__)


def _active_staff(conn):
    today = datetime.now(timezone.utc).date().isoformat()
    return conn.execute("""
        SELECT horizon_person_number, name, job_title
        FROM staff
        WHERE horizon_person_number NOT LIKE 'GENERIC-%%'
        AND (end_date IS NULL OR end_date > %s)
        AND (start_date IS NULL OR start_date <= %s)
        ORDER BY name
    """, (today, today)).fetchall()


@auth_bp.route("/login", methods=["GET"])
def login_page():
    conn = database.get_connection()
    staff = _active_staff(conn)
    conn.close()
    return render_template("login.html", staff=staff, error=None)


@auth_bp.route("/login", methods=["POST"])
def login_submit():
    pid = (request.form.get("horizon_person_number") or "").strip()
    conn = database.get_connection()
    row = conn.execute("""
        SELECT horizon_person_number, name FROM staff
        WHERE horizon_person_number = %s
        AND horizon_person_number NOT LIKE 'GENERIC-%%'
        AND (end_date IS NULL OR end_date > TO_CHAR(CURRENT_DATE, 'YYYY-MM-DD'))
    """, (pid,)).fetchone()

    if not row:
        staff = _active_staff(conn)
        conn.close()
        return render_template("login.html", staff=staff,
                               error="Please select your name from the list."), 400
    conn.close()

    session.permanent = True
    session["user"] = {
        "name":          row["name"],
        "person_number": row["horizon_person_number"],
    }
    logger.info(f"Login: {row['name']} ({row['horizon_person_number']})")

    nxt = request.form.get("next") or "/"
    # Only allow same-site relative redirects
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = "/"
    return redirect(nxt)


@auth_bp.route("/logout")
def logout():
    user = session.pop("user", None)
    if user:
        logger.info(f"Logout: {user.get('name')}")
    return redirect(url_for("auth.login_page"))
