# =============================================================================
# config.py
# Central configuration for the Resource Forecast application.
#
# ALL environment-specific settings live here.
# Nothing else in the codebase contains hardcoded paths or hostnames.
#
# USAGE
# -----
# The app reads from environment variables first, then falls back to the
# defaults defined below. To override any setting without editing this file,
# set the corresponding environment variable before starting the app.
#
# ENVIRONMENTS
# ------------
# development  : Running locally on a developer's machine
# beta         : Shared server for team testing
# production   : Employer-hosted production instance
#
# To switch environments, set:
#   Windows:   set RF_ENV=beta
#   Mac/Linux: export RF_ENV=beta
#
# Or add RF_ENV to your .env file (see .env.template).
# =============================================================================

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Detect environment
# ---------------------------------------------------------------------------
ENV = os.environ.get("RF_ENV", "development").lower()

# ---------------------------------------------------------------------------
# Base directory
# The root of the project — all other paths are relative to this.
# Set RF_BASE_DIR to override (useful when the app is deployed to a server
# with a different folder structure).
# ---------------------------------------------------------------------------
BASE_DIR = Path(os.environ.get("RF_BASE_DIR", Path(__file__).parent.resolve()))

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
# Development: SQLite file in the data/ subfolder
# Beta/Production: Set RF_DATABASE_URL to a connection string.
#   SQLite:   sqlite:///path/to/file.db
#   Postgres: postgresql://user:pass@host/dbname  (future)
DATABASE_URL = os.environ.get(
    "RF_DATABASE_URL",
    f"sqlite:///{BASE_DIR / 'data' / 'resource_forecast.db'}"
)

# Convenience: extracted path for SQLite usage
# Will be None if DATABASE_URL points to a non-SQLite database
if DATABASE_URL.startswith("sqlite:///"):
    SQLITE_PATH = Path(DATABASE_URL[len("sqlite:///"):])
else:
    SQLITE_PATH = None

# ---------------------------------------------------------------------------
# Flask
# ---------------------------------------------------------------------------
FLASK_HOST  = os.environ.get("RF_HOST", "127.0.0.1")
FLASK_PORT  = int(os.environ.get("RF_PORT", "5000"))
FLASK_DEBUG = os.environ.get("RF_DEBUG", "true" if ENV == "development" else "false").lower() == "true"
SECRET_KEY  = os.environ.get("RF_SECRET_KEY", "dev-secret-change-in-production")

# Admin bearer token — protects /admin/* routes and import triggers.
# Generate with: python -c "import secrets; print(secrets.token_hex(32))"
# MUST be set via environment variable in beta and production.
ADMIN_TOKEN = os.environ.get("RF_ADMIN_TOKEN", "")

# ---------------------------------------------------------------------------
# Import file paths
# These point to the Excel exports that the nightly job reads.
#
# Development: full Windows paths on your local machine
# Beta/Production: paths on the server, or SharePoint sync paths
#
# All three can be the same file if the exports are combined into one
# workbook (as in the current Resource_Forecast.xlsx).
# ---------------------------------------------------------------------------
STAFF_LIST_PATH = Path(os.environ.get(
    "RF_STAFF_LIST_PATH",
    str(BASE_DIR / "source-data" / "UK010117_Staff_List.xlsx")
))


# PAR actuals source directory.
# In local mode: the importer scans this folder for the most recent UK_PAR*.xlsx file.
# In SharePoint mode: this is ignored — the importer fetches from SharePoint directly.
# Toggle between modes with RF_PAR_USE_SHAREPOINT in your .env file.
PAR_ACTUALS_PATH = Path(os.environ.get(
    "RF_PAR_ACTUALS_PATH",
    str(BASE_DIR / "source-data")
))

# Set to "true" in .env to fetch PAR directly from SharePoint instead of local file.
PAR_USE_SHAREPOINT = os.environ.get("RF_PAR_USE_SHAREPOINT", "false").lower() == "true"

# SharePoint details for future direct PAR connection (not used yet)
# Source: Power Query M code in Resource_Forecast.xlsx
PAR_SHAREPOINT_SITE   = "https://wsponline.sharepoint.com/sites/GB-UKBIISPowerBIData"
PAR_SHAREPOINT_FOLDER = "Shared Documents/Horizon Project Reports/Project Attribute Report (PAR)/"
PAR_FILENAME_PREFIX   = "UK_PAR"

# The three indirect rows the M code hardcodes — training and annual leave.
# These are added to every PAR import regardless of source.
PAR_INDIRECT_ROWS = [
    {
        "project_type":     "UK Indirect",
        "project_number":   "IDUK-01",
        "project_name":     "Learning Day Release & Study Leave",
        "task_number":      "IDUK-01",
        "task_name":        "Learning Day Release & Study Leave",
    },
    {
        "project_type":     "UK Indirect",
        "project_number":   "ID-04",
        "project_name":     "Training Received",
        "task_number":      "ID-04",
        "task_name":        "Training Received",
    },
    {
        "project_type":     "UK Indirect",
        "project_number":   "ID-06",
        "project_name":     "Annual Leave & Public Holiday",
        "task_number":      "ID-06",
        "task_name":        "Annual Leave & Public Holiday",
    },
]

# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
# Time to run the nightly import (24-hour clock, server local time)
SCHEDULER_HOUR   = int(os.environ.get("RF_SCHEDULER_HOUR",   "0"))
SCHEDULER_MINUTE = int(os.environ.get("RF_SCHEDULER_MINUTE", "0"))

# ---------------------------------------------------------------------------
# Application settings
# ---------------------------------------------------------------------------
# How many months ahead the summary covers
FORECAST_HORIZON_MONTHS = int(os.environ.get("RF_FORECAST_MONTHS", "6"))

# Default office for new files (used when no office is specified)
DEFAULT_OFFICE = os.environ.get("RF_DEFAULT_OFFICE", "London - Chancery Lane")

# ---------------------------------------------------------------------------
# SharePoint (future — not used in development)
# ---------------------------------------------------------------------------
# When RF_ENV is "production" and SharePoint is connected via Microsoft Graph,
# these settings replace the file path imports above.
# Leave blank in development.
SHAREPOINT_TENANT_ID  = os.environ.get("RF_SP_TENANT_ID",  "")
SHAREPOINT_CLIENT_ID  = os.environ.get("RF_SP_CLIENT_ID",  "")
SHAREPOINT_CLIENT_SECRET = os.environ.get("RF_SP_CLIENT_SECRET", "")
SHAREPOINT_SITE_URL   = os.environ.get("RF_SP_SITE_URL",   "")

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def validate():
    """
    Call at startup to catch missing critical settings early.
    Raises ValueError with a clear message if anything critical is wrong.
    """
    errors = []

    if ENV in ("beta", "production"):
        if not ADMIN_TOKEN:
            errors.append(
                "RF_ADMIN_TOKEN must be set in beta and production environments.\n"
                "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
            )
        if SECRET_KEY == "dev-secret-change-in-production":
            errors.append(
                "RF_SECRET_KEY must be set to a secure random value in beta and production."
            )

    if SQLITE_PATH and not SQLITE_PATH.parent.exists():
        errors.append(
            f"Database directory does not exist: {SQLITE_PATH.parent}\n"
            f"Create it with: mkdir {SQLITE_PATH.parent}"
        )

    if errors:
        raise ValueError(
            "Configuration errors found:\n\n" +
            "\n\n".join(f"  • {e}" for e in errors)
        )


def summary():
    """Print a human-readable config summary (without secrets)."""
    print(f"  Environment      : {ENV}")
    print(f"  Base directory   : {BASE_DIR}")
    print(f"  Database         : {DATABASE_URL if not SQLITE_PATH else SQLITE_PATH}")
    print(f"  Flask            : {FLASK_HOST}:{FLASK_PORT} (debug={FLASK_DEBUG})")
    print(f"  Admin token      : {'set' if ADMIN_TOKEN else 'NOT SET'}")
    print(f"  Staff list       : {STAFF_LIST_PATH}")
    print(f"  PAR actuals      : {PAR_ACTUALS_PATH}")
    print(f"  Scheduler        : {SCHEDULER_HOUR:02d}:{SCHEDULER_MINUTE:02d} daily")
    print(f"  Forecast months  : {FORECAST_HORIZON_MONTHS}")


if __name__ == "__main__":
    print("Current configuration:")
    summary()
    try:
        validate()
        print("\n  ✓ Configuration valid")
    except ValueError as e:
        print(f"\n  ✗ {e}")