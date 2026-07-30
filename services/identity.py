"""
services/identity.py
Who is making the request.

Reads the signed session set by routes/auth.py. When Entra ID SSO
replaces the login page, only routes/auth.py changes — the session
shape and these functions stay identical.
"""

from flask import session, has_request_context

import secrets
from functools import wraps

from flask import request, abort

import config


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


def require_admin(f):
    """Decorator: requires the admin bearer token in the Authorization header."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not config.ADMIN_TOKEN:
            abort(503, description=(
                "Admin token not configured. "
                "Set RF_ADMIN_TOKEN in your .env file."
            ))
        auth = request.headers.get("Authorization", "")
        if not secrets.compare_digest(auth, f"Bearer {config.ADMIN_TOKEN}"):
            abort(403)
        return f(*args, **kwargs)
    return decorated
