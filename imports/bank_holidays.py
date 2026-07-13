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
    Retries twice on failure. Raises if all attempts fail.
    """
    last_exc = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                BANK_HOLIDAYS_URL,
                headers={"User-Agent": "WSP-RFT/1.0 (internal tool)"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            # Validate payload shape before returning
            if DIVISION not in data:
                raise ValueError(f"Division {DIVISION!r} not in response")
            events = data[DIVISION].get("events", [])
            if not events:
                raise ValueError("No events in bank holidays response")
            return {event["date"]: 1 for event in events}
        except Exception as e:
            last_exc = e
            if attempt < 2:
                import time
                time.sleep(2 ** attempt)  # 1s, 2s backoff
    raise last_exc
