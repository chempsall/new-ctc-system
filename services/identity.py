"""
services/identity.py
Who is making the request.

Reads the signed session set by routes/auth.py. When Entra ID SSO
replaces the login page, only routes/auth.py changes — the session
shape and these functions stay identical.
"""

from flask import session, has_request_context

import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import request, abort

import config

# Admin rights lapse after this long without an admin request, so an
# administrator who wanders off (or closes the tab without using the exit
# link) does not leave the area unlocked for the rest of the browser session.
ADMIN_IDLE_MINUTES = 15


def get_current_user() -> str:
    """Returns the current user's display name."""
    if has_request_context():
        u = session.get("user")
        if u:
            return u.get("name", "Unknown")
    return "System"


def get_current_person_number():
    """Returns the current user's Horizon person number, or None."""
    if has_request_context():
        u = session.get("user")
        if u:
            return u.get("person_number")
    return None


def is_admin() -> bool:
    """
    True when this browser session is a verified administrator and has been
    active in the admin area recently. The stamp is refreshed on every admin
    request, so continuous work never expires; a gap does.
    """
    if not (has_request_context() and session.get("is_admin")):
        return False
    seen = session.get("admin_seen")
    now  = datetime.now(timezone.utc)
    if seen:
        try:
            if now - datetime.fromisoformat(seen) > timedelta(minutes=ADMIN_IDLE_MINUTES):
                session.pop("is_admin", None)
                session.pop("admin_seen", None)
                return False
        except (ValueError, TypeError):
            session.pop("is_admin", None)
            return False
    session["admin_seen"] = now.isoformat()
    return True


def check_admin_token(supplied: str) -> bool:
    """Constant-time comparison of a supplied admin token."""
    if not config.ADMIN_TOKEN or not supplied:
        return False
    return secrets.compare_digest(supplied, config.ADMIN_TOKEN)


def require_admin(f):
    """
    Decorator: admin only.

    Accepts either a verified admin session (a person who signed in on the
    admin page) or the bearer token in an Authorization header (scripts and
    other machine callers). The session route means the browser never has to
    hold the token in JavaScript.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if not config.ADMIN_TOKEN:
            abort(503, description=(
                "Admin token not configured. "
                "Set RF_ADMIN_TOKEN in your .env file."
            ))
        if is_admin():
            return f(*args, **kwargs)
        auth = request.headers.get("Authorization", "")
        if secrets.compare_digest(auth, f"Bearer {config.ADMIN_TOKEN}"):
            return f(*args, **kwargs)
        abort(403)
    return decorated