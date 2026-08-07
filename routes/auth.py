"""
routes/auth.py
Passwordless identity for alpha testing (Option A).

This is identification, not authentication: the user picks who they are
from the active staff list and a signed session cookie holds it for the
lifetime of the browser session only.
Acceptable for a trusted internal network during alpha; the login route
is the ONLY thing that changes when Entra ID SSO replaces it (Option B) —
the session shape and get_current_user() stay identical.
"""

import logging
from datetime import datetime, timezone

from flask import (Blueprint, jsonify, render_template, request, redirect,
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

    # A browser-session cookie, not a persistent one. Flask's default lifetime
    # for a permanent session is 31 days, which meant a tester was silently
    # remembered for a month. Every change is attributed to whoever is signed
    # in, so people must say who they are each time they open the app rather
    # than inheriting someone else's identity on a shared machine.
    session.permanent = False
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


@auth_bp.route("/api/session/visit", methods=["POST"])
def session_visit():
    """
    Records the start of a visit.

    A browser that still holds the session does not go through the login page,
    so opening the app again later would leave no trace of who was using it.
    The browser reports each new window/tab it opens the app in (tracked with
    sessionStorage, which is discarded when that window closes), giving one
    line per visit however long the gap — five minutes or five hours.
    """
    user = session.get("user")
    if not user:
        return jsonify({"error": "Not signed in"}), 401
    logger.info(f"Visit started: {user.get('name')} ({user.get('person_number')})")
    return jsonify({"status": "ok"})


@auth_bp.route("/logout")
def logout():
    user = session.pop("user", None)
    if user:
        logger.info(f"Logout: {user.get('name')}")
    return redirect(url_for("auth.login_page"))