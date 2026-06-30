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
    # Maintained here rather than in a spreadsheet.
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
    # department   = Horizon cost centre, replaces office
    #                e.g. "UK010117"
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
    # The staff.availability column holds the default (1.0 for full time).
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
    # Pure Horizon/PAR project identity data — no financial figures,
    # no department, team, or CTC-specific columns here.
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
    # CTC FILES
    # One row per CTC Excel file pushed to the system.
    # Represents one team's engagement with a project/task.
    # department replaces office as the grouping field.
    # ------------------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS ctc_files (
            ctc_id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            ctc_guid                TEXT    NOT NULL UNIQUE,
            project_id              INTEGER NOT NULL REFERENCES projects(project_id),
            department              TEXT    NOT NULL,
            ctc_start_date          TEXT,
            file_path               TEXT    NOT NULL,
            conflict_flag           INTEGER NOT NULL DEFAULT 0,
            start_date_changed      INTEGER NOT NULL DEFAULT 0,
            previous_ctc_start_date TEXT,
            last_pushed             TEXT,
            last_updated_by         TEXT
        )
    """)

    # ------------------------------------------------------------------
    # ALLOCATIONS
    # The resource forecast. One row per person x CTC file x month.
    # ------------------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS allocations (
            allocation_id           INTEGER PRIMARY KEY AUTOINCREMENT,
            horizon_person_number   TEXT    NOT NULL REFERENCES staff(horizon_person_number),
            ctc_id                  INTEGER NOT NULL REFERENCES ctc_files(ctc_id) ON DELETE CASCADE,
            period_start            TEXT    NOT NULL,
            days                    REAL    NOT NULL DEFAULT 0,
            pushed_at               TEXT    NOT NULL,
            UNIQUE(horizon_person_number, ctc_id, period_start)
        )
    """)

    # ------------------------------------------------------------------
    # REPORTING PERIODS
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
    # AUDIT
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
    end     = date(2029, 12, 1)
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
