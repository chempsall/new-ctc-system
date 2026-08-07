"""
app.py
Resource Forecast — Flask application entry point.

All configuration comes from config.py (which reads from .env and
environment variables). Route handlers live in routes/, shared
business logic in services/.

To start the development server:
    python app.py
"""

import logging
from datetime import datetime, timedelta, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

# .env must be loaded BEFORE config is imported — config reads
# os.environ at import time.
try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).parent / ".env"
    if _env_path.exists():
        load_dotenv(_env_path, override=True)
        print(f"Loaded configuration from {_env_path}")
    else:
        print("No .env file found — using environment variables and config.py defaults.")
except ImportError:
    print("python-dotenv not installed — using environment variables only.")

import config

try:
    config.validate()
except ValueError as e:
    print(f"\n{'='*60}")
    print("CONFIGURATION ERROR — cannot start the application")
    print('='*60)
    print(e)
    print('='*60)
    raise SystemExit(1)

from flask import Flask, jsonify
from apscheduler.schedulers.background import BackgroundScheduler

import database
import summary as summary_module


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def _setup_logging():
    log_dir = Path(config.BASE_DIR) / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / "app.log"

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Rotating handler — new file each day, keep 28 days
    fh = TimedRotatingFileHandler(
        log_file, when="midnight", backupCount=28,
        encoding="utf-8", utc=True
    )
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)

    # Console handler for dev
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    ch.setLevel(logging.INFO)

    root = logging.getLogger("resource_forecast")
    root.setLevel(logging.DEBUG)
    root.addHandler(fh)
    root.addHandler(ch)
    return root


logger = _setup_logging()

# Routes and jobs import config/database, so they come after setup.
from routes.auth import auth_bp
from routes.dashboard import dashboard_bp
from routes.rtcs import rtcs_bp
from routes.admin import admin_bp
from services.jobs import nightly_imports

app = Flask(__name__)
app.secret_key = config.SECRET_KEY
# Sessions are deliberately non-permanent (see routes/auth.py): the cookie
# lasts only as long as the browser is open, so this lifetime is not used.

app.register_blueprint(auth_bp)
app.register_blueprint(dashboard_bp)
app.register_blueprint(rtcs_bp)
app.register_blueprint(admin_bp)


from flask import session, request, redirect

def _expire_admin_outside_admin_area():
    """
    Admin rights last only while the user is actually in the admin area.

    is_admin lives in the same signed cookie as the user session, which
    survives closing a tab (the cookie only dies with the browser). Without
    this, signing in as an administrator once left the flag set for the rest
    of the day — a new tab could open /admin with no password. Loading any
    ordinary page now stands the user back down, which is the same thing the
    "Back to dashboard" link does.

    Only top-level page loads count: the admin page legitimately fetches
    non-admin endpoints such as /api/rtcs, and those must not clear the flag
    mid-use.
    """
    if not session.get("is_admin"):
        return
    if request.path.startswith("/admin"):
        return
    dest = request.headers.get("Sec-Fetch-Dest")
    is_page = (dest == "document") if dest else \
              ("text/html" in request.headers.get("Accept", ""))
    if is_page:
        session.pop("is_admin", None)
        logger.info(f"Admin rights stood down on leaving the admin area: "
                    f"{session.get('user', {}).get('name', 'unknown')}")


@app.before_request
def require_identity():
    endpoint = request.endpoint or ""
    if endpoint.startswith("auth.") or endpoint == "static":
        return
    if "user" in session:
        _expire_admin_outside_admin_area()
        return
    if request.path.startswith("/api/") or request.path.startswith("/admin/"):
        # fetch() calls must see a status they can handle, not a login page
        if request.path != "/admin":       # the admin *page* itself redirects
            return jsonify({"error": "Not signed in"}), 401
    return redirect("/login?next=" + request.path)


@app.errorhandler(404)
def handle_404(e):
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(Exception)
def handle_exception(e):
    """Return JSON for all unhandled exceptions."""
    import traceback
    from werkzeug.exceptions import HTTPException
    # Intentional HTTP errors (abort(403), 405, 503...) must keep their
    # status code — without this branch they all surface as 500s.
    if isinstance(e, HTTPException):
        return jsonify({"error": e.description}), e.code
    logger.error(f"Unhandled exception: {e}\n{traceback.format_exc()}")
    if config.FLASK_DEBUG:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500
    return jsonify({"error": "An unexpected error occurred"}), 500


# ---------------------------------------------------------------------------
# STARTUP
# ---------------------------------------------------------------------------

def create_app():
    database.initialise_database()
    summary_module.build()
    summary_module.start_worker()

    # IMPORTANT: The scheduler and summary worker thread are in-process globals.
    # Run only ONE worker process (not multiple gunicorn workers) or the nightly
    # import will execute N times concurrently against SQLite.
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        nightly_imports,
        trigger="cron",
        hour=config.SCHEDULER_HOUR,
        minute=config.SCHEDULER_MINUTE,
        id="nightly_imports",
        replace_existing=True
    )
    scheduler.start()
    return app


if __name__ == "__main__":
    print("\nResource Forecast")
    print("=" * 40)
    config.summary()
    print("=" * 40 + "\n")

    application = create_app()
    application.run(
        host=config.FLASK_HOST,
        port=config.FLASK_PORT,
        debug=config.FLASK_DEBUG,
        use_reloader=False
    )