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
from datetime import date, datetime, timedelta
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
@app.route("/holdings")
@app.route("/monthly")
@app.route("/weekly")
@app.route("/db")
@app.route("/by-date")
@app.route("/holdings-by-date")
@app.route("/ledger-equity")
@app.route("/ledger-mf")
@app.route("/stock-ledger")
@app.route("/mf-ledger")
@app.route("/mf-dip")
@app.route("/mf-compare")
@app.route("/live-portfolio")
@app.route("/performance")
@app.route("/tools")
@app.route("/checklist")
def index():
    """Serve main page (separate URLs for each section)."""
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
    Return portfolio summary and holdings. Uses SQLite cache when available:
    - If today's row exists and refresh=0: return from DB (no Zerodha API call).
    - If cache has no MF data: fetch MF only, update cache, return.
    - If today's row missing or refresh=1: fetch from Kite, save to DB, return.
    
    New: skip_price_changes=1 skips expensive Yahoo Finance calls (for fast initial load).
    """
    try:
        db.init_db()
        today = date.today()
        today_str = today.isoformat()
        refresh = request.args.get("refresh", "").lower() in ("1", "true", "yes")
        skip_price_changes = request.args.get("skip_price_changes", "").lower() in ("1", "true", "yes")
        payload = None

        if refresh:
            log.info("Portfolio: refresh=1 -> fetching live from Zerodha (equity + MF)")
            data, err = kite_api.fetch_and_compute_portfolio(session)
            if err:
                return jsonify({"error": err}), 400
            # Skip price changes if requested (will be fetched in background)
            if skip_price_changes:
                price_changes = {}
                log.info("Portfolio: skipping price changes fetch (skip_price_changes=1)")
            else:
                price_changes = kite_api.fetch_price_changes_for_holdings(data["holdings"])
            data["price_changes"] = price_changes
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
                mf_portfolio_cost=data.get("mf_portfolio_cost", 0),
                price_changes=price_changes,
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
                "price_changes": price_changes,
                "month_name": data["month_name"],
            }, False, None)
        else:
            cached = db.get_cached_day(today)
            if cached is None:
                log.info("Portfolio: no cache for %s -> fetching live from Zerodha", today_str)
                data, err = kite_api.fetch_and_compute_portfolio(session)
                if err:
                    return jsonify({"error": err}), 400
                # Skip price changes if requested (will be fetched in background)
                if skip_price_changes:
                    price_changes = {}
                    log.info("Portfolio: skipping price changes fetch (skip_price_changes=1)")
                else:
                    price_changes = kite_api.fetch_price_changes_for_holdings(data["holdings"])
                data["price_changes"] = price_changes
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
                    mf_portfolio_cost=data.get("mf_portfolio_cost", 0),
                    price_changes=price_changes,
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
                    "price_changes": price_changes,
                    "month_name": data["month_name"],
                }, False, None)
            else:
                # Value as of last day of previous month (e.g. 2026-01-31) for comparison
                last_day_prev = today.replace(day=1) - timedelta(days=1)
                cached_first_prev = db.get_cached_day_on_or_before(last_day_prev)
                payload = portfolio_helper.build_portfolio_payload({}, True, cached, cached_first_prev)
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
        # Value as of last day of previous month (for live we add from DB if not already in payload)
        if not payload.get("last_month_value_date"):
            last_day_prev = today.replace(day=1) - timedelta(days=1)
            cached_first_prev = db.get_cached_day_on_or_before(last_day_prev)
            if cached_first_prev:
                pv = float(cached_first_prev.get("portfolio_value") or 0)
                pc = float(cached_first_prev.get("portfolio_cost") or 0)
                mfv = float(cached_first_prev.get("mf_portfolio_value") or 0)
                mfc = float(cached_first_prev.get("mf_portfolio_cost") or 0)
                payload["last_month_value_date"] = cached_first_prev.get("date")
                payload["last_month_portfolio_value"] = pv + mfv
                payload["last_month_portfolio_cost"] = pc + mfc
        # Last day of previous year (for YTD column)
        last_day_prev_year = date(today.year - 1, 12, 31)
        cached_prev_year = db.get_cached_day_on_or_before(last_day_prev_year)
        if cached_prev_year:
            pv = float(cached_prev_year.get("portfolio_value") or 0)
            pc = float(cached_prev_year.get("portfolio_cost") or 0)
            mfv = float(cached_prev_year.get("mf_portfolio_value") or 0)
            mfc = float(cached_prev_year.get("mf_portfolio_cost") or 0)
            payload["last_year_value_date"] = cached_prev_year.get("date")
            payload["last_year_portfolio_value"] = pv + mfv
            payload["last_year_portfolio_cost"] = pc + mfc

        resp = jsonify(payload)
        resp.headers["X-Data-Source"] = "cache" if payload.get("fromCache") else "live"
        return resp
    except Exception as e:
        log.exception("portfolio_summary failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/portfolio-cached")
@require_auth
def portfolio_cached():
    """
    Fast endpoint: Return portfolio from DB cache only (no API calls).
    Returns latest cached data regardless of date. Used for instant initial load.
    """
    try:
        db.init_db()
        today = date.today()
        
        # Try today's cache first, then get the most recent cached day
        cached = db.get_cached_day(today)
        if cached is None:
            cached = db.get_cached_day_on_or_before(today)
        
        if cached is None:
            return jsonify({"error": "no_cache", "message": "No cached data available"}), 404
        
        cache_date = cached.get("date", "")
        last_day_prev = today.replace(day=1) - timedelta(days=1)
        cached_first_prev = db.get_cached_day_on_or_before(last_day_prev)
        
        payload = portfolio_helper.build_portfolio_payload({}, True, cached, cached_first_prev)
        payload["cache_date"] = cache_date
        payload["is_stale"] = cache_date != today.isoformat()
        
        # Add comparison data
        prev_date = db.get_previous_date(date.fromisoformat(cache_date) if cache_date else today)
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
        
        # Add month/year data
        if not payload.get("last_month_value_date"):
            if cached_first_prev:
                pv = float(cached_first_prev.get("portfolio_value") or 0)
                pc = float(cached_first_prev.get("portfolio_cost") or 0)
                mfv = float(cached_first_prev.get("mf_portfolio_value") or 0)
                mfc = float(cached_first_prev.get("mf_portfolio_cost") or 0)
                payload["last_month_value_date"] = cached_first_prev.get("date")
                payload["last_month_portfolio_value"] = pv + mfv
                payload["last_month_portfolio_cost"] = pc + mfc
        
        last_day_prev_year = date(today.year - 1, 12, 31)
        cached_prev_year = db.get_cached_day_on_or_before(last_day_prev_year)
        if cached_prev_year:
            pv = float(cached_prev_year.get("portfolio_value") or 0)
            pc = float(cached_prev_year.get("portfolio_cost") or 0)
            mfv = float(cached_prev_year.get("mf_portfolio_value") or 0)
            mfc = float(cached_prev_year.get("mf_portfolio_cost") or 0)
            payload["last_year_value_date"] = cached_prev_year.get("date")
            payload["last_year_portfolio_value"] = pv + mfv
            payload["last_year_portfolio_cost"] = pc + mfc
        
        log.info("Portfolio cached: serving %s (stale=%s)", cache_date, payload["is_stale"])
        resp = jsonify(payload)
        resp.headers["X-Data-Source"] = "cache"
        return resp
    except Exception as e:
        log.exception("portfolio_cached failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/cache/clear", methods=["POST"])
@require_auth
def clear_cache():
    """Clear DB cache only (portfolio_daily table). Does not clear order-activity file."""
    db.init_db()
    n = db.clear_portfolio_cache()
    log.info("Cache cleared: %d portfolio row(s) deleted from DB", n)
    return jsonify({"ok": True, "portfolio_rows_deleted": n})


@app.route("/api/restore-archive", methods=["POST"])
@require_auth
def restore_archive():
    """Copy all archive-only dates into portfolio_daily (and holdings tables). Data stays in archive too."""
    db.init_db()
    count, dates_restored = db.restore_archive_to_daily()
    log.info("Restored %d date(s) from archive to daily: %s", count, dates_restored)
    return jsonify({"ok": True, "restored": count, "dates": dates_restored})


@app.route("/api/turso-validate")
@require_auth
def turso_validate():
    """Validate Turso connection (URL + token from .env). Returns { ok: bool, message: str }."""
    try:
        from helper import turso_sync as ts
    except ImportError:
        return jsonify({"ok": False, "message": "Turso sync not available"})
    ok, message = ts.validate_turso_connection()
    return jsonify({"ok": ok, "message": message})


@app.route("/api/sync-turso", methods=["POST"])
@require_auth
def sync_turso():
    """Push all local portfolio_daily data to Turso (manual or scheduled sync)."""
    try:
        from helper import turso_sync as ts
    except ImportError:
        return jsonify({"error": "Turso sync not available"}), 500
    if not ts._turso_enabled():
        return jsonify({"error": "Turso not configured (set TURSO_DATABASE_URL and TURSO_AUTH_TOKEN)"}), 400
    db.init_db()
    dates_list = db.get_dates_list(365)
    synced = 0
    for date_str in dates_list or []:
        try:
            d = date.fromisoformat(date_str)
        except ValueError:
            continue
        cached = db.get_cached_day(d)
        if not cached:
            continue
        try:
            holdings = json.loads(cached.get("holdings_json") or "[]")
            mf_holdings = json.loads(cached.get("mf_holdings_json") or "[]")
            month_per_stock = json.loads(cached.get("month_per_stock_json") or "{}")
            price_changes = json.loads(cached.get("price_changes_json") or "{}")
            ts.save_portfolio_day_turso(
                d,
                float(cached.get("portfolio_value") or 0),
                float(cached.get("portfolio_cost") or 0),
                float(cached.get("buy_amount") or 0),
                float(cached.get("sell_amount") or 0),
                float(cached.get("month_buy") or 0),
                float(cached.get("month_sell") or 0),
                holdings,
                month_per_stock,
                mf_holdings=mf_holdings,
                mf_portfolio_value=float(cached.get("mf_portfolio_value") or 0),
                mf_portfolio_cost=float(cached.get("mf_portfolio_cost") or 0),
                price_changes=price_changes,
            )
            synced += 1
        except Exception as e:
            log.exception("Turso sync failed for %s", date_str)
            return jsonify({"error": "Turso sync failed: " + str(e), "synced_so_far": synced}), 500
    checklist_synced = 0
    for row in db.get_all_checklist_rows():
        try:
            ts.set_checklist_turso(row[0], row[1], row[2], row[3], row[4])
            checklist_synced += 1
        except Exception as e:
            log.warning("Turso checklist sync failed for %s %s: %s", row[0], row[1], e)
    log.info("Turso manual sync: %d date(s), %d checklist row(s)", synced, checklist_synced)
    return jsonify({"synced": True, "dates": synced, "checklist_rows": checklist_synced})


@app.route("/api/checklist")
@require_auth
def api_checklist_get():
    """Return checklist data for current month, year, quarter (for Checklist page)."""
    db.init_db()
    now = date.today()
    month_key = now.strftime("%Y-%m")
    year_key = str(now.year)
    q = (now.month - 1) // 3 + 1
    quarter_key = "%s-Q%d" % (now.year, q)
    out = {}
    for period_type, period_key in [("month", month_key), ("year", year_key), ("quarter", quarter_key)]:
        row = db.get_checklist(period_type, period_key)
        out[period_type] = {
            "key": period_key,
            "state": (row or {}).get("state") or {},
            "custom": (row or {}).get("custom") or [],
            "archived": (row or {}).get("archived") or [],
        }
    return jsonify(out)


@app.route("/api/checklist", methods=["POST"])
@require_auth
def api_checklist_post():
    """Save checklist for one period. Body: period_type, period_key, state, custom, archived."""
    data = request.get_json(force=True, silent=True) or {}
    period_type = (data.get("period_type") or "").strip()
    period_key = (data.get("period_key") or "").strip()
    if period_type not in ("month", "year", "quarter") or not period_key:
        return jsonify({"error": "Need period_type (month|year|quarter) and period_key"}), 400
    state = data.get("state")
    custom = data.get("custom")
    archived = data.get("archived")
    if state is None:
        state = {}
    if custom is None:
        custom = []
    if archived is None:
        archived = []
    if not isinstance(state, dict):
        state = {}
    if not isinstance(custom, list):
        custom = []
    if not isinstance(archived, list):
        archived = []
    db.init_db()
    db.set_checklist(period_type, period_key, state, custom, archived)
    return jsonify({"ok": True})


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
            "mf_portfolio_cost": float(r.get("mf_portfolio_cost") or 0),
            "created_at": r["created_at"],
        })
    return jsonify({"stored_dates": len(out), "rows": out})


@app.route("/api/dates")
@require_auth
def api_dates():
    """Return list of dates we have portfolio data for. Query param: limit (default 365, use 100 for holdings-by-date)."""
    db.init_db()
    try:
        limit = int(request.args.get("limit", 365))
    except ValueError:
        limit = 365
    limit = min(max(1, limit), 500)
    dates = db.get_dates_list(limit)
    return jsonify({"dates": dates})


@app.route("/api/holdings-by-date")
@require_auth
def holdings_by_date():
    """Return equity and MF holdings for a given date from DB. Query param: date=YYYY-MM-DD."""
    db.init_db()
    date_str = request.args.get("date")
    if not date_str:
        return jsonify({"error": "Missing date=YYYY-MM-DD"}), 400
    try:
        d = date.fromisoformat(date_str)
    except ValueError:
        return jsonify({"error": "Invalid date format"}), 400
    out = db.get_holdings_for_date(d)
    if out is None:
        return jsonify({"error": "No data for this date"}), 404
    return jsonify(out)


@app.route("/api/summary-by-date")
@require_auth
def summary_by_date():
    """Return only the latest record for each month (for Monthly Summary page)."""
    db.init_db()
    rows = db.get_monthly_summary_rows()
    out = []
    for r in rows:
        out.append({
            "date": r["date"],
            "portfolio_value": float(r["portfolio_value"] or 0),
            "portfolio_cost": float(r["portfolio_cost"] or 0),
            "month_buy": float(r["month_buy"] or 0),
            "month_sell": float(r["month_sell"] or 0),
            "num_holdings": int(r["num_holdings"] or 0),
            "mf_portfolio_value": float(r["mf_portfolio_value"] or 0),
            "mf_portfolio_cost": float(r.get("mf_portfolio_cost") or 0),
            "created_at": r["created_at"],
        })
    return jsonify({"rows": out})


@app.route("/api/weekly-summary")
@require_auth
def weekly_summary():
    """Return all snapshots for a given month (one per week). Query param: month=YYYY-MM."""
    db.init_db()
    month = request.args.get("month")
    if not month:
        now = date.today()
        month = now.strftime("%Y-%m")
    rows = db.get_weekly_summary_rows_for_month(month)
    out = []
    for r in rows:
        out.append({
            "date": r["date"],
            "portfolio_value": float(r["portfolio_value"] or 0),
            "portfolio_cost": float(r["portfolio_cost"] or 0),
            "month_buy": float(r["month_buy"] or 0),
            "month_sell": float(r["month_sell"] or 0),
            "num_holdings": int(r["num_holdings"] or 0),
            "mf_portfolio_value": float(r["mf_portfolio_value"] or 0),
            "mf_portfolio_cost": float(r.get("mf_portfolio_cost") or 0),
            "created_at": r["created_at"],
        })
    return jsonify({"rows": out, "month": month})


@app.route("/api/live-portfolio")
@require_auth
def live_portfolio():
    """
    Fetch live portfolio directly from Zerodha API without any caching.
    Returns exact values as shown in Zerodha Console:
    - Uses API's pnl field directly (includes all charges in calculation)
    - Derives invested from (value - pnl) for accuracy
    """
    try:
        # Fetch equity holdings
        eq_holdings, eq_err = kite_api.fetch_holdings(session)
        if eq_err:
            return jsonify({"error": f"Failed to fetch equity: {eq_err}"}), 400
        
        # Fetch MF holdings
        mf_holdings, mf_err = kite_api.fetch_mf_holdings(session)
        if mf_err:
            log.warning("MF fetch failed: %s", mf_err)
            mf_holdings = []
        
        # Calculate equity totals using API's pnl field
        # Note: Use 'quantity' only (not t1_quantity) to match Zerodha Console
        # The API's pnl field is calculated as (last_price - avg_price) * quantity
        eq_value = 0.0
        eq_pnl = 0.0
        for h in eq_holdings:
            qty = float(h.get("quantity") or 0)  # Don't include t1_quantity
            last_price = float(h.get("last_price") or 0)
            pnl = float(h.get("pnl") or 0)
            eq_value += qty * last_price
            eq_pnl += pnl
        # Zerodha's "Invested" includes stamp duty & charges which API doesn't provide
        # So we derive invested from value - pnl (matches Zerodha's P&L calculation)
        eq_invested = eq_value - eq_pnl
        
        # Calculate MF totals using API's pnl field (may be 0)
        mf_value = 0.0
        mf_pnl = 0.0
        mf_invested_calc = 0.0
        for h in mf_holdings:
            qty = float(h.get("quantity") or 0)
            last_price = float(h.get("last_price") or 0)
            avg_price = float(h.get("average_price") or 0)
            pnl = float(h.get("pnl") or 0)
            mf_value += qty * last_price
            mf_pnl += pnl
            mf_invested_calc += qty * avg_price
        # If MF pnl is 0 (API limitation), use calculated values
        if mf_pnl == 0 and mf_value > 0:
            mf_invested = mf_invested_calc
            mf_pnl = mf_value - mf_invested
        else:
            mf_invested = mf_value - mf_pnl
        
        # Totals
        total_value = eq_value + mf_value
        total_invested = eq_invested + mf_invested
        total_pnl = eq_pnl + mf_pnl
        
        return jsonify({
            "fetched_at": datetime.now().isoformat(),
            "equity": {
                "holdings": eq_holdings,
                "count": len(eq_holdings),
                "value": round(eq_value, 2),
                "invested": round(eq_invested, 2),
                "pnl": round(eq_pnl, 2),
                "pnl_pct": round((eq_pnl / eq_invested * 100) if eq_invested > 0 else 0, 2),
            },
            "mf": {
                "holdings": mf_holdings,
                "count": len(mf_holdings),
                "value": round(mf_value, 2),
                "invested": round(mf_invested, 2),
                "pnl": round(mf_pnl, 2),
                "pnl_pct": round((mf_pnl / mf_invested * 100) if mf_invested > 0 else 0, 2),
            },
            "total": {
                "value": round(total_value, 2),
                "invested": round(total_invested, 2),
                "pnl": round(total_pnl, 2),
                "pnl_pct": round((total_pnl / total_invested * 100) if total_invested > 0 else 0, 2),
            }
        })
    except Exception as e:
        log.exception("Live portfolio fetch failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/ledger/equity")
@require_auth
def ledger_equity():
    """Return equity transaction ledger (recent 500)."""
    db.init_db()
    rows = db.get_transactions("EQUITY", 500)
    return jsonify({"rows": rows})


@app.route("/api/monthly-transactions")
@require_auth
def monthly_transactions():
    """Return monthly buy/sell totals separated by stocks and MF."""
    db.init_db()
    data = db.get_monthly_transaction_totals()
    return jsonify({
        "monthly_totals": data["months"],
        "total_eq_count": data["total_eq_count"],
        "total_mf_count": data["total_mf_count"]
    })


@app.route("/api/monthly-transactions-detail")
@require_auth
def monthly_transactions_detail():
    """Return monthly transactions with per-symbol breakdown."""
    db.init_db()
    data = db.get_monthly_transaction_details()
    return jsonify({"details": data})


@app.route("/api/ledger/mf")
@require_auth
def ledger_mf():
    """Return Mutual Fund transaction ledger (recent 500) with fund names."""
    db.init_db()
    rows = db.get_transactions("MF", 500)
    
    # Get MF name mapping from the most recent MF holdings
    mf_name_map = {}
    today = date.today()
    cached = db.get_cached_day_on_or_before(today)
    if cached:
        try:
            mf_holdings = json.loads(cached.get("mf_holdings_json") or "[]")
            for h in mf_holdings:
                sym = h.get("tradingsymbol", "")
                fund_name = h.get("fund", "")
                if sym and fund_name:
                    mf_name_map[sym] = fund_name
        except (json.JSONDecodeError, TypeError):
            pass
    
    # Add fund names to rows
    enriched_rows = []
    for r in rows:
        row = dict(r)
        sym = row.get("tradingsymbol", "")
        # Try to get fund name from data_json first, then from mapping
        try:
            data = json.loads(row.get("data_json") or "{}")
            fund_name = data.get("fund", "")
        except (json.JSONDecodeError, TypeError):
            fund_name = ""
        if not fund_name:
            fund_name = mf_name_map.get(sym, "")
        row["fund_name"] = fund_name
        enriched_rows.append(row)
    
    return jsonify({"rows": enriched_rows})



@app.route("/api/nifty50-closes")
@require_auth
def nifty50_closes():
    """Return Nifty 50 (^NSEI) close price for given dates. Query param: dates=YYYY-MM-DD,YYYY-MM-DD."""
    import yfinance as yf
    dates_str = request.args.get("dates", "")
    if not dates_str:
        return jsonify({"error": "Missing dates=YYYY-MM-DD,YYYY-MM-DD"}), 400
    try:
        want = [date.fromisoformat(s.strip()) for s in dates_str.split(",") if s.strip()]
    except ValueError:
        return jsonify({"error": "Invalid date in dates"}), 400
    if not want:
        return jsonify({"closes": {}})
    min_d, max_d = min(want), max(want)
    ticker = yf.Ticker("^NSEI")
    hist = ticker.history(start=min_d, end=max_d + timedelta(days=1), auto_adjust=True)
    closes = {}
    if hist.empty:
        return jsonify({"closes": {}})
    for d in want:
        d_str = d.isoformat()
        on_date = hist[hist.index.date == d]
        if len(on_date):
            closes[d_str] = round(float(on_date.iloc[0]["Close"]), 2)
        else:
            before = hist[hist.index.date <= d]
            if len(before):
                closes[d_str] = round(float(before.iloc[-1]["Close"]), 2)
    return jsonify({"closes": closes})


@app.route("/api/performance-data")
@require_auth
def performance_data():
    """
    Return portfolio performance data for charting.
    Simulates: "What if I invested the same amount in Nifty 50 or Nifty Next 50?"
    
    For each date:
    - Track actual portfolio value and cost (invested amount)
    - Calculate hypothetical Nifty 50 / Next 50 value if same investments were made
    - When cost increases (new investment), buy equivalent index units
    - When cost decreases (withdrawal), sell proportional index units
    """
    db.init_db()
    
    # Get all stored portfolio dates
    rows = db.get_cache_status_rows(365)
    if not rows:
        return jsonify({"error": "No portfolio data available"}), 404
    
    # Sort by date ascending
    rows = sorted(rows, key=lambda r: r["date"])
    dates = [r["date"] for r in rows]
    
    if len(dates) < 2:
        return jsonify({"error": "Need at least 2 data points for comparison"}), 400
    
    # Portfolio values and costs (equity + MF)
    portfolio_data = []
    for r in rows:
        pv = float(r.get("portfolio_value") or 0) + float(r.get("mf_portfolio_value") or 0)
        pc = float(r.get("portfolio_cost") or 0) + float(r.get("mf_portfolio_cost") or 0)
        portfolio_data.append({"date": r["date"], "value": pv, "cost": pc})
    
    # Fetch Nifty 50 and Nifty Next 50 index prices
    min_d = date.fromisoformat(dates[0])
    max_d = date.fromisoformat(dates[-1])
    
    nifty50_prices = {}
    niftynext50_prices = {}
    
    try:
        # Nifty 50: ^NSEI
        ticker50 = yf.Ticker("^NSEI")
        hist50 = ticker50.history(start=min_d - timedelta(days=7), end=max_d + timedelta(days=1), auto_adjust=True)
        if not hist50.empty:
            for d_str in dates:
                d = date.fromisoformat(d_str)
                on_date = hist50[hist50.index.date == d]
                if len(on_date):
                    nifty50_prices[d_str] = float(on_date.iloc[0]["Close"])
                else:
                    before = hist50[hist50.index.date <= d]
                    if len(before):
                        nifty50_prices[d_str] = float(before.iloc[-1]["Close"])
        
        # Nifty Next 50: Try multiple tickers
        # NIFTY_NEXT50.NS or use Junior Nifty ETF as proxy
        for nn50_ticker in ["^NSEMDCP50", "NIFTYJR.NS", "JUNIORBEES.NS"]:
            ticker_next50 = yf.Ticker(nn50_ticker)
            hist_next50 = ticker_next50.history(start=min_d - timedelta(days=7), end=max_d + timedelta(days=1), auto_adjust=True)
            if not hist_next50.empty:
                log.info(f"Using {nn50_ticker} for Nifty Next 50 data")
                break
        
        if not hist_next50.empty:
            for d_str in dates:
                d = date.fromisoformat(d_str)
                on_date = hist_next50[hist_next50.index.date == d]
                if len(on_date):
                    niftynext50_prices[d_str] = float(on_date.iloc[0]["Close"])
                else:
                    before = hist_next50[hist_next50.index.date <= d]
                    if len(before):
                        niftynext50_prices[d_str] = float(before.iloc[-1]["Close"])
    except Exception as e:
        log.warning("Failed to fetch index data: %s", e)
    
    # Simulate hypothetical index investments
    # Track units held in each index (as if we bought/sold same amounts as portfolio)
    nifty50_units = 0.0
    niftynext50_units = 0.0
    prev_cost = 0.0
    
    chart_data = []
    for i, d_str in enumerate(dates):
        pv = portfolio_data[i]["value"]
        pc = portfolio_data[i]["cost"]
        
        n50_price = nifty50_prices.get(d_str)
        nn50_price = niftynext50_prices.get(d_str)
        
        # Calculate investment/withdrawal since last date
        cost_change = pc - prev_cost
        
        if cost_change > 0 and n50_price and n50_price > 0:
            # New investment - buy index units
            nifty50_units += cost_change / n50_price
        elif cost_change < 0 and prev_cost > 0:
            # Withdrawal - sell proportional units
            withdrawal_ratio = abs(cost_change) / prev_cost
            nifty50_units *= (1 - withdrawal_ratio)
        
        if cost_change > 0 and nn50_price and nn50_price > 0:
            niftynext50_units += cost_change / nn50_price
        elif cost_change < 0 and prev_cost > 0:
            withdrawal_ratio = abs(cost_change) / prev_cost
            niftynext50_units *= (1 - withdrawal_ratio)
        
        # Calculate hypothetical values
        nifty50_value = nifty50_units * n50_price if n50_price else None
        niftynext50_value = niftynext50_units * nn50_price if nn50_price else None
        
        chart_data.append({
            "date": d_str,
            "portfolio_value": round(pv, 2),
            "portfolio_cost": round(pc, 2),
            "nifty50_value": round(nifty50_value, 2) if nifty50_value is not None else None,
            "niftynext50_value": round(niftynext50_value, 2) if niftynext50_value is not None else None,
            "nifty50_price": round(n50_price, 2) if n50_price else None,
            "niftynext50_price": round(nn50_price, 2) if nn50_price else None,
        })
        
        prev_cost = pc
    
    # Calculate returns based on total invested (cost) vs current value
    total_invested = portfolio_data[-1]["cost"]
    last_value = portfolio_data[-1]["value"]
    last_n50 = chart_data[-1]["nifty50_value"]
    last_nn50 = chart_data[-1]["niftynext50_value"]
    
    # Calculate returns properly
    # Portfolio return: (final_value - total_invested) / total_invested
    # Index return: Simple price change from first to last date
    
    first_value = portfolio_data[0]["value"]
    first_n50_price = nifty50_prices.get(dates[0])
    last_n50_price = nifty50_prices.get(dates[-1])
    first_nn50_price = niftynext50_prices.get(dates[0])
    last_nn50_price = niftynext50_prices.get(dates[-1])
    
    def calc_pct_change(start, end):
        if start and end and start > 0:
            return round((end - start) / start * 100, 2)
        return None
    
    # Portfolio absolute return (value - cost) / cost
    portfolio_return_pct = calc_pct_change(total_invested, last_value) if total_invested > 0 else None
    
    # Index returns: simple price appreciation over the period
    nifty50_return_pct = calc_pct_change(first_n50_price, last_n50_price)
    niftynext50_return_pct = calc_pct_change(first_nn50_price, last_nn50_price)
    
    log.info(f"Performance: invested={total_invested:.0f}, portfolio={last_value:.0f} ({portfolio_return_pct}%)")
    log.info(f"Nifty50: {first_n50_price} -> {last_n50_price} ({nifty50_return_pct}%)")
    log.info(f"NiftyNext50: {first_nn50_price} -> {last_nn50_price} ({niftynext50_return_pct}%)")
    log.info(f"Date range: {dates[0]} to {dates[-1]}, {len(dates)} data points")

    return jsonify({
        "data": chart_data,
        "summary": {
            "start_date": dates[0],
            "end_date": dates[-1],
            "portfolio_end": round(last_value, 2),
            "portfolio_return_pct": portfolio_return_pct,
            "nifty50_end": round(last_n50, 2) if last_n50 else None,
            "nifty50_return_pct": nifty50_return_pct,
            "niftynext50_end": round(last_nn50, 2) if last_nn50 else None,
            "niftynext50_return_pct": niftynext50_return_pct,
            "total_invested": round(total_invested, 2),
        }
    })


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


@app.route("/api/historical-value-by-month")
@require_auth
def historical_value_by_month():
    """
    Fetch portfolio value at the 1st of each month (e.g. Jan 1 last_year ... Jan 1 this_year)
    using Kite historical API. Uses current holdings and quantities; value = sum(qty * historical_close).
    Query params: from_year, from_month, to_year, to_month (defaults: Jan 1 last year → Jan 1 this year).
    """
    today = date.today()
    default_to_year, default_to_month = today.year, 1
    default_from_year, default_from_month = today.year - 1, 1
    from_year = int(request.args.get("from_year", default_from_year))
    from_month = int(request.args.get("from_month", default_from_month))
    to_year = int(request.args.get("to_year", default_to_year))
    to_month = int(request.args.get("to_month", default_to_month))
    rows, err = kite_api.fetch_portfolio_value_at_month_dates(
        session, from_year=from_year, from_month=from_month, to_year=to_year, to_month=to_month
    )
    if err:
        return jsonify({"error": err}), 400
    return jsonify({
        "note": "Value = current holdings valued at historical close on 1st of each month (equity only).",
        "rows": rows,
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


@app.route("/api/mf-list")
@require_auth
def mf_list():
    """Return list of mutual funds user has invested in."""
    db.init_db()
    today = date.today()
    cached = db.get_cached_day_on_or_before(today)
    
    mf_funds = []
    mf_holdings = []
    
    # Try to get from cache first
    if cached:
        try:
            mf_holdings = json.loads(cached.get("mf_holdings_json") or "[]")
        except (json.JSONDecodeError, TypeError):
            pass
    
    # If no cached MF holdings, fetch live from Kite
    if not mf_holdings:
        log.info("MF list: No cached MF holdings, fetching live from Kite")
        mf_list_live, mf_err = kite_api.fetch_mf_holdings(session)
        if mf_list_live:
            mf_holdings = mf_list_live
            # Save to cache for future use
            if cached:
                mf_val = sum(float(h.get("last_price", 0) or 0) * float(h.get("quantity", 0) or 0) for h in mf_holdings)
                db.update_cached_mf_only(today, mf_holdings, mf_val)
    
    for h in mf_holdings:
        sym = h.get("tradingsymbol", "")
        fund_name = h.get("fund", "") or sym
        if sym:
            mf_funds.append({
                "tradingsymbol": sym,
                "fund_name": fund_name,
                "quantity": h.get("quantity", 0),
                "average_price": h.get("average_price", 0),
                "last_price": h.get("last_price", 0),
                "pnl": h.get("pnl", 0),
                "isin": h.get("isin", ""),
            })
    
    return jsonify({"funds": mf_funds})


def _get_amfi_scheme_map():
    """Fetch and parse AMFI NAVAll.txt to build ISIN -> scheme_code mapping."""
    import requests
    try:
        url = 'https://www.amfiindia.com/spages/NAVAll.txt'
        r = requests.get(url, timeout=30)
        if r.status_code != 200:
            return {}
        
        scheme_map = {}  # isin -> {code, name, nav}
        for line in r.text.split('\n'):
            parts = line.split(';')
            if len(parts) >= 5 and parts[0].strip().isdigit():
                code = parts[0].strip()
                isin1 = parts[1].strip()
                isin2 = parts[2].strip()
                name = parts[3].strip()
                nav = parts[4].strip()
                
                if isin1 and isin1 != '-':
                    scheme_map[isin1] = {'code': code, 'name': name, 'nav': nav}
                if isin2 and isin2 != '-':
                    scheme_map[isin2] = {'code': code, 'name': name, 'nav': nav}
                # Also map by name parts for fuzzy matching
                scheme_map[code] = {'code': code, 'name': name, 'nav': nav}
        return scheme_map
    except Exception as e:
        log.warning("Failed to fetch AMFI scheme map: %s", str(e))
        return {}


def _fetch_mf_nav_history_mfapi(scheme_code, start_date, today):
    """Try to fetch NAV history from mfapi.in (free API)."""
    import requests
    nav_data = []
    try:
        url = f'https://api.mfapi.in/mf/{scheme_code}'
        r = requests.get(url, timeout=30)
        if r.status_code == 200:
            data = r.json()
            for entry in data.get('data', []):
                try:
                    nav_date = datetime.strptime(entry['date'], '%d-%m-%Y').date()
                    if nav_date >= start_date:
                        nav_data.append({
                            'date': nav_date.isoformat(),
                            'nav': float(entry['nav'])
                        })
                except (ValueError, KeyError):
                    continue
            nav_data.reverse()  # Oldest first
    except Exception as e:
        log.warning("mfapi.in fetch failed: %s", str(e))
    return nav_data


def _fetch_mf_nav_from_db(symbol, start_date):
    """Build NAV history from our cached MF holdings data."""
    nav_data = []
    try:
        rows = db.get_all_portfolio_days()
        for row in rows:
            try:
                d = datetime.strptime(row['date'], '%Y-%m-%d').date() if isinstance(row['date'], str) else row['date']
                if d < start_date:
                    continue
                mf_holdings = json.loads(row.get('mf_holdings_json') or '[]')
                for h in mf_holdings:
                    if h.get('tradingsymbol') == symbol:
                        nav_data.append({
                            'date': d.isoformat(),
                            'nav': float(h.get('last_price', 0))
                        })
                        break
            except (ValueError, KeyError, json.JSONDecodeError):
                continue
        # Sort by date
        nav_data.sort(key=lambda x: x['date'])
    except Exception as e:
        log.warning("DB NAV fetch failed: %s", str(e))
    return nav_data


@app.route("/api/mf-nav-history/<symbol>")
@require_auth
def mf_nav_history(symbol):
    """Return NAV history for a mutual fund using multiple data sources."""
    from datetime import timedelta
    
    # Get period from query params (default 1 year)
    period = request.args.get("period", "1y")
    
    # Get the ISIN and fund name from cached holdings or live fetch
    db.init_db()
    today = date.today()
    cached = db.get_cached_day_on_or_before(today)
    
    isin = None
    fund_name = symbol
    current_nav = 0
    mf_holdings = []
    
    # Try cache first
    if cached:
        try:
            mf_holdings = json.loads(cached.get("mf_holdings_json") or "[]")
        except (json.JSONDecodeError, TypeError):
            pass
    
    # If no cached MF holdings, fetch live
    if not mf_holdings:
        mf_holdings_live, _ = kite_api.fetch_mf_holdings(session)
        if mf_holdings_live:
            mf_holdings = mf_holdings_live
    
    for h in mf_holdings:
        if h.get("tradingsymbol") == symbol:
            isin = h.get("isin", "")
            fund_name = h.get("fund", "") or symbol
            current_nav = float(h.get("last_price", 0))
            break
    
    # Calculate date range based on period
    period_days = {
        "1m": 30, "3m": 90, "6m": 180, "1y": 365, 
        "2y": 730, "3y": 1095, "all": 3650
    }
    start_date = today - timedelta(days=period_days.get(period, 365))
    
    nav_data = []
    data_source = "none"
    scheme_code = None
    
    log.info(f"MF NAV request: symbol={symbol}, isin={isin}, period={period}")
    
    # Method 1: Try to get scheme code from AMFI and fetch from mfapi.in
    if isin:
        scheme_map = _get_amfi_scheme_map()
        if isin in scheme_map:
            scheme_code = scheme_map[isin]['code']
            log.info("Found scheme code %s for ISIN %s", scheme_code, isin)
            
            # Check cache first (valid for 1 day)
            cache_info = db.get_nav_cache_info(scheme_code)
            if cache_info:
                cached_at = datetime.fromisoformat(cache_info["cached_at"]) if cache_info.get("cached_at") else None
                cache_age_hours = (datetime.now() - cached_at).total_seconds() / 3600 if cached_at else 999
                if cache_age_hours < 24:
                    nav_data = db.get_cached_nav(scheme_code, start_date)
                    if nav_data:
                        data_source = "cache"
                        log.info(f"Using cached NAV data: {len(nav_data)} points, cached {cache_age_hours:.1f}h ago")
            
            # Fetch fresh if no cache or stale
            if not nav_data:
                nav_data = _fetch_mf_nav_history_mfapi(scheme_code, start_date, today)
                if nav_data:
                    data_source = "mfapi"
                    # Save to cache
                    db.save_nav_cache(scheme_code, nav_data)
                    log.info(f"Fetched and cached {len(nav_data)} NAV points from mfapi")
    
    # Method 2: Try mftool with scheme codes lookup
    if not nav_data:
        try:
            from mftool import Mftool
            mf = Mftool()
            
            if not scheme_code:
                # Try to find scheme by searching
                scheme_codes = mf.get_scheme_codes()
                fund_name_upper = fund_name.upper()
                
                for code, name in scheme_codes.items():
                    if code == 'Scheme Code':
                        continue
                    name_upper = name.upper()
                    # Match by fund name keywords
                    if fund_name_upper in name_upper or name_upper in fund_name_upper:
                        scheme_code = code
                        break
                    # Try matching key parts
                    fund_words = [w for w in fund_name_upper.split() if len(w) > 3]
                    if fund_words and all(w in name_upper for w in fund_words[:3]):
                        scheme_code = code
                        break
            
            if scheme_code:
                # Try get_scheme_historical_nav_for_dates (more reliable)
                try:
                    start_str = start_date.strftime('%d-%m-%Y')
                    end_str = today.strftime('%d-%m-%Y')
                    history = mf.get_scheme_historical_nav_for_dates(scheme_code, start_str, end_str)
                    if history and "data" in history:
                        for entry in history["data"]:
                            try:
                                nav_date = datetime.strptime(entry["date"], "%d-%m-%Y").date()
                                nav_data.append({
                                    "date": nav_date.isoformat(),
                                    "nav": float(entry["nav"])
                                })
                            except (ValueError, KeyError):
                                continue
                        nav_data.reverse()  # Oldest first
                        if nav_data:
                            data_source = "mftool"
                except Exception as e:
                    log.warning("mftool historical NAV failed: %s", str(e))
        except ImportError:
            log.info("mftool not available")
        except Exception as e:
            log.warning("mftool error: %s", str(e))
    
    # Method 3: Fallback to our own DB cache (NAV from daily portfolio snapshots)
    if not nav_data:
        nav_data = _fetch_mf_nav_from_db(symbol, start_date)
        if nav_data:
            data_source = "db_cache"
    
    # Add debug info
    if nav_data:
        log.info("MF NAV History for %s: source=%s, points=%d, scheme_code=%s",
                 symbol, data_source, len(nav_data), scheme_code)
    else:
        log.warning("MF NAV History for %s: NO DATA. isin=%s, scheme_code=%s, fund_name=%s",
                    symbol, isin, scheme_code, fund_name)
    
    # Calculate statistics
    stats = {}
    if nav_data:
        navs = [d["nav"] for d in nav_data]
        current_nav = navs[-1] if navs else 0
        min_nav = min(navs)
        max_nav = max(navs)
        avg_nav = sum(navs) / len(navs)
        
        # Calculate percentile of current NAV
        sorted_navs = sorted(navs)
        current_percentile = (sorted_navs.index(min(sorted_navs, key=lambda x: abs(x - current_nav))) / len(sorted_navs)) * 100
        
        # Calculate distance from min/max
        range_nav = max_nav - min_nav
        if range_nav > 0:
            position_in_range = ((current_nav - min_nav) / range_nav) * 100
        else:
            position_in_range = 50
        
        # Determine if it's a dip (current NAV in bottom 20% of range)
        is_dip = position_in_range <= 20
        is_near_dip = position_in_range <= 35
        
        # Calculate percentage dip from max and from average
        dip_from_max = ((max_nav - current_nav) / max_nav) * 100 if max_nav > 0 else 0
        dip_from_avg = ((avg_nav - current_nav) / avg_nav) * 100 if avg_nav > 0 else 0
        gain_from_min = ((current_nav - min_nav) / min_nav) * 100 if min_nav > 0 else 0
        
        # Calculate period return (from start of period to now)
        start_nav = navs[0] if navs else current_nav
        period_return = ((current_nav - start_nav) / start_nav) * 100 if start_nav > 0 else 0
        
        # Calculate returns for different periods
        period_returns = {}
        period_configs = [
            ("1m", 30), ("3m", 90), ("6m", 180), 
            ("1y", 365), ("2y", 730), ("3y", 1095)
        ]
        
        for period_key, days in period_configs:
            cutoff = today - timedelta(days=days)
            period_navs = [d for d in nav_data if datetime.strptime(d["date"], "%Y-%m-%d").date() >= cutoff]
            if period_navs:
                p_start = period_navs[0]["nav"]
                p_return = ((current_nav - p_start) / p_start) * 100 if p_start > 0 else 0
                period_returns[period_key] = round(p_return, 2)
            else:
                period_returns[period_key] = None
        
        stats = {
            "current_nav": round(current_nav, 4),
            "min_nav": round(min_nav, 4),
            "max_nav": round(max_nav, 4),
            "avg_nav": round(avg_nav, 4),
            "position_in_range": round(position_in_range, 1),
            "dip_from_max": round(dip_from_max, 2),
            "dip_from_avg": round(dip_from_avg, 2),
            "gain_from_min": round(gain_from_min, 2),
            "period_return": round(period_return, 2),
            "period_returns": period_returns,
            "is_dip": is_dip,
            "is_near_dip": is_near_dip,
            "days_analyzed": len(navs),
        }
    
    return jsonify({
        "symbol": symbol,
        "fund_name": fund_name,
        "nav_data": nav_data,
        "stats": stats,
        "data_source": data_source,
        "scheme_code": scheme_code,
    })


@app.route("/api/index-history/<symbol>")
@require_auth
def index_history(symbol):
    """Return historical data for an index (Nifty 50, Nifty Next 50, etc.)."""
    from datetime import timedelta
    import yfinance as yf
    
    period = request.args.get("period", "1y")
    
    period_days = {
        "1m": 30, "3m": 90, "6m": 180, "1y": 365,
        "2y": 730, "3y": 1095, "all": 3650
    }
    
    today = date.today()
    start_date = today - timedelta(days=period_days.get(period, 365))
    
    # Map symbol to Yahoo Finance ticker - try multiple options
    # ^NSEI = Nifty 50, ^NSMIDCP = Nifty Next 50 (Junior Nifty)
    ticker_options = {
        "NIFTY50": ["^NSEI"],
        "NIFTYNEXT50": ["^NSMIDCP"],
        "NIFTYMIDCAP": ["^NSEMDCP50"],
        "SENSEX": ["^BSESN"]
    }
    
    tickers_to_try = ticker_options.get(symbol, [symbol])
    log.info(f"Index history request: {symbol}, will try: {tickers_to_try}")
    
    hist = None
    yf_symbol = symbol
    
    for yf_symbol in tickers_to_try:
        try:
            ticker = yf.Ticker(yf_symbol)
            hist = ticker.history(start=start_date.isoformat(), end=today.isoformat())
            if len(hist) > 0:
                log.info(f"Got {len(hist)} data points for {yf_symbol}")
                break
            else:
                log.warning(f"No data for {yf_symbol}, trying next...")
        except Exception as e:
            log.warning(f"Failed to fetch {yf_symbol}: {e}")
            continue
    
    try:
        nav_data = []
        if hist is not None and len(hist) > 0:
            for idx, row in hist.iterrows():
                nav_data.append({
                    "date": idx.strftime("%Y-%m-%d"),
                    "close": round(row["Close"], 2),
                    "nav": round(row["Close"], 2)
                })
        
        if nav_data:
            start_val = nav_data[0]["close"]
            end_val = nav_data[-1]["close"]
            period_return = ((end_val - start_val) / start_val) * 100 if start_val > 0 else 0
        else:
            period_return = 0
        
        return jsonify({
            "symbol": symbol,
            "yf_symbol": yf_symbol,
            "period": period,
            "nav_data": nav_data,
            "data": nav_data,
            "stats": {
                "period_return": round(period_return, 2),
                "data_points": len(nav_data)
            }
        })
        
    except Exception as e:
        log.error(f"Error fetching index history for {symbol}: {e}")
        return jsonify({
            "error": str(e),
            "symbol": symbol,
            "nav_data": [],
            "data": []
        }), 500


if __name__ == "__main__":
    config.ensure_data_dir()
    _migrate_data_to_data_dir()
    db.init_db()
    app.run(debug=True, port=5000)
