"""
database.py
Creates and initialises the SQLite database.
All path configuration comes from config.py.

Run directly to create a fresh database:
    python database.py
"""

import sqlite3
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta
import config

DB_PATH = config.SQLITE_PATH


def get_connection():
    if DB_PATH is None:
        raise RuntimeError("SQLITE_PATH is not set. Check RF_DATABASE_URL in .env")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def initialise_database():
    if DB_PATH is None:
        raise RuntimeError("No SQLite path configured.")
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection()
    c = conn.cursor()

    # ------------------------------------------------------------------
    # DISCIPLINES
    # Lookup table for job functions.
    # Derived from the suffix after the comma in Horizon job titles.
    # e.g. "Lead Professional, Mechanical Engineering" -> "Mechanical Engineering"
    # ------------------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS disciplines (
            discipline_id   INTEGER PRIMARY KEY AUTOINCREMENT,
            discipline_name TEXT    NOT NULL UNIQUE
        )
    """)

    # ------------------------------------------------------------------
    # STAFF
    # Populated from the staff list Excel file (interim solution).
    # Future: direct Horizon API connection.
    #
    # job_title    = Horizon's technical grade field
    #                e.g. "Lead Professional, Mechanical Engineering"
    # job_family   = broad category e.g. "Engineering"
    # job_function = discipline, derived from job title suffix
    #                e.g. "Mechanical Engineering"
    # department   = Horizon cost centre e.g. "UK010117"
    # ------------------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS staff (
            horizon_person_number   TEXT PRIMARY KEY,
            name                    TEXT NOT NULL,
            job_title               TEXT,
            job_family              TEXT,
            job_function            TEXT,
            department              TEXT,
            availability            REAL NOT NULL DEFAULT 1.0,
            start_date              TEXT,
            end_date                TEXT,
            last_imported           TEXT
        )
    """)

    # Per-period availability overrides.
    # Used for joiners, leavers, and temporary part-time arrangements.
    c.execute("""
        CREATE TABLE IF NOT EXISTS staff_availability (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            horizon_person_number   TEXT NOT NULL REFERENCES staff(horizon_person_number),
            period_start            TEXT NOT NULL,
            availability_fraction   REAL NOT NULL,
            UNIQUE(horizon_person_number, period_start)
        )
    """)

    # ------------------------------------------------------------------
    # PROJECTS
    # Pure Horizon/PAR project identity data.
    # No financial figures, no department or team columns here.
    # ------------------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            project_id              INTEGER PRIMARY KEY AUTOINCREMENT,
            project_number          TEXT    NOT NULL,
            task_order_number       TEXT    NOT NULL,
            project_type            TEXT,
            project_name            TEXT,
            task_name               TEXT,
            project_organisation    TEXT,
            project_customer        TEXT,
            project_status          TEXT,
            project_director        TEXT,
            project_manager         TEXT,
            task_start_date         TEXT,
            task_end_date           TEXT,
            reporting_period        TEXT,
            last_imported           TEXT,
            UNIQUE(project_number, task_order_number)
        )
    """)

    # ------------------------------------------------------------------
    # RTCs  (Resource to Complete)
    # One row per RTC. Represents one team's resourcing engagement
    # with a project/task, entered directly via the web interface.
    #
    # Identity notes:
    #   - rtc_id is the sole, server-assigned identity. No GUIDs, no
    #     file paths, no external identifiers. Clean and unambiguous.
    #   - created_by / last_updated_by / last_opened_by are all set
    #     from get_current_user() in app.py, which is a lightweight
    #     placeholder now and will be replaced with proper corporate
    #     auth (e.g. Microsoft SSO) when the system moves to the
    #     WSP corporate environment.
    #
    # Status / lifecycle:
    #   - last_opened is updated every time any user loads the RTC.
    #     An RTC not opened in 30+ days is considered "needs review".
    #   - is_archived is set by the admin Supersede action, which
    #     targets RTCs with no future allocations AND last_opened
    #     more than 30 days ago. Archived RTCs are hidden by default
    #     but never deleted — their data is permanently preserved.
    # ------------------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS rtcs (
            rtc_id          INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id      INTEGER NOT NULL REFERENCES projects(project_id),
            department      TEXT    NOT NULL,
            start_date      TEXT    NOT NULL,
            created_by      TEXT,
            created_at      TEXT,
            last_updated_by TEXT,
            last_updated_at TEXT,
            last_opened_by  TEXT,
            last_opened     TEXT,
            is_archived     INTEGER NOT NULL DEFAULT 0
        )
    """)

    # ------------------------------------------------------------------
    # ALLOCATIONS
    # One row per person x RTC x month. The core resourcing data.
    # Cascade-deletes when the parent RTC is deleted.
    # ------------------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS allocations (
            allocation_id           INTEGER PRIMARY KEY AUTOINCREMENT,
            horizon_person_number   TEXT    NOT NULL REFERENCES staff(horizon_person_number),
            rtc_id                  INTEGER NOT NULL REFERENCES rtcs(rtc_id) ON DELETE CASCADE,
            period_start            TEXT    NOT NULL,
            days                    REAL    NOT NULL DEFAULT 0,
            last_updated            TEXT    NOT NULL,
            UNIQUE(horizon_person_number, rtc_id, period_start)
        )
    """)

    # ------------------------------------------------------------------
    # REPORTING PERIODS
    # Pre-seeded calendar of months with working-day counts.
    # ------------------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS reporting_periods (
            period_id       INTEGER PRIMARY KEY AUTOINCREMENT,
            period_start    TEXT NOT NULL UNIQUE,
            period_end      TEXT NOT NULL,
            working_days    INTEGER NOT NULL,
            label           TEXT NOT NULL UNIQUE
        )
    """)

    # ------------------------------------------------------------------
    # AUDIT / CACHE
    # ------------------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS import_log (
            log_id          INTEGER PRIMARY KEY AUTOINCREMENT,
            import_type     TEXT    NOT NULL,
            filename        TEXT,
            started_at      TEXT    NOT NULL,
            completed_at    TEXT,
            rows_processed  INTEGER DEFAULT 0,
            rows_inserted   INTEGER DEFAULT 0,
            rows_updated    INTEGER DEFAULT 0,
            errors          TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS summary_cache (
            cache_id        INTEGER PRIMARY KEY CHECK (cache_id = 1),
            generated_at    TEXT    NOT NULL,
            payload         TEXT    NOT NULL
        )
    """)

    conn.commit()
    _seed_disciplines(c)
    _seed_reporting_periods(c)
    conn.commit()
    conn.close()
    print(f"Database initialised at {DB_PATH}")


def _seed_disciplines(c):
    """
    Known job functions derived from Horizon job title suffixes.
    Add new entries here as they are discovered.
    """
    disciplines = [
        "Mechanical Engineering",
        "Electrical Engineering",
        "Information Modelling",
        "Building Technology Systems",
        "Plumbing Engineering",
        "Subsector Leadership",
        "Utility Engineering",
    ]
    for d in disciplines:
        c.execute("""
            INSERT OR IGNORE INTO disciplines (discipline_name)
            VALUES (?)
        """, (d,))


def _seed_reporting_periods(c):
    QUARTER_START = {1, 4, 7, 10}
    current = date(2025, 1, 1)
    end     = date(2030, 12, 1)
    while current <= end:
        m   = current.month
        nxt = current + relativedelta(months=1)
        c.execute("""
            INSERT OR IGNORE INTO reporting_periods
                (period_start, period_end, working_days, label)
            VALUES (?, ?, ?, ?)
        """, (current.isoformat(), (nxt - timedelta(days=1)).isoformat(),
              25 if m in QUARTER_START else 20,
              current.strftime("%b-%Y")))
        current = nxt


if __name__ == "__main__":
    config.summary()
    initialise_database()
