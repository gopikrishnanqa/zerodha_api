"""
Persisted daily order activity (monthly_activity.json) for month buy/sell totals.
File lives in data/ folder.
"""
import json
from pathlib import Path

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
