"""
Zerodha Kite Connect - Portfolio Holdings with Price Comparison
Fetches holdings from Zerodha API, shows % change from market data, exports to CSV.
"""

import json
import logging
import os
import hashlib
import csv
import io
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from functools import wraps

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

import requests
import yfinance as yf
from flask import Flask, redirect, request, session, jsonify, render_template, send_file
from dotenv import load_dotenv

# Load .env from the same folder as this file (project root)
_env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(_env_path)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "zerodha-portfolio-dev-secret")

API_KEY = os.environ.get("KITE_API_KEY", "")
API_SECRET = os.environ.get("KITE_API_SECRET", "")
BASE_URL = "https://api.kite.trade"
LOGIN_URL = "https://kite.zerodha.com/connect/login"
REDIRECT_URL = os.environ.get("REDIRECT_URL", "http://127.0.0.1:5000/api/callback")

ACTIVITY_FILE = Path(__file__).parent / "monthly_activity.json"
DB_PATH = Path(__file__).parent / "portfolio.db"


def get_db():
    """Get SQLite connection."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create portfolio_daily table if not exists and add MF column if missing."""
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS portfolio_daily (
            date TEXT PRIMARY KEY,
            portfolio_value REAL NOT NULL,
            portfolio_cost REAL NOT NULL,
            buy_amount REAL NOT NULL,
            sell_amount REAL NOT NULL,
            month_buy REAL NOT NULL,
            month_sell REAL NOT NULL,
            holdings_json TEXT,
            month_per_stock_json TEXT,
            num_holdings INTEGER DEFAULT 0,
            mf_holdings_json TEXT,
            mf_portfolio_value REAL DEFAULT 0,
            created_at TEXT
        )
    """)
    conn.commit()
    try:
        conn.execute("ALTER TABLE portfolio_daily ADD COLUMN mf_holdings_json TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists
    try:
        conn.execute("ALTER TABLE portfolio_daily ADD COLUMN mf_portfolio_value REAL DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    conn.close()


def get_cached_day(d: date) -> dict | None:
    """Return cached row for date, or None."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM portfolio_daily WHERE date = ?",
        (d.isoformat(),)
    ).fetchone()
    conn.close()
    if row is None:
        return None
    out = {
        "date": row["date"],
        "portfolio_value": row["portfolio_value"],
        "portfolio_cost": row["portfolio_cost"],
        "buy_amount": row["buy_amount"],
        "sell_amount": row["sell_amount"],
        "month_buy": row["month_buy"],
        "month_sell": row["month_sell"],
        "holdings_json": row["holdings_json"],
        "month_per_stock_json": row["month_per_stock_json"],
        "num_holdings": row["num_holdings"],
    }
    try:
        out["mf_holdings_json"] = row["mf_holdings_json"] if "mf_holdings_json" in row.keys() else None
        out["mf_portfolio_value"] = float(row["mf_portfolio_value"] or 0) if "mf_portfolio_value" in row.keys() else 0
    except (TypeError, KeyError):
        out["mf_holdings_json"] = None
        out["mf_portfolio_value"] = 0
    return out


def get_previous_date(d: date) -> date | None:
    """Return the most recent stored date before d."""
    conn = get_db()
    row = conn.execute(
        "SELECT date FROM portfolio_daily WHERE date < ? ORDER BY date DESC LIMIT 1",
        (d.isoformat(),)
    ).fetchone()
    conn.close()
    if row is None:
        return None
    return date.fromisoformat(row["date"])


def save_portfolio_day(
    d: date,
    portfolio_value: float,
    portfolio_cost: float,
    buy_amount: float,
    sell_amount: float,
    month_buy: float,
    month_sell: float,
    holdings: list,
    month_per_stock: dict,
    mf_holdings: list | None = None,
    mf_portfolio_value: float = 0,
):
    """Insert or replace portfolio snapshot for the given date."""
    mf_holdings = mf_holdings or []
    conn = get_db()
    conn.execute("""
        INSERT OR REPLACE INTO portfolio_daily
        (date, portfolio_value, portfolio_cost, buy_amount, sell_amount,
         month_buy, month_sell, holdings_json, month_per_stock_json, num_holdings,
         mf_holdings_json, mf_portfolio_value, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        d.isoformat(),
        portfolio_value,
        portfolio_cost,
        buy_amount,
        sell_amount,
        month_buy,
        month_sell,
        json.dumps(holdings),
        json.dumps(month_per_stock),
        len(holdings),
        json.dumps(mf_holdings),
        mf_portfolio_value,
        datetime.now().isoformat(),
    ))
    conn.commit()
    conn.close()


def update_cached_mf_only(d: date, mf_holdings: list, mf_portfolio_value: float) -> None:
    """Update only MF fields for an existing portfolio_daily row."""
    conn = get_db()
    try:
        conn.execute(
            "UPDATE portfolio_daily SET mf_holdings_json = ?, mf_portfolio_value = ? WHERE date = ?",
            (json.dumps(mf_holdings), mf_portfolio_value, d.isoformat()),
        )
        conn.commit()
        log.info("Cache updated with MF data for %s: %d funds, Rs %.2f", d.isoformat(), len(mf_holdings), mf_portfolio_value)
    except Exception as e:
        log.warning("Failed to update cache with MF: %s", e)
    finally:
        conn.close()


def require_auth(f):
    """Require valid Kite session."""
    @wraps(f)
    def decorated(*args, **kwargs):
        access_token = session.get("access_token")
        if not access_token or not API_KEY:
            return jsonify({"error": "Not authenticated"}), 401
        return f(*args, **kwargs)
    return decorated


def zerodha_request(method, path, **kwargs):
    """Make authenticated request to Kite API."""
    access_token = session.get("access_token")
    headers = {
        "X-Kite-Version": "3",
        "Authorization": f"token {API_KEY}:{access_token}",
        **(kwargs.pop("headers", {})),
    }
    url = f"{BASE_URL}{path}"
    return requests.request(method, url, headers=headers, **kwargs)


def symbol_for_yahoo(tradingsymbol: str, exchange: str) -> str:
    """Convert Zerodha symbol to Yahoo Finance format."""
    exchange = (exchange or "").upper()
    if exchange == "NSE":
        return f"{tradingsymbol}.NS"
    if exchange == "BSE":
        return f"{tradingsymbol}.BO"
    return tradingsymbol


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
        with open(ACTIVITY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def get_price_change_pct(symbol_yahoo: str, days_back: int) -> float | None:
    """Get % change from N days ago to now. Returns None if unavailable."""
    try:
        ticker = yf.Ticker(symbol_yahoo)
        end = date.today()
        start = end - timedelta(days=days_back + 5)
        hist = ticker.history(start=start, end=end, auto_adjust=True)
        if hist.empty or len(hist) < 2:
            return None
        old_price = hist["Close"].iloc[0]
        new_price = hist["Close"].iloc[-1]
        if old_price <= 0:
            return None
        return round(((new_price - old_price) / old_price) * 100, 2)
    except Exception:
        return None


@app.route("/")
def index():
    """Serve main page."""
    return render_template("index.html", has_api_key=bool(API_KEY))


@app.route("/api/login")
def login():
    """Redirect to Zerodha Kite login."""
    if not API_KEY:
        return jsonify({"error": "KITE_API_KEY not configured"}), 500
    params = {"v": "3", "api_key": API_KEY}
    return redirect(f"{LOGIN_URL}?v=3&api_key={API_KEY}")


@app.route("/api/callback")
def callback():
    """Handle OAuth callback, exchange request_token for access_token."""
    request_token = request.args.get("request_token")
    if not request_token:
        return redirect("/?error=no_request_token")

    if not API_KEY or not API_SECRET:
        return redirect("/?error=missing_api_credentials")

    checksum_str = f"{API_KEY}{request_token}{API_SECRET}"
    checksum = hashlib.sha256(checksum_str.encode()).hexdigest()

    resp = requests.post(
        f"{BASE_URL}/session/token",
        headers={"X-Kite-Version": "3"},
        data={
            "api_key": API_KEY,
            "request_token": request_token,
            "checksum": checksum,
        },
    )

    data = resp.json()
    if data.get("status") != "success":
        return redirect("/?error=token_exchange_failed")

    session["access_token"] = data["data"]["access_token"]
    session["user"] = data["data"].get("user_name", "User")
    return redirect("/")


@app.route("/api/logout")
def logout():
    """Clear session."""
    session.clear()
    return redirect("/")


def _fetch_holdings():
    """Fetch equity holdings from Kite API. Returns (holdings_list, error)."""
    r = zerodha_request("GET", "/portfolio/holdings")
    data = r.json()
    if data.get("status") != "success":
        return [], data.get("message", "Failed to fetch holdings")
    return data.get("data", []), None


def _fetch_mf_holdings():
    """Fetch mutual fund holdings from Kite API (GET /mf/holdings). Returns (list, error)."""
    r = zerodha_request("GET", "/mf/holdings")
    data = r.json()
    if data.get("status") != "success":
        msg = data.get("message", "Failed to fetch MF holdings")
        log.warning("MF holdings fetch failed: %s", msg)
        return [], msg
    raw = data.get("data") or []
    # Ensure we have a list (API can return null)
    if not isinstance(raw, list):
        raw = []
    log.info("MF holdings: fetched %d funds", len(raw))
    return raw, None


def _fetch_orders_and_update_activity():
    """Fetch today's orders, update activity file, return (today_buy, today_sell, month_buy, month_sell, month_per_stock)."""
    r = zerodha_request("GET", "/orders")
    data = r.json()
    orders_list = data.get("data", []) if data.get("status") == "success" else []

    today_key = date.today().isoformat()
    today_bought = today_sold = 0.0
    today_per_stock = {}

    for o in orders_list:
        if o.get("status") != "COMPLETE":
            continue
        try:
            filled = int(o.get("filled_quantity") or 0)
            avg = float(o.get("average_price") or 0)
        except (TypeError, ValueError):
            continue
        val = filled * avg
        tt = (o.get("transaction_type") or "").upper()
        sym = o.get("tradingsymbol", "")
        ex = o.get("exchange", "")
        key = f"{sym}|{ex}"
        if tt == "BUY":
            today_bought += val
            today_per_stock[key] = today_per_stock.get(key, {"bought": 0, "sold": 0})
            today_per_stock[key]["bought"] += val
        elif tt == "SELL":
            today_sold += val
            today_per_stock[key] = today_per_stock.get(key, {"bought": 0, "sold": 0})
            today_per_stock[key]["sold"] += val

    activity = load_activity()
    if today_per_stock:
        activity[today_key] = today_per_stock
        save_activity(activity)

    now = date.today()
    month_start = now.replace(day=1)
    month_bought = month_sold = 0.0
    month_per_stock = {}

    for dk, day_data in activity.items():
        try:
            dt = datetime.strptime(dk, "%Y-%m-%d").date()
        except ValueError:
            continue
        if dt < month_start:
            continue
        for sym_key, v in day_data.items():
            if isinstance(v, dict):
                b = float(v.get("bought", 0))
                s = float(v.get("sold", 0))
                month_bought += b
                month_sold += s
                month_per_stock[sym_key] = month_per_stock.get(sym_key, {"bought": 0, "sold": 0})
                month_per_stock[sym_key]["bought"] += b
                month_per_stock[sym_key]["sold"] += s

    return today_bought, today_sold, month_bought, month_sold, month_per_stock


def _fetch_and_compute_portfolio():
    """Fetch from Kite (equity + MF), compute totals, update activity. Returns dict or None on error."""
    holdings_list, err = _fetch_holdings()
    if err:
        return None, err
    mf_holdings_list, _ = _fetch_mf_holdings()  # don't fail entire fetch if MF fails
    today_buy, today_sell, month_buy, month_sell, month_per_stock = _fetch_orders_and_update_activity()

    portfolio_value = 0.0
    portfolio_cost = 0.0
    for h in holdings_list:
        qty = int(h.get("quantity") or 0)
        last = float(h.get("last_price") or 0)
        avg = float(h.get("average_price") or 0)
        portfolio_value += qty * last
        portfolio_cost += qty * avg

    mf_portfolio_value = 0.0
    for h in mf_holdings_list:
        try:
            qty = float(h.get("quantity") or 0)
            last = float(h.get("last_price") or 0)
            mf_portfolio_value += qty * last
        except (TypeError, ValueError):
            pass
    if mf_holdings_list:
        log.info("MF portfolio value: Rs %.2f (%d funds)", mf_portfolio_value, len(mf_holdings_list))

    return {
        "holdings": holdings_list,
        "mf_holdings": mf_holdings_list,
        "portfolio_value": round(portfolio_value, 2),
        "portfolio_cost": round(portfolio_cost, 2),
        "mf_portfolio_value": round(mf_portfolio_value, 2),
        "buy_amount": round(today_buy, 2),
        "sell_amount": round(today_sell, 2),
        "month_buy": round(month_buy, 2),
        "month_sell": round(month_sell, 2),
        "month_per_stock": {k: {"bought": round(v["bought"], 2), "sold": round(v["sold"], 2)} for k, v in month_per_stock.items()},
        "month_name": date.today().strftime("%B %Y"),
    }, None


def _build_portfolio_payload(data: dict, from_cache: bool, cached: dict | None) -> dict:
    """Build response payload with optional MF and comparison."""
    if from_cache and cached:
        holdings = json.loads(cached["holdings_json"] or "[]")
        month_per_stock = json.loads(cached["month_per_stock_json"] or "{}")
        mf_holdings = json.loads(cached.get("mf_holdings_json") or "[]")
        mf_value = float(cached.get("mf_portfolio_value") or 0)
        return {
            "fromCache": True,
            "date": cached["date"],
            "portfolio_value": float(cached["portfolio_value"]),
            "portfolio_cost": float(cached["portfolio_cost"]),
            "buy_amount": float(cached["buy_amount"]),
            "sell_amount": float(cached["sell_amount"]),
            "month_buy": float(cached["month_buy"]),
            "month_sell": float(cached["month_sell"]),
            "holdings": holdings,
            "month_per_stock": month_per_stock,
            "mf_holdings": mf_holdings,
            "mf_portfolio_value": mf_value,
            "month_name": date.today().strftime("%B %Y"),
        }
    return {
        "fromCache": False,
        "date": data["date"],
        "portfolio_value": data["portfolio_value"],
        "portfolio_cost": data["portfolio_cost"],
        "buy_amount": data["buy_amount"],
        "sell_amount": data["sell_amount"],
        "month_buy": data["month_buy"],
        "month_sell": data["month_sell"],
        "holdings": data["holdings"],
        "month_per_stock": data["month_per_stock"],
        "mf_holdings": data.get("mf_holdings", []),
        "mf_portfolio_value": data.get("mf_portfolio_value", 0),
        "month_name": data["month_name"],
    }


@app.route("/api/portfolio-summary")
@require_auth
def portfolio_summary():
    """
    Return portfolio summary and holdings. Uses SQLite cache:
    - If today's row exists and refresh=0: return from DB (no equity API call).
    - If cache has no MF data: fetch MF only, update cache, return.
    - If today's row missing or refresh=1: fetch from Kite, save to DB, return.
    """
    init_db()
    today = date.today()
    today_str = today.isoformat()
    refresh = request.args.get("refresh", "").lower() in ("1", "true", "yes")
    payload = None
    from_cache = False

    if refresh:
        log.info("Portfolio: refresh=1 -> fetching live from Zerodha (equity + MF)")
        data, err = _fetch_and_compute_portfolio()
        if err:
            return jsonify({"error": err}), 400
        save_portfolio_day(
            today,
            data["portfolio_value"],
            data["portfolio_cost"],
            data["buy_amount"],
            data["sell_amount"],
            data["month_buy"],
            data["month_sell"],
            data["holdings"],
            data["month_per_stock"],
            mf_holdings=data.get("mf_holdings", []),
            mf_portfolio_value=data.get("mf_portfolio_value", 0),
        )
        payload = _build_portfolio_payload({
            "date": today_str,
            "portfolio_value": data["portfolio_value"],
            "portfolio_cost": data["portfolio_cost"],
            "buy_amount": data["buy_amount"],
            "sell_amount": data["sell_amount"],
            "month_buy": data["month_buy"],
            "month_sell": data["month_sell"],
            "holdings": data["holdings"],
            "month_per_stock": data["month_per_stock"],
            "mf_holdings": data.get("mf_holdings", []),
            "mf_portfolio_value": data.get("mf_portfolio_value", 0),
            "month_name": data["month_name"],
        }, False, None)
    else:
        cached = get_cached_day(today)
        if cached is None:
            log.info("Portfolio: no cache for %s -> fetching live from Zerodha", today_str)
            data, err = _fetch_and_compute_portfolio()
            if err:
                return jsonify({"error": err}), 400
            save_portfolio_day(
                today,
                data["portfolio_value"],
                data["portfolio_cost"],
                data["buy_amount"],
                data["sell_amount"],
                data["month_buy"],
                data["month_sell"],
                data["holdings"],
                data["month_per_stock"],
                mf_holdings=data.get("mf_holdings", []),
                mf_portfolio_value=data.get("mf_portfolio_value", 0),
            )
            payload = _build_portfolio_payload({
                "date": today_str,
                "portfolio_value": data["portfolio_value"],
                "portfolio_cost": data["portfolio_cost"],
                "buy_amount": data["buy_amount"],
                "sell_amount": data["sell_amount"],
                "month_buy": data["month_buy"],
                "month_sell": data["month_sell"],
                "holdings": data["holdings"],
                "month_per_stock": data["month_per_stock"],
                "mf_holdings": data.get("mf_holdings", []),
                "mf_portfolio_value": data.get("mf_portfolio_value", 0),
                "month_name": data["month_name"],
            }, False, None)
        else:
            from_cache = True
            payload = _build_portfolio_payload({}, True, cached)
            mf_in_cache = (payload.get("mf_holdings") or []) and float(payload.get("mf_portfolio_value") or 0) > 0
            if not mf_in_cache:
                log.info("Portfolio: serving from cache for %s but MF missing/empty -> fetching MF only", today_str)
                mf_list, mf_err = _fetch_mf_holdings()
                if mf_list:
                    mf_val = 0.0
                    for h in mf_list:
                        try:
                            mf_val += float(h.get("quantity") or 0) * float(h.get("last_price") or 0)
                        except (TypeError, ValueError):
                            pass
                    mf_val = round(mf_val, 2)
                    payload["mf_holdings"] = mf_list
                    payload["mf_portfolio_value"] = mf_val
                    update_cached_mf_only(today, mf_list, mf_val)
                else:
                    log.warning("Portfolio: MF backfill failed: %s", mf_err or "no data")
            else:
                log.info("Portfolio: serving from cache for %s (no Zerodha API call)", today_str)

    prev_date = get_previous_date(today)
    comparison = None
    if prev_date:
        prev_row = get_cached_day(prev_date)
        if prev_row:
            comparison = {
                "previous_date": prev_date.isoformat(),
                "invested_diff": round(payload["portfolio_cost"] - float(prev_row["portfolio_cost"]), 2),
                "buy_diff": round(payload["buy_amount"] - float(prev_row["buy_amount"]), 2),
                "sell_diff": round(payload["sell_amount"] - float(prev_row["sell_amount"]), 2),
                "portfolio_value_diff": round(payload["portfolio_value"] - float(prev_row["portfolio_value"]), 2),
            }
    payload["comparison"] = comparison

    resp = jsonify(payload)
    resp.headers["X-Data-Source"] = "cache" if payload.get("fromCache") else "live"
    return resp


@app.route("/api/cache-status")
@require_auth
def cache_status():
    """Return what is stored in the DB so you can verify values are saved."""
    init_db()
    conn = get_db()
    rows = conn.execute(
        "SELECT date, portfolio_value, portfolio_cost, buy_amount, sell_amount, month_buy, month_sell, num_holdings, mf_portfolio_value, created_at FROM portfolio_daily ORDER BY date DESC LIMIT 31"
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        out.append({
            "date": r[0],
            "portfolio_value": r[1],
            "portfolio_cost": r[2],
            "buy_amount": r[3],
            "sell_amount": r[4],
            "month_buy": r[5],
            "month_sell": r[6],
            "num_holdings": r[7],
            "mf_portfolio_value": r[8],
            "created_at": r[9],
        })
    return jsonify({"stored_dates": len(out), "rows": out})


@app.route("/api/holdings")
@require_auth
def holdings():
    """Fetch portfolio holdings from Zerodha (used when not using cache)."""
    r = zerodha_request("GET", "/portfolio/holdings")
    data = r.json()
    if data.get("status") != "success":
        return jsonify({"error": data.get("message", "Failed to fetch holdings")}), 400
    return jsonify({"holdings": data.get("data", [])})


@app.route("/api/orders")
@require_auth
def orders():
    """Fetch today's orders from Zerodha."""
    r = zerodha_request("GET", "/orders")
    data = r.json()
    if data.get("status") != "success":
        return jsonify({"error": data.get("message", "Failed to fetch orders")}), 400
    return jsonify({"orders": data.get("data", [])})


@app.route("/api/orders/summary")
@require_auth
def orders_summary():
    """Today's bought/sold and current month totals (from persisted daily snapshots)."""
    r = zerodha_request("GET", "/orders")
    data = r.json()
    orders_list = data.get("data", []) if data.get("status") == "success" else []

    today_key = date.today().isoformat()
    today_bought = today_sold = 0.0
    today_per_stock = {}

    for o in orders_list:
        if o.get("status") != "COMPLETE":
            continue
        try:
            filled = int(o.get("filled_quantity") or 0)
            avg = float(o.get("average_price") or 0)
        except (TypeError, ValueError):
            continue
        val = filled * avg
        tt = (o.get("transaction_type") or "").upper()
        sym = o.get("tradingsymbol", "")
        ex = o.get("exchange", "")
        key = f"{sym}|{ex}"

        if tt == "BUY":
            today_bought += val
            today_per_stock[key] = today_per_stock.get(key, {"bought": 0, "sold": 0})
            today_per_stock[key]["bought"] += val
        elif tt == "SELL":
            today_sold += val
            today_per_stock[key] = today_per_stock.get(key, {"bought": 0, "sold": 0})
            today_per_stock[key]["sold"] += val

    activity = load_activity()
    if today_per_stock:
        activity[today_key] = today_per_stock
        save_activity(activity)

    now = date.today()
    month_start = now.replace(day=1)
    month_bought = month_sold = 0.0
    month_per_stock = {}

    for dk, day_data in activity.items():
        try:
            dt = datetime.strptime(dk, "%Y-%m-%d").date()
        except ValueError:
            continue
        if dt < month_start:
            continue
        for sym_key, v in day_data.items():
            if isinstance(v, dict):
                b = float(v.get("bought", 0))
                s = float(v.get("sold", 0))
                month_bought += b
                month_sold += s
                month_per_stock[sym_key] = month_per_stock.get(sym_key, {"bought": 0, "sold": 0})
                month_per_stock[sym_key]["bought"] += b
                month_per_stock[sym_key]["sold"] += s

    return jsonify({
        "today": {"bought": round(today_bought, 2), "sold": round(today_sold, 2)},
        "month": {"bought": round(month_bought, 2), "sold": round(month_sold, 2)},
        "month_per_stock": {k: {"bought": round(v["bought"], 2), "sold": round(v["sold"], 2)} for k, v in month_per_stock.items()},
        "month_name": now.strftime("%B %Y"),
    })


@app.route("/api/company/<exchange>/<tradingsymbol>")
@require_auth
def company_info(exchange, tradingsymbol):
    """Get company name from market data (CIN/compare source)."""
    symbol_yahoo = symbol_for_yahoo(tradingsymbol, exchange)
    try:
        ticker = yf.Ticker(symbol_yahoo)
        info = ticker.info or {}
        name = info.get("longName") or info.get("shortName") or info.get("symbol", "")
        return jsonify({"company": name or None})
    except Exception:
        return jsonify({"company": None})


@app.route("/api/price-history/<exchange>/<tradingsymbol>")
@require_auth
def price_history(exchange, tradingsymbol):
    """Get % price change for 7d, 30d, 6m, 1y (for UI display only)."""
    symbol_yahoo = symbol_for_yahoo(tradingsymbol, exchange)
    result = {
        "7d": get_price_change_pct(symbol_yahoo, 7),
        "30d": get_price_change_pct(symbol_yahoo, 30),
        "6m": get_price_change_pct(symbol_yahoo, 182),
        "1y": get_price_change_pct(symbol_yahoo, 365),
    }
    return jsonify(result)


@app.route("/api/export-csv")
@require_auth
def export_csv():
    """Generate CSV with holdings data. Uses cached today's data if available to avoid API call."""
    init_db()
    today_d = date.today()
    today = today_d.strftime("%Y-%m-%d")
    cached = get_cached_day(today_d)
    if cached and cached.get("holdings_json"):
        holdings_list = json.loads(cached["holdings_json"])
        month_per_stock = json.loads(cached.get("month_per_stock_json") or "{}")
    else:
        r = zerodha_request("GET", "/portfolio/holdings")
        data = r.json()
        if data.get("status") != "success":
            return jsonify({"error": "Failed to fetch holdings"}), 400
        holdings_list = data.get("data", [])
        month_per_stock = {}
        try:
            summary_data = load_activity()
            month_start = today_d.replace(day=1)
            for dk, day_data in summary_data.items():
                try:
                    dt = datetime.strptime(dk, "%Y-%m-%d").date()
                except ValueError:
                    continue
                if dt < month_start:
                    continue
                for sym_key, v in day_data.items():
                    if isinstance(v, dict):
                        month_per_stock[sym_key] = month_per_stock.get(sym_key, {"bought": 0, "sold": 0})
                        month_per_stock[sym_key]["bought"] += float(v.get("bought", 0))
                        month_per_stock[sym_key]["sold"] += float(v.get("sold", 0))
        except Exception:
            pass

    filename = f"zerodha_holdings_{today}.csv"
    output = io.StringIO()
    writer = csv.writer(output)

    headers = [
        "Date", "Trading Symbol", "Exchange", "Quantity", "Average Price",
        "Last Price", "Current Value", "P&L", "P&L %", "Invested (Month)", "Sold (Month)",
        "Product", "ISIN"
    ]
    writer.writerow(headers)

    total_value = 0.0
    for h in holdings_list:
        qty = h.get("quantity", 0)
        avg = float(h.get("average_price", 0))
        last = float(h.get("last_price", 0))
        val = qty * last
        total_value += val
        pnl = float(h.get("pnl", 0))
        pnl_pct = round((pnl / (avg * qty) * 100), 2) if avg and qty else 0
        key = f"{h.get('tradingsymbol', '')}|{h.get('exchange', 'NSE')}"
        m = month_per_stock.get(key, {})
        invested = round(m.get("bought", 0), 2)
        sold = round(m.get("sold", 0), 2)

        writer.writerow([
            today,
            h.get("tradingsymbol", ""),
            h.get("exchange", ""),
            qty,
            avg,
            last,
            val,
            pnl,
            pnl_pct,
            invested,
            sold,
            h.get("product", ""),
            h.get("isin", ""),
        ])

    writer.writerow([])
    writer.writerow(["", "", "", "", "", "Total Value", total_value, "", "", "", "", "", ""])

    output.seek(0)
    return send_file(
        io.BytesIO(output.getvalue().encode("utf-8-sig")),
        mimetype="text/csv",
        as_attachment=True,
        download_name=filename,
    )


if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5000)
