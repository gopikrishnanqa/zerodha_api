"""
Zerodha Kite API and market data (yfinance) helpers.
"""
from datetime import date, datetime, timedelta
import logging
import time

import requests
import yfinance as yf

from config import API_KEY, BASE_URL
from helper.activity import load_activity, save_activity

log = logging.getLogger(__name__)

# Kite historical API: 3 requests per second
HISTORICAL_REQUEST_DELAY = 0.35


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
    except Exception as e:
        log.debug("Price change pct unavailable for %s: %s", symbol_yahoo, e)
        return None


def fetch_price_changes_for_holdings(holdings_list: list) -> dict:
    """
    For each equity holding, fetch 7d, 30d, 6m, 1y % price change from market data.
    Returns dict keyed by 'tradingsymbol|exchange' with value {"7d": float|null, "30d": ..., "6m": ..., "1y": ...}.
    """
    out = {}
    for h in holdings_list:
        sym = h.get("tradingsymbol") or ""
        ex = (h.get("exchange") or "NSE").strip()
        key = f"{sym}|{ex}"
        symbol_yahoo = symbol_for_yahoo(sym, ex)
        out[key] = {
            "7d": get_price_change_pct(symbol_yahoo, 7),
            "30d": get_price_change_pct(symbol_yahoo, 30),
            "6m": get_price_change_pct(symbol_yahoo, 182),
            "1y": get_price_change_pct(symbol_yahoo, 365),
        }
    log.info("Fetched price changes for %d holdings (7d, 30d, 6m, 1y)", len(out))
    return out


def fetch_holdings(session) -> tuple[list, str | None]:
    """Fetch equity holdings from Kite API. Returns (holdings_list, error)."""
    r = zerodha_request("GET", "/portfolio/holdings", session=session)
    data = r.json()
    if data.get("status") != "success":
        return [], data.get("message", "Failed to fetch holdings")
    holdings = data.get("data", [])
    # Use opening_quantity (total holding including T+1) when present, so quantity matches Zerodha UI
    for h in holdings:
        oq = h.get("opening_quantity")
        if oq is not None:
            try:
                h["quantity"] = int(oq)
            except (TypeError, ValueError):
                h["quantity"] = oq
    return holdings, None


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


def fetch_mf_orders(session) -> tuple[list, str | None]:
    """Fetch mutual fund orders from Kite API. Returns (list, error)."""
    r = zerodha_request("GET", "/mf/orders", session=session)
    data = r.json()
    if data.get("status") != "success":
        msg = data.get("message", "Failed to fetch MF orders")
        log.warning("MF orders fetch failed: %s", msg)
        return [], msg
    raw = data.get("data") or []
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

    # Save to transactions table (Equity)
    equity_transactions = []
    for o in orders_list:
        if o.get("status") != "COMPLETE": continue
        qty = float(o.get("filled_quantity") or 0)
        price = float(o.get("average_price") or 0)
        tt = (o.get("transaction_type") or "").upper()
        equity_transactions.append({
            "id": o.get("order_id"),
            "date": o.get("order_timestamp", "").split(" ")[0] or today_key,
            "type": tt,
            "instrument_type": "EQUITY",
            "tradingsymbol": o.get("tradingsymbol"),
            "exchange": o.get("exchange"),
            "quantity": qty,
            "price": price,
            "amount": qty * price,
            "status": o.get("status"),
        })
    if equity_transactions:
        from helper import db
        db.save_transactions(equity_transactions)

    # Fetch and save MF orders
    mf_orders, _ = fetch_mf_orders(session)
    mf_transactions = []
    for o in mf_orders:
        if o.get("status") != "COMPLETE": continue
        qty = float(o.get("quantity") or 0)
        price = float(o.get("last_price") or 0) # MF orders often use NAV/Last Price
        amt = float(o.get("amount") or (qty * price))
        tt = (o.get("transaction_type") or "").upper()
        mf_transactions.append({
            "id": o.get("order_id"),
            "date": o.get("order_timestamp", "").split(" ")[0] or today_key,
            "type": tt,
            "instrument_type": "MF",
            "tradingsymbol": o.get("tradingsymbol"),
            "fund": o.get("fund", ""),  # Fund name from Kite API
            "exchange": "MF",
            "quantity": qty,
            "price": price if qty else 0,
            "amount": amt,
            "status": o.get("status"),
        })
    if mf_transactions:
        from helper import db
        db.save_transactions(mf_transactions)

    now = date.today()
    month_start = now.replace(day=1)
    month_bought = month_sold = 0.0
    month_per_stock = {}

    # Include equity orders from activity file
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

    # Include MF orders in monthly totals
    for o in mf_orders:
        if o.get("status") != "COMPLETE":
            continue
        try:
            order_date_str = o.get("order_timestamp", "").split(" ")[0]
            if not order_date_str:
                continue
            order_date = datetime.strptime(order_date_str, "%Y-%m-%d").date()
            if order_date < month_start:
                continue
            amt = float(o.get("amount") or 0)
            if amt <= 0:
                qty = float(o.get("quantity") or 0)
                price = float(o.get("last_price") or 0)
                amt = qty * price
            tt = (o.get("transaction_type") or "").upper()
            sym = o.get("tradingsymbol", "")
            key = f"{sym}|MF"
            if tt == "BUY":
                month_bought += amt
                month_per_stock[key] = month_per_stock.get(key, {"bought": 0, "sold": 0})
                month_per_stock[key]["bought"] += amt
            elif tt == "SELL":
                month_sold += amt
                month_per_stock[key] = month_per_stock.get(key, {"bought": 0, "sold": 0})
                month_per_stock[key]["sold"] += amt
        except (ValueError, TypeError):
            continue

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
    mf_portfolio_cost = 0.0
    for h in mf_holdings_list:
        try:
            qty = float(h.get("quantity") or 0)
            last = float(h.get("last_price") or 0)
            avg = float(h.get("average_price") or 0)
            mf_portfolio_value += qty * last
            mf_portfolio_cost += qty * avg
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
        "mf_portfolio_cost": round(mf_portfolio_cost, 2),
        "buy_amount": round(today_buy, 2),
        "sell_amount": round(today_sell, 2),
        "month_buy": round(month_buy, 2),
        "month_sell": round(month_sell, 2),
        "month_per_stock": {k: {"bought": round(v["bought"], 2), "sold": round(v["sold"], 2)} for k, v in month_per_stock.items()},
        "month_name": date.today().strftime("%B %Y"),
    }, None


def fetch_historical_candles(session, instrument_token: int, from_date: date, to_date: date, interval: str = "day"):
    """
    Fetch historical OHLC candles from Kite.
    GET /instruments/historical/{instrument_token}/{interval}?from=yyyy-mm-dd hh:mm:ss&to=...
    Returns list of [timestamp, open, high, low, close, volume] or [] on error.
    """
    from_str = from_date.strftime("%Y-%m-%d 09:15:00")
    to_str = to_date.strftime("%Y-%m-%d 15:30:00")
    path = f"/instruments/historical/{instrument_token}/{interval}"
    r = zerodha_request("GET", path, session=session, params={"from": from_str, "to": to_str})
    data = r.json()
    if data.get("status") != "success":
        return []
    return data.get("data", {}).get("candles") or []


def _parse_candle_date(candle) -> date | None:
    """From candle [timestamp, o, h, l, c, v] get the date. Timestamp is like 2017-12-15T09:15:00+0530."""
    try:
        ts = candle[0]
        if isinstance(ts, str):
            return date.fromisoformat(ts.split("T")[0])
        return None
    except (IndexError, ValueError, TypeError):
        return None


def fetch_portfolio_value_at_month_dates(
    session,
    from_year: int = 2025,
    from_month: int = 1,
    to_year: int = 2026,
    to_month: int = 1,
):
    """
    For current equity holdings, fetch historical close prices at the 1st of each month
    (Jan 1 2025, Feb 1 2025, ... till Jan 1 2026) using Kite historical API.
    Uses current holdings and quantities; value = sum(quantity * historical_close).
    Returns list of { "date": "YYYY-MM-DD", "num_stocks": int, "total_value": float }.
    """
    holdings_list, err = fetch_holdings(session)
    if err or not holdings_list:
        return [], err or "No holdings"
    # Build month dates: 1st of each month from (from_year, from_month) to (to_year, to_month) inclusive
    month_dates = []
    y, m = from_year, from_month
    while (y, m) <= (to_year, to_month):
        try:
            month_dates.append(date(y, m, 1))
        except ValueError:
            pass
        m += 1
        if m > 12:
            m = 1
            y += 1
    if not month_dates:
        return [], None
    from_date = month_dates[0]
    to_date = month_dates[-1] + timedelta(days=31)
    # Per holding: fetch day candles for the range, build date -> close map
    # Holdings must have instrument_token (equity holdings from Kite include it)
    date_to_value = {d.isoformat(): 0.0 for d in month_dates}
    num_stocks = len(holdings_list)
    for h in holdings_list:
        try:
            token = int(h.get("instrument_token") or 0)
        except (TypeError, ValueError):
            log.warning("Skipping holding %s: no instrument_token", h.get("tradingsymbol"))
            continue
        if token <= 0:
            continue
        qty = int(h.get("quantity") or 0)
        if qty <= 0:
            continue
        time.sleep(HISTORICAL_REQUEST_DELAY)
        candles = fetch_historical_candles(session, token, from_date, to_date, "day")
        if not candles:
            continue
        # Build map: calendar date (YYYY-MM-DD) -> close price (use latest candle on or before that date)
        date_to_close = {}
        for c in candles:
            d = _parse_candle_date(c)
            if d is None:
                continue
            close = c[4] if len(c) > 4 else 0
            date_to_close[d.isoformat()] = float(close)
        sorted_dates = sorted(date_to_close.keys())
        for target in month_dates:
            t_str = target.isoformat()
            if t_str in date_to_close:
                date_to_value[t_str] += qty * date_to_close[t_str]
            else:
                # Use previous trading day's close if that day had no candle
                prev_close = None
                for sd in reversed(sorted_dates):
                    if sd <= t_str:
                        prev_close = date_to_close[sd]
                        break
                if prev_close is not None:
                    date_to_value[t_str] += qty * prev_close
    out = [
        {"date": d, "num_stocks": num_stocks, "total_value": round(date_to_value[d], 2)}
        for d in sorted(date_to_value.keys())
    ]
    log.info("Historical portfolio value: %d month points (%s .. %s)", len(out), month_dates[0].isoformat() if month_dates else "?", month_dates[-1].isoformat() if month_dates else "?")
    return out, None
