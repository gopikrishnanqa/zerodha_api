"""
Turso (libSQL) remote sync: dual-write portfolio data to Turso when configured.
Uses Turso HTTP API (v2/pipeline). Local DB remains source of truth for reads.
"""
import json
import logging
from datetime import date, datetime

import requests

import config

log = logging.getLogger(__name__)


def _turso_enabled():
    return bool(config.TURSO_DATABASE_URL and config.TURSO_AUTH_TOKEN)


def validate_turso_connection():
    """
    Validate Turso URL and auth token. Returns (success: bool, message: str).
    Runs a simple SELECT 1 to verify both connectivity and token.
    """
    if not config.TURSO_DATABASE_URL or not config.TURSO_AUTH_TOKEN:
        return False, "TURSO_DATABASE_URL and TURSO_AUTH_TOKEN must be set in .env"
    base = _turso_base_url()
    if not base:
        return False, "Invalid TURSO_DATABASE_URL (use libsql://... or https://...)"
    url = base + "/v2/pipeline"
    payload = {
        "requests": [
            {"type": "execute", "stmt": {"sql": "SELECT 1"}},
            {"type": "close"},
        ]
    }
    try:
        r = requests.post(
            url,
            json=payload,
            headers={
                "Authorization": "Bearer " + config.TURSO_AUTH_TOKEN,
                "Content-Type": "application/json",
            },
            timeout=15,
        )
        if not r.ok:
            return False, "HTTP %s: %s" % (r.status_code, (r.text or "No body")[:300])
        data = r.json()
        results = data.get("results") or []
        for res in results:
            if isinstance(res, dict) and res.get("type") == "error":
                err = res.get("error", res)
                return False, "Turso error: %s" % (err.get("message", err))
        return True, "Connected successfully"
    except requests.exceptions.Timeout:
        return False, "Connection timed out (check URL and network)"
    except requests.exceptions.ConnectionError as e:
        return False, "Connection failed: %s" % str(e)[:200]
    except Exception as e:
        return False, str(e)[:200]


def _turso_base_url():
    """Convert libsql://host to https://host for HTTP API."""
    u = (config.TURSO_DATABASE_URL or "").strip()
    if u.startswith("libsql://"):
        return "https://" + u[len("libsql://"):].rstrip("/")
    if u.startswith("https://"):
        return u.rstrip("/")
    return u.rstrip("/") if u else ""


def _sanitize_float(x):
    """Replace NaN/Inf with 0 so Turso accepts the value."""
    if x is None:
        return None
    try:
        f = float(x)
        if f != f or f == float("inf") or f == float("-inf"):
            return 0.0
        return f
    except (TypeError, ValueError):
        return 0.0


def _arg_value(v):
    """Convert Python value to Turso API arg. Turso: float value as JSON number (f64), integer value as string."""
    if v is None:
        return {"type": "null"}
    if isinstance(v, bool):
        return {"type": "integer", "value": "1" if v else "0"}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        v = _sanitize_float(v) or 0.0
        return {"type": "float", "value": v}
    s = str(v)
    if len(s) > 1_000_000:
        log.warning("Turso: truncating very long arg (len=%d) to 1MB", len(s))
        s = s[:1_000_000]
    return {"type": "text", "value": s}


def _execute_one(sql: str, args: list | None = None) -> dict | None:
    """Execute a single SQL statement on Turso. Returns pipeline response or None on failure."""
    base = _turso_base_url()
    if not base:
        return None
    url = base + "/v2/pipeline"
    stmt = {"sql": sql}
    if args is not None:
        stmt["args"] = [_arg_value(a) for a in args]
    payload = {
        "requests": [
            {"type": "execute", "stmt": stmt},
            {"type": "close"},
        ]
    }
    try:
        r = requests.post(
            url,
            json=payload,
            headers={
                "Authorization": "Bearer " + config.TURSO_AUTH_TOKEN,
                "Content-Type": "application/json",
            },
            timeout=30,
        )
        if not r.ok:
            log.warning("Turso execute failed: HTTP %s %s", r.status_code, r.text[:500] if r.text else "")
            return None
        data = r.json()
        if isinstance(data.get("results"), list):
            for i, res in enumerate(data["results"]):
                if isinstance(res, dict) and res.get("type") == "error":
                    err = res.get("error", res)
                    log.warning("Turso execute error in result[%s]: %s", i, err)
                    return None
        return data
    except Exception as e:
        log.warning("Turso execute failed: %s", e)
        return None


def _execute_many(requests_list: list) -> tuple[dict | None, str | None]:
    """Run multiple execute requests in one pipeline. Returns (data, None) on success, (None, error_message) on failure."""
    base = _turso_base_url()
    if not base:
        return None, "Turso base URL not set"
    url = base + "/v2/pipeline"
    reqs = []
    for sql, args in requests_list:
        stmt = {"sql": sql}
        if args is not None:
            stmt["args"] = [_arg_value(a) for a in args]
        reqs.append({"type": "execute", "stmt": stmt})
    reqs.append({"type": "close"})
    try:
        r = requests.post(
            url,
            json={"requests": reqs},
            headers={
                "Authorization": "Bearer " + config.TURSO_AUTH_TOKEN,
                "Content-Type": "application/json",
            },
            timeout=60,
        )
        if not r.ok:
            msg = r.text[:800] if r.text else ""
            log.warning("Turso pipeline failed: HTTP %s %s", r.status_code, msg)
            return None, "HTTP %s: %s" % (r.status_code, msg[:200] or "No body")
        data = r.json()
        results = data.get("results") or []
        for i, res in enumerate(results):
            if not isinstance(res, dict):
                continue
            if res.get("type") == "error":
                err = res.get("error", res)
                msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                log.warning("Turso pipeline error in result[%s]: %s. Full response: %s", i, err, json.dumps(data)[:1500])
                return None, msg
            # Some APIs put error inside response
            resp = res.get("response") or {}
            if isinstance(resp, dict) and resp.get("type") == "error":
                err = resp.get("error", resp)
                msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                log.warning("Turso pipeline error in result[%s].response: %s. Full response: %s", i, err, json.dumps(data)[:1500])
                return None, msg
        return data, None
    except Exception as e:
        log.warning("Turso pipeline failed: %s", e)
        return None, str(e)[:200]


def init_turso_tables():
    """Ensure all required tables exist on Turso."""
    if not _turso_enabled():
        return

    # Aggregate snapshots per day
    _execute_one("""
        CREATE TABLE IF NOT EXISTS portfolio_daily (
            date TEXT PRIMARY KEY,
            portfolio_value REAL NOT NULL,
            portfolio_cost REAL NOT NULL,
            buy_amount REAL NOT NULL,
            sell_amount REAL NOT NULL,
            month_buy REAL NOT NULL,
            month_sell REAL NOT NULL,
            month_per_stock_json TEXT,
            num_holdings INTEGER DEFAULT 0,
            mf_portfolio_value REAL DEFAULT 0,
            mf_portfolio_cost REAL DEFAULT 0,
            price_changes_json TEXT,
            created_at TEXT
        )
    """)
    # Per-stock snapshots per day
    _execute_one("""
        CREATE TABLE IF NOT EXISTS holdings_equity_daily (
            date TEXT NOT NULL,
            tradingsymbol TEXT NOT NULL,
            exchange TEXT NOT NULL,
            data_json TEXT NOT NULL,
            created_at TEXT,
            PRIMARY KEY (date, tradingsymbol, exchange)
        )
    """)
    _execute_one("""
        CREATE TABLE IF NOT EXISTS holdings_mf_daily (
            date TEXT NOT NULL,
            tradingsymbol TEXT NOT NULL,
            folio TEXT NOT NULL,
            data_json TEXT NOT NULL,
            created_at TEXT,
            PRIMARY KEY (date, tradingsymbol, folio)
        )
    """)

    # Archive tables for weekly snapshots
    _execute_one("""
        CREATE TABLE IF NOT EXISTS portfolio_archive (
            date TEXT NOT NULL,
            portfolio_value REAL NOT NULL,
            portfolio_cost REAL NOT NULL,
            buy_amount REAL NOT NULL,
            sell_amount REAL NOT NULL,
            month_buy REAL NOT NULL,
            month_sell REAL NOT NULL,
            month_per_stock_json TEXT,
            num_holdings INTEGER DEFAULT 0,
            mf_portfolio_value REAL DEFAULT 0,
            mf_portfolio_cost REAL DEFAULT 0,
            price_changes_json TEXT,
            created_at TEXT,
            archived_at TEXT
        )
    """)
    _execute_one("""
        CREATE TABLE IF NOT EXISTS holdings_equity_archive (
            date TEXT NOT NULL,
            tradingsymbol TEXT NOT NULL,
            exchange TEXT NOT NULL,
            data_json TEXT NOT NULL,
            created_at TEXT,
            archived_at TEXT
        )
    """)
    _execute_one("""
        CREATE TABLE IF NOT EXISTS holdings_mf_archive (
            date TEXT NOT NULL,
            tradingsymbol TEXT NOT NULL,
            folio TEXT NOT NULL,
            data_json TEXT NOT NULL,
            created_at TEXT,
            archived_at TEXT
        )
    """)
    _execute_one("""
        CREATE TABLE IF NOT EXISTS transactions (
            id TEXT PRIMARY KEY,
            date TEXT NOT NULL,
            type TEXT NOT NULL,
            instrument_type TEXT NOT NULL,
            tradingsymbol TEXT NOT NULL,
            exchange TEXT NOT NULL,
            quantity REAL NOT NULL,
            price REAL NOT NULL,
            amount REAL NOT NULL,
            data_json TEXT,
            created_at TEXT
        )
    """)
    _execute_one("""
        CREATE TABLE IF NOT EXISTS checklist (
            period_type TEXT NOT NULL,
            period_key TEXT NOT NULL,
            state_json TEXT NOT NULL DEFAULT '{}',
            custom_json TEXT NOT NULL DEFAULT '[]',
            archived_json TEXT NOT NULL DEFAULT '[]',
            updated_at TEXT,
            PRIMARY KEY (period_type, period_key)
        )
    """)
    log.info("Turso tables ensured (init_turso_tables)")


def set_checklist_turso(period_type: str, period_key: str, state: dict, custom: list, archived: list) -> None:
    """Mirror set_checklist to Turso."""
    if not _turso_enabled():
        return
    now = datetime.now().isoformat()
    state_json = json.dumps(state)
    custom_json = json.dumps(custom)
    archived_json = json.dumps(archived)
    out = _execute_one(
        """INSERT OR REPLACE INTO checklist (period_type, period_key, state_json, custom_json, archived_json, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (period_type, period_key, state_json, custom_json, archived_json, now),
    )
    if out is None:
        log.warning("Turso set_checklist failed for %s %s", period_type, period_key)
    else:
        log.info("Turso sync: checklist %s %s", period_type, period_key)


def save_portfolio_day_turso(
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
    mf_portfolio_cost: float = 0,
    price_changes: dict | None = None,
):
    """Mirror save_portfolio_day to Turso (same week + same month archiving as local; never archive across month)."""
    if not _turso_enabled():
        return
    mf_holdings = mf_holdings or []
    price_changes = price_changes or {}
    date_str = d.isoformat()
    now = datetime.now().isoformat()

    # Archiving logic on Turso: since we can't easily do a multi-step isocalendar check 
    # without multiple round trips, we'll use a slightly simplified approach or 
    # fetch existing dates first if needed. But for sync, we can just follow local.
    
    # Actually, Turso's save_portfolio_day_turso is often called during live fetch,
    # so we should replicate the "move to archive" logic here too.
    
    year, week, weekday = d.isocalendar()
    
    # We can't easily "move" rows between tables in one statement with sqlite logic on Turso pipeline
    # if we don't know the exact dates. So we first fetch existing dates in that ISO week.
    
    # For now, let's keep it consistent with daily_portfolio being the "current week's active record".
    # Local DB is the source of truth, but we want Turso to match.
    
    # Simplified: identify records for this ISO week. 
    # Since we don't have isocalendar in SQL, we'll fetch all dates from Turso for this year
    # and check them.
    
    # 1. Fetch dates for this year
    dates_resp = _execute_one("SELECT date FROM portfolio_daily WHERE date LIKE ?", [f"{d.year}%"])
    to_archive = []
    if dates_resp and "results" in dates_resp:
        for res in dates_resp["results"]:
            if res.get("type") == "error": continue
            resp = res.get("response") or {}
            rows = resp.get("queryset", {}).get("rows", [])
            for r in rows:
                if not r: continue
                r_date_str = r[0].get("value")
                if r_date_str == date_str: continue
                try:
                    rd = date.fromisoformat(r_date_str)
                    ry, rw, rwd = rd.isocalendar()
                    if ry == year and rw == week and rd.month == d.month:
                        to_archive.append(r_date_str)
                except Exception: continue

    reqs = []
    for adate in to_archive:
        # Move aggregates
        reqs.append((
            "INSERT INTO portfolio_archive SELECT *, ? as archived_at FROM portfolio_daily WHERE date = ?",
            [now, adate]
        ))
        # Move Holdings
        reqs.append((
            "INSERT INTO holdings_equity_archive SELECT *, ? as archived_at FROM holdings_equity_daily WHERE date = ?",
            [now, adate]
        ))
        reqs.append((
            "INSERT INTO holdings_mf_archive SELECT *, ? as archived_at FROM holdings_mf_daily WHERE date = ?",
            [now, adate]
        ))
        # Delete from daily
        reqs.append(("DELETE FROM portfolio_daily WHERE date = ?", [adate]))
        reqs.append(("DELETE FROM holdings_equity_daily WHERE date = ?", [adate]))
        reqs.append(("DELETE FROM holdings_mf_daily WHERE date = ?", [adate]))

    # Add the main insert
    reqs.append((
        """INSERT OR REPLACE INTO portfolio_daily
        (date, portfolio_value, portfolio_cost, buy_amount, sell_amount,
         month_buy, month_sell, month_per_stock_json, num_holdings,
         mf_portfolio_value, mf_portfolio_cost, price_changes_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            date_str,
            _sanitize_float(portfolio_value) or 0,
            _sanitize_float(portfolio_cost) or 0,
            _sanitize_float(buy_amount) or 0,
            _sanitize_float(sell_amount) or 0,
            _sanitize_float(month_buy) or 0,
            _sanitize_float(month_sell) or 0,
            json.dumps(month_per_stock),
            len(holdings),
            _sanitize_float(mf_portfolio_value) or 0,
            _sanitize_float(mf_portfolio_cost) or 0,
            json.dumps(price_changes),
            now,
        )
    ))

    if reqs:
        out, err = _execute_many(reqs)
        if err:
            log.error("Turso sync: portfolio/archive pipeline failed for %s: %s", date_str, err)
            # We don't raise error here to avoid blocking local save if Turso fails
    
    # Individual holdings inserts (separate because of potential large volume)
    _execute_one("DELETE FROM holdings_equity_daily WHERE date = ?", (date_str,))
    for h in holdings:
        sym = h.get("tradingsymbol") or ""
        ex = (h.get("exchange") or "NSE").strip()
        if not sym: continue
        _execute_one(
            "INSERT OR REPLACE INTO holdings_equity_daily (date, tradingsymbol, exchange, data_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (date_str, sym, ex, json.dumps(h), now),
        )
    
    _execute_one("DELETE FROM holdings_mf_daily WHERE date = ?", (date_str,))
    for h in mf_holdings:
        sym = h.get("tradingsymbol") or ""
        folio = str(h.get("folio") or "")
        _execute_one(
            "INSERT OR REPLACE INTO holdings_mf_daily (date, tradingsymbol, folio, data_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (date_str, sym, folio, json.dumps(h), now),
        )
    log.info("Turso sync: saved portfolio for %s with archiving (%d equity, %d MF)", date_str, len(holdings), len(mf_holdings))


def update_cached_mf_only_turso(d: date, mf_holdings: list, mf_portfolio_value: float) -> None:
    """Mirror update_cached_mf_only to Turso."""
    if not _turso_enabled():
        return
    date_str = d.isoformat()
    now = datetime.now().isoformat()
    _execute_one("UPDATE portfolio_daily SET mf_portfolio_value = ? WHERE date = ?", (mf_portfolio_value, date_str))
    _execute_one("DELETE FROM holdings_mf_daily WHERE date = ?", (date_str,))
    for h in mf_holdings:
        sym = h.get("tradingsymbol") or ""
        folio = str(h.get("folio") or "")
        _execute_one(
            "INSERT OR REPLACE INTO holdings_mf_daily (date, tradingsymbol, folio, data_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (date_str, sym, folio, json.dumps(h), now),
        )
    log.info("Turso sync: updated MF for %s", date_str)


def clear_portfolio_cache_turso() -> None:
    """Mirror clear_portfolio_cache to Turso."""
    if not _turso_enabled():
        return
    _execute_one("DELETE FROM portfolio_daily")
    _execute_one("DELETE FROM holdings_equity_daily")
    _execute_one("DELETE FROM holdings_mf_daily")
    log.info("Turso sync: cleared portfolio cache")


def save_transactions_turso(transactions: list) -> None:
    """Mirror save_transactions to Turso."""
    if not _turso_enabled():
        return
    now = datetime.now().isoformat()
    reqs = []
    for t in transactions:
        tid = str(t.get("id") or t.get("order_id") or "")
        if not tid: continue
        reqs.append((
            """INSERT OR REPLACE INTO transactions 
               (id, date, type, instrument_type, tradingsymbol, exchange, quantity, price, amount, data_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                tid,
                t.get("date"),
                t.get("type"),
                t.get("instrument_type"),
                t.get("tradingsymbol"),
                t.get("exchange", ""),
                _sanitize_float(t.get("quantity")) or 0,
                _sanitize_float(t.get("price")) or 0,
                _sanitize_float(t.get("amount")) or 0,
                json.dumps(t),
                now
            )
        ))
    if reqs:
        _execute_many(reqs)
        log.info("Turso sync: saved %d transactions", len(reqs))

