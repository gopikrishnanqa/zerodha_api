"""
Persisted daily order activity (monthly_activity.json) for month buy/sell totals.
File lives in data/ folder.
"""
import json
from datetime import date, datetime, timedelta

from config import ACTIVITY_FILE


def load_activity() -> dict:
    """Load persisted daily order activity."""
    if ACTIVITY_FILE.exists():
        try:
            with open(ACTIVITY_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_activity(data: dict) -> None:
    """Persist daily order activity."""
    try:
        ACTIVITY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(ACTIVITY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def clear_activity() -> None:
    """Clear all persisted order activity (monthly_activity.json -> {})."""
    save_activity({})


def month_totals_for(for_date: date) -> tuple[float, float]:
    """
    Return (month_buy, month_sell) for the month containing for_date,
    by summing all activity entries in that month. No Kite API call.
    """
    month_start = for_date.replace(day=1)
    if month_start.month == 12:
        month_end = month_start.replace(year=month_start.year + 1, month=1) - timedelta(days=1)
    else:
        month_end = month_start.replace(month=month_start.month + 1) - timedelta(days=1)
    month_bought = month_sold = 0.0
    activity = load_activity()
    for dk, day_data in activity.items():
        try:
            dt = datetime.strptime(dk, "%Y-%m-%d").date()
        except ValueError:
            continue
        if dt < month_start or dt > month_end:
            continue
        for _sym, v in day_data.items():
            if isinstance(v, dict):
                month_bought += float(v.get("bought", 0))
                month_sold += float(v.get("sold", 0))
    return month_bought, month_sold
