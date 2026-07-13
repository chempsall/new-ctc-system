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
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

# .env must be loaded BEFORE config is imported — config reads
# os.environ at import time.
try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).parent / ".env"
    if _env_path.exists():
        load_dotenv(_env_path)
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
from routes.dashboard import dashboard_bp
from routes.rtcs import rtcs_bp
from routes.admin import admin_bp
from services.jobs import nightly_imports

app = Flask(__name__)
app.secret_key = config.SECRET_KEY

app.register_blueprint(dashboard_bp)
app.register_blueprint(rtcs_bp)
app.register_blueprint(admin_bp)


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
