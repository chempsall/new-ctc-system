"""
bank_holidays.py
Fetches UK bank holidays from the gov.uk API and returns
England & Wales public holidays as a dict of {date_iso: days_count}.

Each bank holiday counts as 1 working day.
"""

import json
import urllib.request
from datetime import date

BANK_HOLIDAYS_URL = "https://www.gov.uk/bank-holidays.json"
DIVISION          = "england-and-wales"


def fetch() -> dict[str, int]:
    """
    Returns {date_iso: 1} for all England & Wales bank holidays.
    Raises on network or parse failure — caller should catch and log.
    """
    req = urllib.request.Request(
        BANK_HOLIDAYS_URL,
        headers={"User-Agent": "WSP-RFT/1.0 (internal tool)"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    events = data[DIVISION]["events"]
    return {event["date"]: 1 for event in events}


def fetch_for_period(period_start: str) -> int:
    """
    Returns the number of bank holidays in a given period (month).
    period_start is YYYY-MM-01.
    Returns 0 on failure.
    """
    try:
        holidays = fetch()
        year, month, _ = period_start.split("-")
        return sum(
            1 for d in holidays
            if d.startswith(f"{year}-{month}-")
        )
    except Exception:
        return 0
