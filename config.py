"""
App configuration and paths. DB and activity file live under data/.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(_PROJECT_ROOT / ".env")

# Data folder: DB and activity file stored here
DATA_DIR = _PROJECT_ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

DB_PATH = DATA_DIR / "portfolio.db"
ACTIVITY_FILE = DATA_DIR / "monthly_activity.json"

# Zerodha / Flask
API_KEY = os.environ.get("KITE_API_KEY", "")
API_SECRET = os.environ.get("KITE_API_SECRET", "")
SECRET_KEY = os.environ.get("SECRET_KEY", "zerodha-portfolio-dev-secret")
BASE_URL = "https://api.kite.trade"
LOGIN_URL = "https://kite.zerodha.com/connect/login"
REDIRECT_URL = os.environ.get("REDIRECT_URL", "http://127.0.0.1:5000/api/callback")


def ensure_data_dir():
    """Ensure data directory exists (idempotent)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
