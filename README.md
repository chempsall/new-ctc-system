# Resource Forecast

Web-based resourcing system for WSP London Building Services. Replaced the
Excel/macro-based CTC system with a web application where RTCs (Resource to
Complete) are created and edited directly in the browser.

## Setup

```
pip install -r requirements.txt
copy .env.template .env        # then edit .env with your paths
python database.py
python imports\staff_list.py source-data\staff_list.xlsx
python imports\par_import.py
python app.py
```

Open `http://localhost:5000` in your browser.

## Structure

| Path | Purpose |
|---|---|
| `app.py` | Flask server and all API endpoints |
| `database.py` | Schema definition and seed data |
| `summary.py` | Pre-built JSON cache for the dashboard |
| `imports/` | Staff list and PAR data importers |
| `source-data/` | Local data files — not committed to git |
| `data/` | SQLite database — not committed to git |
| `static/` | CSS, JavaScript, images |
| `templates/` | Jinja HTML templates |

## Data sources

- **Staff list**: `source-data/staff_list.xlsx` — imported manually when the
  staff list changes
- **PAR actuals**: Most recent `YYYYMM_UK_PAR_*.xlsx` in `source-data/`,
  or direct SharePoint connection when `RF_PAR_USE_SHAREPOINT=true`

Both imports run automatically every night and can be triggered manually
from the Admin page.

## Architecture notes

- RTCs are database rows, not files. There is no Excel template or macro.
  Identity is server-assigned (`rtc_id`) — no GUID or file-path based
  identity, so duplicating, renaming, or moving is not a concern.
- The dashboard summary is pre-calculated and cached on every import or
  RTC save. All filtering happens in JavaScript against that cache — zero
  additional server requests during normal browsing.
- Authentication is a placeholder (`get_current_user()` in `app.py`).
  When this system moves to the WSP corporate environment, replace that
  single function with the appropriate corporate auth mechanism.
