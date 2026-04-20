# source-data/

The database (data/resource_forecast.db) is NOT in git. Rebuild it on each machine with:
    python database.py
    python imports/staff_list.py source-data/UK010117_Staff_List.xlsx
    python imports/par_import.py