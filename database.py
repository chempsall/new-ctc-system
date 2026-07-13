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
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


from contextlib import contextmanager

@contextmanager
def db():
    """Context manager for database connections.
    Ensures connections are always closed even if an exception occurs.
    Usage: with db() as conn:
    """
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()

def ensure_periods_through(conn, target_date):
    """
    Extend reporting_periods so that target_date's month exists.
    Safe to call multiple times — uses INSERT OR IGNORE.
    """
    from datetime import date as _date
    from dateutil.relativedelta import relativedelta as _rd
    c = conn.cursor()
    last = c.execute(
        "SELECT MAX(period_start) FROM reporting_periods"
    ).fetchone()[0]
    current = (_date.fromisoformat(last) + _rd(months=1)) if last \
              else _date.today().replace(day=1)
    if isinstance(target_date, str):
        target_date = _date.fromisoformat(target_date)
    target = target_date.replace(day=1)
    while current <= target:
        nxt = current + _rd(months=1)
        c.execute("""INSERT OR IGNORE INTO reporting_periods
                     (period_start, period_end, working_days, label)
                     VALUES (?,?,?,?)""",
                  (current.isoformat(),
                   (nxt - timedelta(days=1)).isoformat(),
                   25 if current.month in {1, 4, 7, 10} else 20,
                   current.strftime("%b-%Y")))
        current = nxt
    conn.commit()

def _ensure_column(c, table, column, decl):
    """Add a column to an existing table if it does not already exist.
    Safe to call repeatedly — idempotent.
    """
    cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def initialise_database():
    if DB_PATH is None:
        raise RuntimeError("No SQLite path configured.")
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection()
    c = conn.cursor()

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
            is_archived     INTEGER NOT NULL DEFAULT 0,
            auto_linked     INTEGER NOT NULL DEFAULT 0,
            source_file     TEXT,
            notes           TEXT
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
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_allocations_rtc
        ON allocations(rtc_id, period_start)
    """)
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_allocations_period
        ON allocations(period_start)
    """)

    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_allocations_person
        ON allocations(horizon_person_number, period_start)
    """)

    # Column migrations — safe for pre-existing databases
    _ensure_column(c, "rtcs", "notes",       "TEXT")
    _ensure_column(c, "rtcs", "source_file", "TEXT")
    _ensure_column(c, "rtcs", "auto_linked", "INTEGER NOT NULL DEFAULT 0")

    # Bank holidays cache
    c.execute("""
        CREATE TABLE IF NOT EXISTS bank_holidays (
            date        TEXT PRIMARY KEY,
            days        INTEGER NOT NULL DEFAULT 1,
            last_updated TEXT
        )
    """)
    
    _seed_reporting_periods(c)
    _seed_generic_staff(c)
    conn.commit()
    conn.close()
    print(f"Database initialised at {DB_PATH}")


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

def _seed_generic_staff(c):
    """
    Generic placeholder staff for use in RTCs when a specific person
    hasn't been identified yet. Available across all departments.
    Identified by department = '_GENERIC'.
    """
    generics = [
        ("GENERIC-UK-DIRECTOR", "UK Director", "P7 - Director"),
        ("GENERIC-UK-TECHNICAL-DIRECTOR", "UK Technical Director", "P6 - Technical Director"),
        ("GENERIC-UK-ASSOCIATE-DIRECTOR", "UK Associate/Associate Director", "P5 - Associate/Associate Director"),
        ("GENERIC-UK-PRINCIPAL-ENGINEER", "UK Principal Engineer/Consultant", "P4 - Principal Engineer/Consultant"),
        ("GENERIC-UK-SENIOR-ENGINEER", "UK Senior Engineer/Consultant", "P3 - Senior Engineer/Consultant"),
        ("GENERIC-UK-ENGINEER", "UK Engineer/Consultant", "P2 - Engineer/Consultant"),
        ("GENERIC-UK-GRADUATE-ENGINEER", "UK Graduate/Assistant Engineer/Consultant", "P1 - Graduate/Assistant Engineer/Consultant"),
        ("GENERIC-UK-UNDERGRADUATE-ENGINEER", "UK Undergraduate Engineer", "P0 - Undergraduate Engineer/Consultant"),
        ("GENERIC-UK-SENIOR-TECHNICIAN", "UK Senior Technician", "T4 - Senior Technician"),
        ("GENERIC-UK-EXPERIENCED-TECHNICIAN", "UK Experienced Technician", "T3 - Experienced Technician"),
        ("GENERIC-UK-INTERMEDIATE-TECHNICIAN", "UK Intermediate Technician", "T2 - Intermediate Technician"),
        ("GENERIC-UK-ASSISTANT-TECHNICIAN", "UK Assistant Technician", "T1 - Assistant Technician"),
        ("GENERIC-UK-TECHNICIAN-IN-TRAINING", "UK Technician in Training", "T0 - Technician in Training"),
        ("GENERIC-UK-DOCUMENT-CONTROL", "UK Document Control", "P3 - Senior Engineer/Consultant"),
    ]
    for horizon_id, name, job_title in generics:
        c.execute("""
            INSERT OR IGNORE INTO staff (
                horizon_person_number, name, job_title, job_family,
                job_function, department, availability, last_imported
            ) VALUES (?, ?, ?, 'Generic', 'Generic', '_GENERIC', 1.0, 'seeded')
        """, (horizon_id, name, job_title))


if __name__ == "__main__":
    config.summary()
    initialise_database()
