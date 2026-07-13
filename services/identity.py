"""
services/identity.py
Who is making the request, and is it an admin?

get_current_user() is a lightweight placeholder — replace this single
function when WSP corporate auth is available (Microsoft SSO or
equivalent). Every part of the codebase that needs to know "who is
doing this" calls get_current_user() and nothing else.
"""

import secrets
from functools import wraps

from flask import request, abort

import config


def get_current_user() -> str:
    """Returns the current user's display name."""
    # TODO: replace with real auth when corporate SSO is available
    return "Test User"


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
