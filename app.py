"""
Zerodha Kite Connect - Portfolio Holdings with Price Comparison
Fetches holdings from Zerodha API, shows % change from market data, exports to CSV.
"""

import hashlib
import csv
import io
import json
import logging
import os
from datetime import date, datetime
from pathlib import Path
from functools import wraps

import requests
import yfinance as yf
from flask import Flask, redirect, request, session, jsonify, render_template, send_file

import config
from helper import activity as activity_helper
from helper import db
from helper import kite_api
from helper import portfolio as portfolio_helper

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = config.SECRET_KEY


def require_auth(f):
    """Require valid Kite session."""
    @wraps(f)
    def decorated(*args, **kwargs):
        access_token = session.get("access_token")
        if not access_token or not config.API_KEY:
            return jsonify({"error": "Not authenticated"}), 401
        return f(*args, **kwargs)
    return decorated


def _migrate_data_to_data_dir():
    """One-time: move portfolio.db and monthly_activity.json from project root to data/ if they exist."""
    root = config._PROJECT_ROOT
    data_dir = config.DATA_DIR
    for name in ("portfolio.db", "monthly_activity.json"):
        src = root / name
        dst = data_dir / name
        if src.exists() and not dst.exists():
            try:
                import shutil
                shutil.move(str(src), str(dst))
                log.info("Migrated %s -> %s", src, dst)
            except Exception as e:
                log.warning("Migration of %s failed: %s", name, e)


@app.route("/")
def index():
    """Serve main page."""
    return render_template("index.html", has_api_key=bool(config.API_KEY))


@app.route("/api/login")
def login():
    """Redirect to Zerodha Kite login."""
    if not config.API_KEY:
        return jsonify({"error": "KITE_API_KEY not configured"}), 500
    return redirect(f"{config.LOGIN_URL}?v=3&api_key={config.API_KEY}")


@app.route("/api/callback")
def callback():
    """Handle OAuth callback, exchange request_token for access_token."""
    request_token = request.args.get("request_token")
    if not request_token:
        return redirect("/?error=no_request_token")

    if not config.API_KEY or not config.API_SECRET:
        return redirect("/?error=missing_api_credentials")

    checksum_str = f"{config.API_KEY}{request_token}{config.API_SECRET}"
    checksum = hashlib.sha256(checksum_str.encode()).hexdigest()

    resp = requests.post(
        f"{config.BASE_URL}/session/token",
        headers={"X-Kite-Version": "3"},
        data={
            "api_key": config.API_KEY,
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


@app.route("/api/portfolio-summary")
@require_auth
def portfolio_summary():
    """
    Return portfolio summary and holdings. Uses SQLite cache:
    - If today's row exists and refresh=0: return from DB (no equity API call).
    - If cache has no MF data: fetch MF only, update cache, return.
    - If today's row missing or refresh=1: fetch from Kite, save to DB, return.
    """
    db.init_db()
    today = date.today()
    today_str = today.isoformat()
    refresh = request.args.get("refresh", "").lower() in ("1", "true", "yes")
    payload = None
    from_cache = False

    if refresh:
        log.info("Portfolio: refresh=1 -> fetching live from Zerodha (equity + MF)")
        data, err = kite_api.fetch_and_compute_portfolio(session)
        if err:
            return jsonify({"error": err}), 400
        db.save_portfolio_day(
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
        payload = portfolio_helper.build_portfolio_payload({
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
        cached = db.get_cached_day(today)
        if cached is None:
            log.info("Portfolio: no cache for %s -> fetching live from Zerodha", today_str)
            data, err = kite_api.fetch_and_compute_portfolio(session)
            if err:
                return jsonify({"error": err}), 400
            db.save_portfolio_day(
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
            payload = portfolio_helper.build_portfolio_payload({
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
            payload = portfolio_helper.build_portfolio_payload({}, True, cached)
            mf_in_cache = (payload.get("mf_holdings") or []) and float(payload.get("mf_portfolio_value") or 0) > 0
            if not mf_in_cache:
                log.info("Portfolio: serving from cache for %s but MF missing/empty -> fetching MF only", today_str)
                mf_list, mf_err = kite_api.fetch_mf_holdings(session)
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
                    db.update_cached_mf_only(today, mf_list, mf_val)
                else:
                    log.warning("Portfolio: MF backfill failed: %s", mf_err or "no data")
            else:
                log.info("Portfolio: serving from cache for %s (no Zerodha API call)", today_str)

    prev_date = db.get_previous_date(today)
    comparison = None
    if prev_date:
        prev_row = db.get_cached_day(prev_date)
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
    db.init_db()
    rows = db.get_cache_status_rows(31)
    out = []
    for r in rows:
        out.append({
            "date": r["date"],
            "portfolio_value": r["portfolio_value"],
            "portfolio_cost": r["portfolio_cost"],
            "buy_amount": r["buy_amount"],
            "sell_amount": r["sell_amount"],
            "month_buy": r["month_buy"],
            "month_sell": r["month_sell"],
            "num_holdings": r["num_holdings"],
            "mf_portfolio_value": r["mf_portfolio_value"],
            "created_at": r["created_at"],
        })
    return jsonify({"stored_dates": len(out), "rows": out})


@app.route("/api/holdings")
@require_auth
def holdings():
    """Fetch portfolio holdings from Zerodha (used when not using cache)."""
    holdings_list, err = kite_api.fetch_holdings(session)
    if err:
        return jsonify({"error": err}), 400
    return jsonify({"holdings": holdings_list})


@app.route("/api/orders")
@require_auth
def orders():
    """Fetch today's orders from Zerodha."""
    r = kite_api.zerodha_request("GET", "/orders", session=session)
    data = r.json()
    if data.get("status") != "success":
        return jsonify({"error": data.get("message", "Failed to fetch orders")}), 400
    return jsonify({"orders": data.get("data", [])})


@app.route("/api/orders/summary")
@require_auth
def orders_summary():
    """Today's bought/sold and current month totals (from persisted daily snapshots)."""
    today_buy, today_sell, month_buy, month_sell, month_per_stock = kite_api.fetch_orders_and_update_activity(session)
    now = date.today()
    return jsonify({
        "today": {"bought": round(today_buy, 2), "sold": round(today_sell, 2)},
        "month": {"bought": round(month_buy, 2), "sold": round(month_sell, 2)},
        "month_per_stock": {k: {"bought": round(v["bought"], 2), "sold": round(v["sold"], 2)} for k, v in month_per_stock.items()},
        "month_name": now.strftime("%B %Y"),
    })


@app.route("/api/company/<exchange>/<tradingsymbol>")
@require_auth
def company_info(exchange, tradingsymbol):
    """Get company name from market data (CIN/compare source)."""
    symbol_yahoo = kite_api.symbol_for_yahoo(tradingsymbol, exchange)
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
    symbol_yahoo = kite_api.symbol_for_yahoo(tradingsymbol, exchange)
    result = {
        "7d": kite_api.get_price_change_pct(symbol_yahoo, 7),
        "30d": kite_api.get_price_change_pct(symbol_yahoo, 30),
        "6m": kite_api.get_price_change_pct(symbol_yahoo, 182),
        "1y": kite_api.get_price_change_pct(symbol_yahoo, 365),
    }
    return jsonify(result)


@app.route("/api/export-csv")
@require_auth
def export_csv():
    """Generate CSV with holdings data. Uses cached today's data if available to avoid API call."""
    db.init_db()
    today_d = date.today()
    today = today_d.strftime("%Y-%m-%d")
    cached = db.get_cached_day(today_d)
    if cached and cached.get("holdings_json"):
        holdings_list = json.loads(cached["holdings_json"])
        month_per_stock = json.loads(cached.get("month_per_stock_json") or "{}")
    else:
        holdings_list, err = kite_api.fetch_holdings(session)
        if err:
            return jsonify({"error": err or "Failed to fetch holdings"}), 400
        month_per_stock = {}
        try:
            summary_data = activity_helper.load_activity()
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
    config.ensure_data_dir()
    _migrate_data_to_data_dir()
    db.init_db()
    app.run(debug=True, port=5000)
