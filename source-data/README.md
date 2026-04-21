# source-data/

Place the following files here. This folder is excluded from git.

| File | Description |
|---|---|
| `staff_list.xlsx` | Staff details |
| `YYYYMM_UK_PAR_*.xlsx` | Latest PAR export from SharePoint |

The most recent `UK_PAR` file is used automatically — no need to rename it.

To rebuild the database after placing files here:
    python imports\staff_list.py source-data\staff_list.xlsx
    python imports\par_import.py
