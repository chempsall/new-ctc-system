# WSP UK Project Waypoint — Resource Forecast

Web-based resourcing system for WSP UK Building Services. Replaces the
Excel/macro-based CTC system: RTCs (Resource to Complete) are database records
created and edited directly in the browser, with staff and project data
imported nightly from Horizon.

Currently in **alpha**, running against a local PostgreSQL instance. See
"Deployment notes" below for what a hosted environment needs.

---

## Requirements

| | |
|---|---|
| Python | 3.11 or later |
| PostgreSQL | 16 or later (developed against 16, tested on 18) |
| Encoding | The database **must** be UTF-8 — see Deployment notes |

## Setup

```
pip install -r requirements.txt
copy .env.template .env          # then edit — see Configuration below
python app.py
```

The schema is created automatically on first start; there is no separate
migration step. Open `http://localhost:5000`.

To populate it, sign in and use the Admin page to run the staff list and PAR
imports, or wait for the overnight job.

## Configuration

All settings live in `.env` (never committed). The important ones:

| Variable | Purpose |
|---|---|
| `RF_DATABASE_URL` | `postgresql://user:password@host:5432/resource_forecast` |
| `RF_ADMIN_TOKEN` | Password for the admin area. Required — admin pages return 503 without it |
| `RF_STAFF_LIST_PATH` | Full path to the HR staff-list workbook |
| `RF_PAR_ACTUALS_PATH` | Folder holding the PAR exports |
| `RF_SCHEDULER_HOUR` / `RF_SCHEDULER_MINUTE` | When the nightly import runs (default 08:00) |
| `RF_ENV` | `development`, `beta` or `production` |

`.env` is read once at startup, so changes need a restart.

## Structure

| Path | Purpose |
|---|---|
| `app.py` | Application setup, logging, scheduler, request gate |
| `config.py` | Settings, version number and changelog |
| `database.py` | Schema, connection handling, seed data |
| `summary.py` | Pre-built JSON cache behind the dashboard |
| `routes/` | HTTP endpoints — `dashboard`, `rtcs`, `admin`, `auth` |
| `services/` | Domain logic — identity, jobs, projects, special RTCs, audit |
| `imports/` | Staff list, PAR and bank-holiday importers |
| `templates/`, `static/` | Jinja templates, CSS, JavaScript, images |
| `source-data/` | Local data files — **not committed** |
| `logs/` | Rotating application log — **not committed** |

## Data sources

- **Staff list** — the HR export named by `RF_STAFF_LIST_PATH`.
- **PAR actuals** — the most recent `YYYYMM…UK_PAR….xlsx` in the PAR folder.
  The filename must start with a six-digit `YYYYMM` and contain `UK_PAR`;
  the highest date prefix wins, so older files can be left in place.
  A direct SharePoint connection exists behind `RF_PAR_USE_SHAREPOINT=true`
  but has not been exercised in this deployment.

Only rows with `Project Status = Active` are imported, so a closed project is
absent rather than present-and-closed.

Both imports run nightly and can be triggered from the Admin page.

## Architecture notes

- **RTCs are database rows, not files.** Identity is server-assigned
  (`rtc_id`); duplicating, renaming or moving a file is not a concern.
- **One PAR project/task = one RTC**, enforced on creation, on linking and on
  conversion. An archived RTC still holds the claim.
- **The dashboard summary is pre-calculated** and cached on every import and
  RTC save. Filtering happens in the browser against that cache, so normal
  navigation makes no further server requests.
- **The nightly job isolates its steps**: one failure is logged and the
  remainder still run.
- **Authentication is a placeholder.** `services/identity.py` holds
  `get_current_user()`; users pick their name from the staff list, with no
  password. When this moves to the corporate environment, that one function is
  the seam to replace with SSO. The admin area has its own separate password
  (`RF_ADMIN_TOKEN`).
- **An audit trail** (`services/audit.py`, `audit_log` table) records who
  created, linked, converted, deleted or re-forecast each RTC. It is kept
  indefinitely, unlike the application log, which rotates after 28 days.

## Deployment notes

Points that matter when this is hosted:

- **The database must be UTF-8.** A Windows `initdb` defaults to WIN1252,
  which cannot store names containing characters such as `ş` and will fail the
  import. Create with
  `CREATE DATABASE ... ENCODING 'UTF8' TEMPLATE template0;`
- **Run as a single process.** The scheduler and summary worker are in-process;
  multiple workers would run the nightly import several times over.
- **Back up with `pg_dump`**, not by copying files.
- **The nightly import takes roughly 30–60 seconds** on ~150,000 PAR rows and
  is parse-bound (reading the spreadsheet), not database-bound.
- `RF_ADMIN_TOKEN` must be set to a real secret; the admin area is disabled
  without it.
- Outbound HTTPS to `gov.uk` is used once a night for the bank-holiday
  calendar. The import tolerates failure.