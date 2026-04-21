# New CTC System

Resource forecasting system. Built for WSP London Building Services with one
eye on potential scalability.

Replaces the Excel-based `Resource_Forecast.xlsx` aggregator with a Flask web
application. Project managers maintain individual CTC files using the provided
Excel template. The macro in each file pushes allocation data to the server on
save. The dashboard aggregates everything in real time.

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
| `app.py` | Flask server and API endpoints |
| `database.py` | Schema and seed data |
| `summary.py` | Pre-built JSON cache for the dashboard |
| `imports/` | Staff list and PAR importers |
| `template/` | CTC Excel template (contains macro) |
| `macro/` | VBA source files for reference |
| `source-data/` | Local data files — not in git |
| `data/` | SQLite database — not in git |

## Data sources

- **Staff list**: `source-data/staff_list.xlsx`
- **PAR**: Most recent `YYYYMM_UK_PAR_*.xlsx` in `source-data/`, or direct
  SharePoint connection when `RF_PAR_USE_SHAREPOINT=true`

The database is rebuilt from source files and is never committed to git.
