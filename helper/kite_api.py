"""
Zerodha Kite API and market data (yfinance) helpers.
"""
from datetime import date, datetime, timedelta
import logging

import requests
import yfinance as yf

from config import API_KEY, BASE_URL
from helper.activity import load_activity, save_activity

log = logging.getLogger(__name__)


def zerodha_request(method: str, path: str, session, **kwargs):
    """Make authenticated request to Kite API. Requires Flask session."""
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


def fetch_holdings(session) -> tuple[list, str | None]:
    """Fetch equity holdings from Kite API. Returns (holdings_list, error)."""
    r = zerodha_request("GET", "/portfolio/holdings", session=session)
    data = r.json()
    if data.get("status") != "success":
        return [], data.get("message", "Failed to fetch holdings")
    return data.get("data", []), None


def fetch_mf_holdings(session) -> tuple[list, str | None]:
    """Fetch mutual fund holdings from Kite API. Returns (list, error)."""
    r = zerodha_request("GET", "/mf/holdings", session=session)
    data = r.json()
    if data.get("status") != "success":
        msg = data.get("message", "Failed to fetch MF holdings")
        log.warning("MF holdings fetch failed: %s", msg)
        return [], msg
    raw = data.get("data") or []
    if not isinstance(raw, list):
        raw = []
    log.info("MF holdings: fetched %d funds", len(raw))
    return raw, None


def fetch_orders_and_update_activity(session) -> tuple[float, float, float, float, dict]:
    """Fetch today's orders, update activity file, return (today_buy, today_sell, month_buy, month_sell, month_per_stock)."""
    r = zerodha_request("GET", "/orders", session=session)
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


def fetch_and_compute_portfolio(session) -> tuple[dict | None, str | None]:
    """Fetch from Kite (equity + MF), compute totals, update activity. Returns (data_dict, error)."""
    holdings_list, err = fetch_holdings(session)
    if err:
        return None, err
    mf_holdings_list, _ = fetch_mf_holdings(session)
    today_buy, today_sell, month_buy, month_sell, month_per_stock = fetch_orders_and_update_activity(session)

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
