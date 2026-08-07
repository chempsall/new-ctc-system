"""
database.py
Creates and initialises the PostgreSQL database.
Connection configuration comes from config.py (RF_DATABASE_URL).

Run directly to create a fresh schema:
    python database.py
"""

import psycopg2
import psycopg2.extras
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta
import config

DATABASE_URL = config.DATABASE_URL


class ChainableCursor(psycopg2.extras.RealDictCursor):
    """
    RealDictCursor whose execute()/executemany() return the cursor itself,
    matching sqlite3's chainable convention:

        c.execute(sql, params).fetchone()

    psycopg2 cursors return None from execute(), which breaks every
    chained call site in the codebase; this restores the contract.
    """

    def execute(self, sql, params=None):
        # params must stay None when absent: psycopg2 only %-interpolates
        # when a params sequence is supplied, and no-param queries may
        # legitimately contain literal % (e.g. LIKE 'GENERIC-%').
        super().execute(sql, params)
        return self

    def executemany(self, sql, seq):
        super().executemany(sql, seq)
        return self


class Connection:
    """
    Thin wrapper around a psycopg2 connection that preserves the
    sqlite3 calling convention used throughout the codebase:

        conn.execute(sql, params).fetchall()
        c = conn.cursor(); c.execute(sql, params).fetchone()

    psycopg2 connections have no .execute() — only cursors do — so this
    wrapper creates a cursor per execute() call and returns it. All
    cursors are ChainableCursor (a RealDictCursor), so both the chained
    calling style and row["column_name"] access work exactly as sqlite3
    Row/Cursor did.
    """

    def __init__(self, raw):
        self._raw = raw

    def cursor(self):
        return self._raw.cursor(cursor_factory=ChainableCursor)

    def execute(self, sql, params=None):
        cur = self.cursor()
        cur.execute(sql, params)
        return cur

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        self._raw.close()

    @property
    def closed(self):
        return self._raw.closed


def get_connection():
    if not DATABASE_URL or not DATABASE_URL.startswith("postgresql"):
        raise RuntimeError(
            "RF_DATABASE_URL must be a postgresql:// connection string. "
            f"Current value: {DATABASE_URL!r}"
        )
    raw = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    # Force UTF-8 on the client link regardless of the OS/client locale.
    # Without this, a WIN1252-defaulted Windows client can fail to encode
    # non-Western characters (e.g. 'ş') even when the database is UTF-8.
    raw.set_client_encoding("UTF8")
    return Connection(raw)

from contextlib import contextmanager

@contextmanager
def db():
    """Context manager for database connections.
    Rolls back on error (a psycopg2 connection that hits an error is in
    an aborted state until rollback) and always closes.
    Usage: with db() as conn:
    """
    conn = get_connection()
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_periods_through(conn, target_date):
    """
    Extend reporting_periods so that target_date's month exists.
    Safe to call multiple times — uses ON CONFLICT DO NOTHING.
    """
    from datetime import date as _date
    from dateutil.relativedelta import relativedelta as _rd
    c = conn.cursor()
    c.execute("SELECT MAX(period_start) AS m FROM reporting_periods")
    last = c.fetchone()["m"]
    current = (_date.fromisoformat(last) + _rd(months=1)) if last \
              else _date.today().replace(day=1)
    if isinstance(target_date, str):
        target_date = _date.fromisoformat(target_date)
    target = target_date.replace(day=1)
    while current <= target:
        nxt = current + _rd(months=1)
        c.execute("""INSERT INTO reporting_periods
                     (period_start, period_end, working_days, label)
                     VALUES (%s,%s,%s,%s)
                     ON CONFLICT DO NOTHING""",
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
    c.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = %s AND column_name = %s
    """, (table, column))
    if not c.fetchone():
        c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def initialise_database():
    conn = get_connection()
    c = conn.cursor()

    # ------------------------------------------------------------------
    # STAFF
    # Populated from the staff list Excel file (interim solution).
    # Future: direct Horizon API connection.
    #
    # job_title    = Horizon's technical grade field
    #                e.g. "Lead Professional, Mechanical Engineering"
    # job_function = discipline, derived from job title suffix
    #                e.g. "Mechanical Engineering"
    # department   = Horizon cost centre e.g. "UK010117"
    # ------------------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS staff (
            horizon_person_number   TEXT PRIMARY KEY,
            name                    TEXT NOT NULL,
            job_title               TEXT,
            job_function            TEXT,
            line_manager            TEXT,
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
            id                      SERIAL PRIMARY KEY,
            horizon_person_number   TEXT NOT NULL REFERENCES staff(horizon_person_number),
            period_start            TEXT NOT NULL,
            availability_fraction   REAL NOT NULL,
            UNIQUE(horizon_person_number, period_start)
        )
    """)

    # ------------------------------------------------------------------
    # PROJECTS
    # Pure Horizon/PAR project identity data.
    # ------------------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            project_id              SERIAL  PRIMARY KEY,
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
    # One row per RTC — one team's resourcing engagement with a
    # project/task, entered directly via the web interface.
    # Archived RTCs are hidden by default but never deleted.
    # ------------------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS rtcs (
            rtc_id          SERIAL  PRIMARY KEY,
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
            notes           TEXT,
            last_edited_by  TEXT,
            last_edited_at  TEXT
        )
    """)

    # ------------------------------------------------------------------
    # ALLOCATIONS
    # One row per person x RTC x month. The core resourcing data.
    # Cascade-deletes when the parent RTC is deleted.
    # ------------------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS allocations (
            allocation_id           SERIAL  PRIMARY KEY,
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
            period_id       SERIAL PRIMARY KEY,
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
            log_id          SERIAL  PRIMARY KEY,
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
        CREATE TABLE IF NOT EXISTS audit_log (
            audit_id        SERIAL  PRIMARY KEY,
            occurred_at     TEXT    NOT NULL,
            person_name     TEXT,
            person_number   TEXT,
            action          TEXT    NOT NULL,
            rtc_id          INTEGER,
            -- The RTC's identity AS IT WAS when the event happened. Looking it
            -- up later would relabel history after a conversion changes the
            -- project number, quietly rewriting the past.
            rtc_description TEXT,
            detail          TEXT
        )
    """)
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_audit_occurred ON audit_log(occurred_at DESC)
    """)
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_audit_rtc ON audit_log(rtc_id)
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

    # Column migrations — no-ops on a fresh schema, safe on an old one
    _ensure_column(c, "rtcs", "notes",       "TEXT")
    _ensure_column(c, "rtcs", "source_file", "TEXT")
    _ensure_column(c, "rtcs", "auto_linked", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(c, "staff", "line_manager",  "TEXT")
    _ensure_column(c, "rtcs", "last_edited_by", "TEXT")
    _ensure_column(c, "rtcs", "last_edited_at", "TEXT")

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
    print(f"Database initialised ({DATABASE_URL.split('@')[-1] if '@' in DATABASE_URL else 'postgresql'})")


def _seed_reporting_periods(c):
    QUARTER_START = {1, 4, 7, 10}
    current = date(2025, 1, 1)
    end     = date(2030, 12, 1)
    while current <= end:
        m   = current.month
        nxt = current + relativedelta(months=1)
        c.execute("""
            INSERT INTO reporting_periods
                (period_start, period_end, working_days, label)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT DO NOTHING
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
            INSERT INTO staff (
                horizon_person_number, name, job_title,
                job_function, department, availability, last_imported
            ) VALUES (%s, %s, %s, 'Generic', '_GENERIC', 1.0, 'seeded')
            ON CONFLICT DO NOTHING
        """, (horizon_id, name, job_title))


if __name__ == "__main__":
    config.summary()
    initialise_database()