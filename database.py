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
    # GRADE RATES
    # Used only for server-side financial calculations.
    # Never returned by any API endpoint.
    # Keyed on job_title which is the same as Horizon's technical grade.
    # Rates will need extending when offices beyond London are added.
    # ------------------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS grade_rates (
            job_title       TEXT PRIMARY KEY,
            raw_cost        REAL NOT NULL,
            burdened_cost   REAL NOT NULL,
            utilisation     REAL NOT NULL
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
    # Pure Horizon/PAR data only.
    # No department, team, or CTC-specific columns here.
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
            budget_baseline_date    TEXT,
            funding_value           REAL,
            current_budget_dlm      REAL,
            current_budget_raw_labor REAL,
            current_budget_nr       REAL,
            actual_itd_dlm          REAL,
            actual_itd_raw_labor    REAL,
            actual_itd_nr           REAL,
            actual_period_dlm       REAL,
            actual_period_raw_labor REAL,
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
            project_id              INTEGER NOT NULL REFERENCES projects(project_id),
            department              TEXT    NOT NULL,
            ctc_start_date          TEXT,
            file_path               TEXT    NOT NULL UNIQUE,
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
            ctc_id                  INTEGER NOT NULL REFERENCES ctc_files(ctc_id),
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
            label           TEXT NOT NULL UNIQUE,
            financial_year  TEXT NOT NULL
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
    _seed_grade_rates(c)
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


def _seed_grade_rates(c):
    """
    Grade rates for server-side financial calculations.
    Keyed on job_title (Horizon's technical grade field).
    London rates only for now — will need extending for other offices.
    """
    rates = [
        ("L3 - Director",                               532.07, 1261.008, 0.67),
        ("P7 - Director",                               455.75, 1080.119, 0.75),
        ("P6 - Technical Director",                     338.91,  803.208, 0.87),
        ("P5 - Associate/Associate Director",           266.46,  631.512, 0.87),
        ("P4 - Principal Engineer/Consultant",          221.52,  525.000, 0.93),
        ("P3 - Senior Engineer/Consultant",             183.59,  435.120, 0.93),
        ("P2 - Engineer/Consultant",                    146.95,  348.264, 0.93),
        ("P1 - Graduate/Assistant Engineer/Consultant", 119.42,  283.035, 0.93),
        ("P0 - Undergraduate Engineer/Consultant",       94.86,  224.815, 0.93),
        ("T4 - Senior Technician",                      195.36,  463.008, 0.93),
        ("T3 - Experienced Technician",                 195.72,  463.848, 0.93),
        ("T2 - Intermediate Technician",                103.21,  244.600, 0.93),
        ("T1 - Assistant Technician",                    68.98,  163.477, 0.93),
        ("T0 - Technician in Training",                  95.42,  226.154, 0.93),
    ]
    for job_title, raw, burdened, util in rates:
        c.execute("""
            INSERT OR IGNORE INTO grade_rates
                (job_title, raw_cost, burdened_cost, utilisation)
            VALUES (?, ?, ?, ?)
        """, (job_title, raw, burdened, util))


def _seed_reporting_periods(c):
    QUARTER_START = {1, 4, 7, 10}
    current = date(2025, 1, 1)
    end     = date(2029, 12, 1)
    while current <= end:
        m   = current.month
        y   = current.year
        nxt = current + relativedelta(months=1)
        fy  = f"{y}-{str(y+1)[-2:]}" if m >= 4 else f"{y-1}-{str(y)[-2:]}"
        c.execute("""
            INSERT OR IGNORE INTO reporting_periods
                (period_start, period_end, working_days, label, financial_year)
            VALUES (?, ?, ?, ?, ?)
        """, (current.isoformat(), (nxt - timedelta(days=1)).isoformat(),
              25 if m in QUARTER_START else 20,
              current.strftime("%b-%Y"), fy))
        current = nxt


if __name__ == "__main__":
    config.summary()
    initialise_database()
