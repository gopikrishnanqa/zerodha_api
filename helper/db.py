"""
SQLite persistence: portfolio_daily (one row per day for aggregates) and
holdings_equity_daily / holdings_mf_daily (one row per stock/MF per day).
DB file lives in data/ folder. When Turso is configured, all writes are
dual-written to Turso so local and remote stay in sync.
"""
import json
import logging
import sqlite3
from datetime import date, datetime
from pathlib import Path

from config import DB_PATH

log = logging.getLogger(__name__)


def _is_trading_day(d: date) -> bool:
    """Check if a date is likely a trading day (weekday, not major holiday)."""
    if d.weekday() >= 5:
        return False
    nse_holidays_2026 = {
        date(2026, 1, 26),  # Republic Day
        date(2026, 3, 10),  # Holi
        date(2026, 3, 30),  # Id-Ul-Fitr (tentative)
        date(2026, 4, 2),   # Ram Navami
        date(2026, 4, 3),   # Good Friday
        date(2026, 4, 14),  # Dr. Ambedkar Jayanti
        date(2026, 5, 1),   # Maharashtra Day
        date(2026, 6, 5),   # Id-Ul-Adha (tentative)
        date(2026, 7, 6),   # Muharram (tentative)
        date(2026, 8, 15),  # Independence Day
        date(2026, 9, 4),   # Milad-un-Nabi (tentative)
        date(2026, 10, 2),  # Gandhi Jayanti
        date(2026, 10, 20), # Dussehra
        date(2026, 11, 5),  # Diwali-Laxmi Puja (tentative)
        date(2026, 11, 6),  # Diwali-Balipratipada
        date(2026, 11, 27), # Gurunanak Jayanti
        date(2026, 12, 25), # Christmas
    }
    return d not in nse_holidays_2026

def _turso_sync():
    """Lazy import to avoid loading config/requests when Turso not used."""
    try:
        from helper import turso_sync as ts
        return ts
    except Exception as e:
        log.debug("Turso sync not available: %s", e)
        return None


def get_db():
    """Get SQLite connection."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    print(f"DEBUG: init_db using DB_PATH={DB_PATH}")
    conn = get_db()
    print(f"DEBUG: Connected to DB")
    # One row per day: totals only (no big JSON blobs)
    conn.execute("""
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
    # One row per equity holding per date
    conn.execute("""
        CREATE TABLE IF NOT EXISTS holdings_equity_daily (
            date TEXT NOT NULL,
            tradingsymbol TEXT NOT NULL,
            exchange TEXT NOT NULL,
            data_json TEXT NOT NULL,
            created_at TEXT,
            PRIMARY KEY (date, tradingsymbol, exchange)
        )
    """)
    # One row per MF holding per date
    conn.execute("""
        CREATE TABLE IF NOT EXISTS holdings_mf_daily (
            date TEXT NOT NULL,
            tradingsymbol TEXT NOT NULL,
            folio TEXT NOT NULL,
            data_json TEXT NOT NULL,
            created_at TEXT,
            PRIMARY KEY (date, tradingsymbol, folio)
        )
    """)

    # Archive tables (older fetches from same week+month when we save a new snapshot)
    conn.execute("""
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS holdings_equity_archive (
            date TEXT NOT NULL,
            tradingsymbol TEXT NOT NULL,
            exchange TEXT NOT NULL,
            data_json TEXT NOT NULL,
            created_at TEXT,
            archived_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS holdings_mf_archive (
            date TEXT NOT NULL,
            tradingsymbol TEXT NOT NULL,
            folio TEXT NOT NULL,
            data_json TEXT NOT NULL,
            created_at TEXT,
            archived_at TEXT
        )
    """)

    # Transactions ledger
    conn.execute("""
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
    conn.commit()

    # Checklist: one row per period (month/year/quarter) and key (e.g. 2026-02, 2026, 2026-Q1)
    conn.execute("""
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mf_nav_cache (
            scheme_code TEXT NOT NULL,
            nav_date TEXT NOT NULL,
            nav REAL NOT NULL,
            cached_at TEXT NOT NULL,
            PRIMARY KEY (scheme_code, nav_date)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_mf_nav_cache_scheme ON mf_nav_cache(scheme_code)
    """)
    conn.commit()

    # Add mf_portfolio_cost if missing (for category-wise invested amount)
    try:
        conn.execute("ALTER TABLE portfolio_daily ADD COLUMN mf_portfolio_cost REAL DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    # Migrate old schema: if portfolio_daily has holdings_json, backfill holdings tables for each date
    try:
        rows = conn.execute("SELECT date FROM portfolio_daily LIMIT 1").fetchone()
        if rows and "holdings_json" in rows.keys():
            log.info("Starting holdings migration...")
            rows = conn.execute("SELECT date, holdings_json, mf_holdings_json FROM portfolio_daily").fetchall()
            for row in rows:
                hj = row["holdings_json"]
                mj = row["mf_holdings_json"]
                if row["date"] and (hj or mj):
                    _migrate_holdings_from_json(conn, row["date"], hj or "[]", mj or "[]")
            conn.commit()
            log.info("Holdings migration complete.")
    except sqlite3.OperationalError:
        pass

    conn.close()
    ts = _turso_sync()
    if ts:
        try:
            ts.init_turso_tables()
        except Exception as e:
            log.warning("Turso init_tables failed: %s", e)


def get_checklist(period_type: str, period_key: str) -> dict | None:
    """Return { state: dict, custom: list, archived: list } for the period, or None if not found."""
    conn = get_db()
    row = conn.execute(
        "SELECT state_json, custom_json, archived_json FROM checklist WHERE period_type = ? AND period_key = ?",
        (period_type, period_key),
    ).fetchone()
    conn.close()
    if row is None:
        return None
    try:
        return {
            "state": json.loads(row["state_json"] or "{}"),
            "custom": json.loads(row["custom_json"] or "[]"),
            "archived": json.loads(row["archived_json"] or "[]"),
        }
    except (TypeError, ValueError):
        return None


def set_checklist(period_type: str, period_key: str, state: dict, custom: list, archived: list) -> None:
    """Save checklist data for the period. state is id->bool, custom is list of strings, archived is list of {id, label}."""
    conn = get_db()
    now = datetime.now().isoformat()
    conn.execute(
        """INSERT OR REPLACE INTO checklist (period_type, period_key, state_json, custom_json, archived_json, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (period_type, period_key, json.dumps(state), json.dumps(custom), json.dumps(archived), now),
    )
    conn.commit()
    conn.close()
    log.info("Checklist saved: %s %s", period_type, period_key)
    ts = _turso_sync()
    if ts:
        try:
            ts.set_checklist_turso(period_type, period_key, state, custom, archived)
        except Exception as e:
            log.warning("Turso set_checklist failed: %s", e)


def get_all_checklist_rows() -> list:
    """Return all checklist rows for sync: list of (period_type, period_key, state, custom, archived)."""
    conn = get_db()
    rows = conn.execute(
        "SELECT period_type, period_key, state_json, custom_json, archived_json FROM checklist"
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        try:
            out.append((
                r["period_type"],
                r["period_key"],
                json.loads(r["state_json"] or "{}"),
                json.loads(r["custom_json"] or "[]"),
                json.loads(r["archived_json"] or "[]"),
            ))
        except (TypeError, ValueError):
            continue
    return out


def _migrate_holdings_from_json(conn, d: str, holdings_json: str, mf_holdings_json: str) -> None:
    """One-time: copy from old holdings_json/mf_holdings_json into holdings_equity_daily and holdings_mf_daily."""
    try:
        equity = json.loads(holdings_json or "[]")
        mf = json.loads(mf_holdings_json or "[]")
    except Exception:
        return
    now = datetime.now().isoformat()
    for h in equity:
        sym = h.get("tradingsymbol") or ""
        ex = (h.get("exchange") or "NSE").strip()
        if not sym:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO holdings_equity_daily (date, tradingsymbol, exchange, data_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (d, sym, ex, json.dumps(h), now),
        )
    for h in mf:
        sym = h.get("tradingsymbol") or ""
        folio = str(h.get("folio") or "")
        conn.execute(
            "INSERT OR REPLACE INTO holdings_mf_daily (date, tradingsymbol, folio, data_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (d, sym, folio, json.dumps(h), now),
        )
    log.info("Migrated holdings to equity_daily and mf_daily for date %s", d)


def _row_key(row, key, default=None):
    """Safe read from sqlite3.Row (no .get); use default if key missing."""
    try:
        if key in row.keys():
            return row[key]
    except (TypeError, KeyError):
        pass
    return default


def get_cached_day(d: date) -> dict | None:
    """Return cached data for date: aggregates from portfolio_daily (or portfolio_archive) + holdings from daily or archive tables."""
    conn = get_db()
    date_str = d.isoformat()
    row = conn.execute(
        "SELECT * FROM portfolio_daily WHERE date = ?",
        (date_str,),
    ).fetchone()
    from_daily = True
    if row is None:
        row = conn.execute(
            "SELECT * FROM portfolio_archive WHERE date = ?",
            (date_str,),
        ).fetchone()
        from_daily = False
    if row is None:
        conn.close()
        return None
    out = {
        "date": _row_key(row, "date", ""),
        "portfolio_value": _row_key(row, "portfolio_value", 0),
        "portfolio_cost": _row_key(row, "portfolio_cost", 0),
        "buy_amount": _row_key(row, "buy_amount", 0),
        "sell_amount": _row_key(row, "sell_amount", 0),
        "month_buy": _row_key(row, "month_buy", 0),
        "month_sell": _row_key(row, "month_sell", 0),
        "month_per_stock_json": _row_key(row, "month_per_stock_json") or "{}",
        "num_holdings": _row_key(row, "num_holdings", 0) or 0,
        "mf_portfolio_value": float(_row_key(row, "mf_portfolio_value", 0) or 0),
        "mf_portfolio_cost": float(_row_key(row, "mf_portfolio_cost", 0) or 0),
        "price_changes_json": _row_key(row, "price_changes_json"),
    }
    if from_daily:
        equity_rows = conn.execute(
            "SELECT tradingsymbol, exchange, data_json FROM holdings_equity_daily WHERE date = ? ORDER BY tradingsymbol",
            (date_str,),
        ).fetchall()
        mf_rows = conn.execute(
            "SELECT tradingsymbol, folio, data_json FROM holdings_mf_daily WHERE date = ? ORDER BY tradingsymbol",
            (date_str,),
        ).fetchall()
    else:
        equity_rows = conn.execute(
            "SELECT tradingsymbol, exchange, data_json FROM holdings_equity_archive WHERE date = ? ORDER BY tradingsymbol",
            (date_str,),
        ).fetchall()
        mf_rows = conn.execute(
            "SELECT tradingsymbol, folio, data_json FROM holdings_mf_archive WHERE date = ? ORDER BY tradingsymbol",
            (date_str,),
        ).fetchall()
    if equity_rows:
        holdings = [json.loads(r["data_json"]) for r in equity_rows]
    else:
        hj = _row_key(row, "holdings_json") if from_daily else None
        try:
            holdings = json.loads(hj) if hj else []
        except Exception:
            holdings = []
    if mf_rows:
        mf_holdings = [json.loads(r["data_json"]) for r in mf_rows]
    else:
        mj = _row_key(row, "mf_holdings_json") if from_daily else None
        try:
            mf_holdings = json.loads(mj) if mj else []
        except Exception:
            mf_holdings = []
    conn.close()
    out["holdings_json"] = json.dumps(holdings)
    out["mf_holdings_json"] = json.dumps(mf_holdings)
    return out


def get_previous_date(d: date) -> date | None:
    """Return the most recent stored date before d, checking both daily and archive."""
    conn = get_db()
    # Check both tables and take the MAX date that is < d
    query = """
        SELECT MAX(date) as last_date FROM (
            SELECT date FROM portfolio_daily WHERE date < ?
            UNION ALL
            SELECT date FROM portfolio_archive WHERE date < ?
        )
    """
    row = conn.execute(query, (d.isoformat(), d.isoformat())).fetchone()
    conn.close()
    if row is None or row["last_date"] is None:
        return None
    return date.fromisoformat(row["last_date"])


def get_cached_day_on_or_before(d: date) -> dict | None:
    """Return full cached row for the most recent stored date on or before d (e.g. value as of 1st of last month)."""
    conn = get_db()
    row = conn.execute(
        "SELECT date FROM portfolio_daily WHERE date <= ? ORDER BY date DESC LIMIT 1",
        (d.isoformat(),)
    ).fetchone()
    conn.close()
    if row is None:
        return None
    return get_cached_day(date.fromisoformat(row["date"]))


def _detect_transactions_from_holdings(conn, d: date, holdings: list, mf_holdings: list) -> list:
    """Compare current holdings with previous day to detect implicit transactions.
    - Get all stocks, compare quantity and realised_quantity (remaining = T+1).
    - Only create a transaction when there is an actual change vs previous snapshot.
    - Attribute transactions to the older (previous) date, not the latest run date.
    """
    detected = []
    today_str = d.isoformat()

    # Skip transaction detection on non-trading days (weekends/holidays)
    if not _is_trading_day(d):
        log.info("Skipping transaction detection for %s (non-trading day)", today_str)
        return detected

    # Load previous day's equity (and MF) first; only use when immediate previous day (no missing date)
    prev_date = get_previous_date(d)
    is_recent = False
    prev_equity = {}
    prev_mf = {}
    if prev_date:
        days_diff = (d - prev_date).days
        if days_diff == 1:
            is_recent = True
        else:
            log.info("Previous snapshot for %s is %d days old (%s). Skipping to avoid ghost trades.", today_str, days_diff, prev_date.isoformat())

    if is_recent and prev_date:
        pdate_str = prev_date.isoformat()
        prev_equity_rows = conn.execute(
            "SELECT tradingsymbol, data_json FROM holdings_equity_daily WHERE date = ?", (pdate_str,)
        ).fetchall()
        if not prev_equity_rows:
            prev_equity_rows = conn.execute(
                "SELECT tradingsymbol, data_json FROM holdings_equity_archive WHERE date = ?", (pdate_str,)
            ).fetchall()
        prev_equity = {r["tradingsymbol"]: json.loads(r["data_json"]) for r in prev_equity_rows}
        prev_mf_rows = conn.execute(
            "SELECT tradingsymbol, folio, data_json FROM holdings_mf_daily WHERE date = ?", (pdate_str,)
        ).fetchall()
        if not prev_mf_rows:
            prev_mf_rows = conn.execute(
                "SELECT tradingsymbol, folio, data_json FROM holdings_mf_archive WHERE date = ?", (pdate_str,)
            ).fetchall()
        prev_mf = {(r["tradingsymbol"], r["folio"]): json.loads(r["data_json"]) for r in prev_mf_rows}

    # Use older (previous) date for all inferred transactions, not latest
    tx_date = prev_date.isoformat() if (is_recent and prev_date) else today_str

    # 1. T1 (remaining = quantity - realised_quantity): only when we have prev snapshot to compare
    #    Record only when remaining actually increased vs prev; attribute to older date.
    if not (is_recent and prev_date):
        pass  # skip T1 and diff when any date is missing (gap)
    else:
        for h in holdings:
            qty = float(h.get("quantity") or 0)
            realised = float(h.get("realised_quantity") or 0)
            t1_now = qty - realised
            if t1_now < 0.0001:
                continue
            sym = h["tradingsymbol"]
            old_qty = 0.0
            old_realised = 0.0
            if prev_equity and sym in prev_equity:
                old_qty = float(prev_equity[sym].get("quantity") or 0)
                old_realised = float(prev_equity[sym].get("realised_quantity") or 0)
            old_t1 = old_qty - old_realised
            t1_increase = t1_now - old_t1
            if t1_increase < 0.0001:
                if old_qty >= qty:
                    log.info("Skipping T1 for %s: no increase in remaining (prev_t1=%f, now_t1=%f)", sym, old_t1, t1_now)
                continue
            if old_qty >= qty:
                log.info("Skipping T1 BUY for %s: prev qty >= current (unsettled from before)", sym)
                continue
            log.info("Detected T1 increase for %s: +%f (remaining)", sym, t1_increase)
            detected.append({
                "id": f"HOLDING_T1_{sym}_{tx_date}",
                "date": tx_date,
                "type": "BUY",
                "instrument_type": "EQUITY",
                "tradingsymbol": sym,
                "exchange": h.get("exchange", "NSE"),
                "quantity": t1_increase,
                "price": float(h.get("average_price") or 0),
                "amount": t1_increase * float(h.get("average_price") or 0),
            })

    # 2. Equity diff: only when prev snapshot had this symbol (old_qty > 0) to avoid phantoms for long-held stocks
    if is_recent and prev_date:
        pdate_str = prev_date.isoformat()
        log.info("Performing archive-aware diff with %s (tx date = %s)", pdate_str, tx_date)

        for h in holdings:
            sym = h["tradingsymbol"]
            new_qty = float(h.get("quantity") or 0)
            old_h = prev_equity.get(sym)
            old_qty = float(old_h.get("quantity") or 0) if old_h else 0

            if abs(new_qty - old_qty) < 0.0001:
                continue
            diff = new_qty - old_qty
            if diff > 0:
                # Only record BUY if symbol existed in previous snapshot (old_qty > 0).
                # If old_qty == 0, symbol was missing in prev → likely phantom for long-held stocks.
                if old_qty < 0.0001:
                    log.info("Skipping DIF BUY for %s: symbol was not in previous snapshot (phantom risk)", sym)
                    continue
                t1_amount = next((x["quantity"] for x in detected if x["tradingsymbol"] == sym), 0)
                real_diff = diff - t1_amount
                if real_diff > 0.0001:
                    log.info("Detecting BUY for %s: diff=%f (date=%s)", sym, real_diff, tx_date)
                    detected.append({
                        "id": f"DIF_BUY_{sym}_{tx_date}",
                        "date": tx_date,
                        "type": "BUY",
                        "instrument_type": "EQUITY",
                        "tradingsymbol": sym,
                        "exchange": h.get("exchange", "NSE"),
                        "quantity": real_diff,
                        "price": float(h.get("average_price") or 0),
                        "amount": real_diff * float(h.get("average_price") or 0),
                    })
            else:
                sell_qty = abs(diff)
                log.info("Detecting SELL for %s: diff=%f (date=%s)", sym, sell_qty, tx_date)
                detected.append({
                    "id": f"DIF_SELL_{sym}_{tx_date}",
                    "date": tx_date,
                    "type": "SELL",
                    "instrument_type": "EQUITY",
                    "tradingsymbol": sym,
                    "exchange": h.get("exchange", "NSE"),
                    "quantity": sell_qty,
                    "price": float(h.get("last_price") or 0),
                    "amount": sell_qty * float(h.get("last_price") or 0),
                })

        # MF: do not infer transactions from holdings diff. MF holdings snapshots can be
        # missing or delayed for a fund (e.g. API quirk), so comparing prev vs current
        # often produces false SELLs when the fund simply wasn't in the latest response.
        # Ledger uses only explicit Kite MF orders from /mf/orders.

    return detected


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
    mf_portfolio_cost: float = 0,
    price_changes: dict | None = None,
):
    """
    Save daily aggregates to portfolio_daily and one row per stock/MF to holdings_equity_daily and holdings_mf_daily.
    Archiving logic: If a record for the same ISO week and same calendar month already exists in portfolio_daily,
    move it to archive before saving. We never archive across month boundary (e.g. Mar 1 refresh does not archive Feb 28).
    """
    mf_holdings = mf_holdings or []
    price_changes = price_changes or {}
    conn = get_db()
    date_str = d.isoformat()
    now = datetime.now().isoformat()

    # Only archive records in the same ISO week AND same month (never move February when saving March)
    year, week, weekday = d.isocalendar()
    all_dates = conn.execute("SELECT date FROM portfolio_daily").fetchall()
    to_archive = []
    for row in all_dates:
        try:
            rd = date.fromisoformat(row["date"])
            ry, rw, rwd = rd.isocalendar()
            if ry == year and rw == week and rd.month == d.month and row["date"] != date_str:
                to_archive.append(row["date"])
        except ValueError:
            continue

    # Move same-week same-month records to archive
    for adate in to_archive:
        # Move aggregates
        conn.execute("""
            INSERT INTO portfolio_archive
            (date, portfolio_value, portfolio_cost, buy_amount, sell_amount, 
             month_buy, month_sell, month_per_stock_json, num_holdings, 
             mf_portfolio_value, mf_portfolio_cost, price_changes_json, created_at, archived_at)
            SELECT date, portfolio_value, portfolio_cost, buy_amount, sell_amount, 
                   month_buy, month_sell, month_per_stock_json, num_holdings, 
                   mf_portfolio_value, mf_portfolio_cost, price_changes_json, created_at, ? as archived_at 
            FROM portfolio_daily WHERE date = ?
        """, (now, adate))
        # Move equity holdings
        conn.execute("""
            INSERT INTO holdings_equity_archive
            (date, tradingsymbol, exchange, data_json, created_at, archived_at)
            SELECT date, tradingsymbol, exchange, data_json, created_at, ? as archived_at 
            FROM holdings_equity_daily WHERE date = ?
        """, (now, adate))
        # Move MF holdings
        conn.execute("""
            INSERT INTO holdings_mf_archive
            (date, tradingsymbol, folio, data_json, created_at, archived_at)
            SELECT date, tradingsymbol, folio, data_json, created_at, ? as archived_at 
            FROM holdings_mf_daily WHERE date = ?
        """, (now, adate))
        # Delete from daily tables
        conn.execute("DELETE FROM portfolio_daily WHERE date = ?", (adate,))
        conn.execute("DELETE FROM holdings_equity_daily WHERE date = ?", (adate,))
        conn.execute("DELETE FROM holdings_mf_daily WHERE date = ?", (adate,))
        log.info("Archived existing record for %s (same week and month as %s)", adate, date_str)

    # Aggregates only (no holdings JSON in portfolio_daily)
    conn.execute("""
        INSERT OR REPLACE INTO portfolio_daily
        (date, portfolio_value, portfolio_cost, buy_amount, sell_amount,
         month_buy, month_sell, month_per_stock_json, num_holdings,
         mf_portfolio_value, mf_portfolio_cost, price_changes_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        date_str,
        portfolio_value,
        portfolio_cost,
        buy_amount,
        sell_amount,
        month_buy,
        month_sell,
        json.dumps(month_per_stock),
        len(holdings),
        mf_portfolio_value,
        mf_portfolio_cost,
        json.dumps(price_changes),
        now,
    ))
    # One row per equity holding for this date
    conn.execute("DELETE FROM holdings_equity_daily WHERE date = ?", (date_str,))
    for h in holdings:
        sym = h.get("tradingsymbol") or ""
        ex = (h.get("exchange") or "NSE").strip()
        if not sym:
            continue
        conn.execute(
            "INSERT INTO holdings_equity_daily (date, tradingsymbol, exchange, data_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (date_str, sym, ex, json.dumps(h), now),
        )
    # One row per MF holding for this date
    conn.execute("DELETE FROM holdings_mf_daily WHERE date = ?", (date_str,))
    for h in mf_holdings:
        sym = h.get("tradingsymbol") or ""
        folio = str(h.get("folio") or "")
        conn.execute(
            "INSERT INTO holdings_mf_daily (date, tradingsymbol, folio, data_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (date_str, sym, folio, json.dumps(h), now),
        )
    conn.commit()
    # Detect implicit transactions from holdings diff after we commit the daily rows
    # so that get_previous_date can find the data if needed.
    detected = _detect_transactions_from_holdings(conn, d, holdings, mf_holdings)
    conn.close()
    
    if detected:
        # Cleanup: Remove any previously detected transactions for the date(s) we are writing.
        # Inferred tx are attributed to the older (prev) date, so delete by those dates.
        conn2 = get_db()
        dates_to_clean = {tx["date"] for tx in detected}
        for dclean in dates_to_clean:
            conn2.execute("DELETE FROM transactions WHERE date = ? AND (id LIKE 'HOLDING_T1_%' OR id LIKE 'DIF_%')", (dclean,))
        conn2.commit()
        conn2.close()
        save_transactions(detected)
    
    log.info("Saved portfolio for %s: %d equity, %d MF rows", date_str, len(holdings), len(mf_holdings))
    ts = _turso_sync()
    if ts:
        try:
            ts.save_portfolio_day_turso(
                d, portfolio_value, portfolio_cost, buy_amount, sell_amount,
                month_buy, month_sell, holdings, month_per_stock,
                mf_holdings=mf_holdings, mf_portfolio_value=mf_portfolio_value,
                mf_portfolio_cost=mf_portfolio_cost, price_changes=price_changes,
            )
        except Exception as e:
            log.warning("Turso save_portfolio_day failed: %s", e)


def update_cached_mf_only(d: date, mf_holdings: list, mf_portfolio_value: float) -> None:
    """Update only MF data for an existing day: holdings_mf_daily rows and portfolio_daily.mf_portfolio_value."""
    conn = get_db()
    date_str = d.isoformat()
    now = datetime.now().isoformat()
    try:
        conn.execute("UPDATE portfolio_daily SET mf_portfolio_value = ? WHERE date = ?", (mf_portfolio_value, date_str))
        conn.execute("DELETE FROM holdings_mf_daily WHERE date = ?", (date_str,))
        for h in mf_holdings:
            sym = h.get("tradingsymbol") or ""
            folio = str(h.get("folio") or "")
            conn.execute(
                "INSERT INTO holdings_mf_daily (date, tradingsymbol, folio, data_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (date_str, sym, folio, json.dumps(h), now),
            )
        conn.commit()
        log.info("Cache updated with MF data for %s: %d funds, Rs %.2f", date_str, len(mf_holdings), mf_portfolio_value)
    except Exception as e:
        log.warning("Failed to update cache with MF: %s", e)
    finally:
        conn.close()
    ts = _turso_sync()
    if ts:
        try:
            ts.update_cached_mf_only_turso(d, mf_holdings, mf_portfolio_value)
        except Exception as e:
            log.warning("Turso update_cached_mf_only failed: %s", e)


def restore_archive_to_daily() -> tuple[int, list[str]]:
    """
    Copy all dates that exist in portfolio_archive but not in portfolio_daily
    into the daily tables (portfolio_daily, holdings_equity_daily, holdings_mf_daily).
    Archive rows are left in place. Returns (count of dates restored, list of dates).
    """
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT date FROM portfolio_archive WHERE date NOT IN (SELECT date FROM portfolio_daily)"
        ).fetchall()
        dates_to_restore = [r["date"] for r in rows]
        for date_str in dates_to_restore:
            conn.execute(
                """
                INSERT INTO portfolio_daily
                (date, portfolio_value, portfolio_cost, buy_amount, sell_amount,
                 month_buy, month_sell, month_per_stock_json, num_holdings,
                 mf_portfolio_value, mf_portfolio_cost, price_changes_json, created_at)
                SELECT date, portfolio_value, portfolio_cost, buy_amount, sell_amount,
                       month_buy, month_sell, month_per_stock_json, num_holdings,
                       mf_portfolio_value, mf_portfolio_cost, price_changes_json, created_at
                FROM portfolio_archive WHERE date = ?
                """,
                (date_str,),
            )
            conn.execute(
                """
                INSERT INTO holdings_equity_daily (date, tradingsymbol, exchange, data_json, created_at)
                SELECT date, tradingsymbol, exchange, data_json, created_at
                FROM holdings_equity_archive WHERE date = ?
                """,
                (date_str,),
            )
            conn.execute(
                """
                INSERT INTO holdings_mf_daily (date, tradingsymbol, folio, data_json, created_at)
                SELECT date, tradingsymbol, folio, data_json, created_at
                FROM holdings_mf_archive WHERE date = ?
                """,
                (date_str,),
            )
        conn.commit()
        return len(dates_to_restore), dates_to_restore
    finally:
        conn.close()


def clear_portfolio_cache() -> int:
    """Delete all rows from portfolio_daily and holdings tables. Returns number of portfolio_daily rows deleted."""
    conn = get_db()
    cur = conn.execute("SELECT COUNT(*) FROM portfolio_daily")
    n = cur.fetchone()[0]
    conn.execute("DELETE FROM portfolio_daily")
    conn.execute("DELETE FROM holdings_equity_daily")
    conn.execute("DELETE FROM holdings_mf_daily")
    conn.commit()
    conn.close()
    log.info("Cleared portfolio cache: %d day(s), all equity and MF rows", n)
    ts = _turso_sync()
    if ts:
        try:
            ts.clear_portfolio_cache_turso()
        except Exception as e:
            log.warning("Turso clear_portfolio_cache failed: %s", e)
    return n


def delete_inferred_mf_transactions() -> int:
    """Remove inferred MF transactions (DIF_MF_*) from ledger. Returns number deleted."""
    conn = get_db()
    cur = conn.execute("SELECT COUNT(*) FROM transactions WHERE id LIKE 'DIF_MF_%'")
    n = cur.fetchone()[0]
    conn.execute("DELETE FROM transactions WHERE id LIKE 'DIF_MF_%'")
    conn.commit()
    conn.close()
    if n:
        log.info("Removed %d inferred MF transaction(s) (DIF_MF_*) from ledger", n)
        ts = _turso_sync()
        if ts:
            try:
                ts.delete_inferred_mf_transactions_turso()
            except Exception as e:
                log.warning("Turso delete_inferred_mf_transactions failed: %s", e)
    return n


def _row_to_dict(r, include_mf_cost=True):
    """Convert sqlite3.Row to dict; Row has no .get() so APIs need plain dicts."""
    d = dict(r)
    if include_mf_cost and "mf_portfolio_cost" not in d:
        d["mf_portfolio_cost"] = 0
    return d


def get_cache_status_rows(limit: int = 31) -> list:
    """Return list of dicts for cache-status/summary-by-date APIs (from portfolio_daily)."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT date, portfolio_value, portfolio_cost, buy_amount, sell_amount, month_buy, month_sell, num_holdings, mf_portfolio_value, mf_portfolio_cost, created_at FROM portfolio_daily ORDER BY date DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = conn.execute(
            "SELECT date, portfolio_value, portfolio_cost, buy_amount, sell_amount, month_buy, month_sell, num_holdings, mf_portfolio_value, created_at FROM portfolio_daily ORDER BY date DESC LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def get_dates_list(limit: int = 365) -> list:
    """Return list of dates (YYYY-MM-DD) we have data for, newest first. Includes archive so archived same-week snapshots (e.g. Feb) still appear."""
    conn = get_db()
    rows = conn.execute(
        """
        SELECT date FROM (
            SELECT date FROM portfolio_daily
            UNION
            SELECT date FROM portfolio_archive
        ) ORDER BY date DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()
    return [r["date"] for r in rows]


def get_monthly_summary_rows() -> list:
    """Return the latest snapshot for each month from portfolio_daily AND portfolio_archive."""
    conn = get_db()
    # Combine both daily and archive tables, then pick the latest date per month
    query = """
        WITH all_data AS (
            SELECT date, portfolio_value, portfolio_cost, buy_amount, sell_amount,
                   month_buy, month_sell, month_per_stock_json, num_holdings,
                   mf_portfolio_value, mf_portfolio_cost, price_changes_json, created_at,
                   'daily' as source
            FROM portfolio_daily
            UNION ALL
            SELECT date, portfolio_value, portfolio_cost, buy_amount, sell_amount,
                   month_buy, month_sell, month_per_stock_json, num_holdings,
                   mf_portfolio_value, mf_portfolio_cost, price_changes_json, created_at,
                   'archive' as source
            FROM portfolio_archive
        )
        SELECT * FROM all_data
        WHERE date IN (SELECT MAX(date) FROM all_data GROUP BY SUBSTR(date, 1, 7))
        ORDER BY date DESC
    """
    rows = conn.execute(query).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def get_weekly_summary_rows_for_month(year_month: str) -> list:
    """Return all snapshots for a given YYYY-MM from portfolio_daily and portfolio_archive (prefer daily when same date)."""
    conn = get_db()
    prefix = year_month + "%"
    rows_daily = conn.execute(
        "SELECT * FROM portfolio_daily WHERE date LIKE ? ORDER BY date DESC",
        (prefix,),
    ).fetchall()
    rows_archive = conn.execute(
        "SELECT date, portfolio_value, portfolio_cost, buy_amount, sell_amount, month_buy, month_sell, "
        "month_per_stock_json, num_holdings, mf_portfolio_value, mf_portfolio_cost, price_changes_json, created_at "
        "FROM portfolio_archive WHERE date LIKE ? ORDER BY date DESC",
        (prefix,),
    ).fetchall()
    conn.close()
    seen = {r["date"] for r in rows_daily}
    merged = list(rows_daily)
    for r in rows_archive:
        if r["date"] not in seen:
            merged.append(r)
            seen.add(r["date"])
    merged.sort(key=lambda x: x["date"], reverse=True)
    return [_row_to_dict(r) for r in merged]


def get_holdings_for_date(d: date) -> dict | None:
    """Return equity and MF holdings for a given date (from normalized tables). Includes cost for Invested summary."""
    cached = get_cached_day(d)
    if cached is None:
        return None
    return {
        "date": cached["date"],
        "portfolio_value": float(cached.get("portfolio_value", 0)),
        "portfolio_cost": float(cached.get("portfolio_cost", 0)),
        "mf_portfolio_value": float(cached.get("mf_portfolio_value", 0)),
        "mf_portfolio_cost": float(cached.get("mf_portfolio_cost", 0)),
        "equity": json.loads(cached.get("holdings_json") or "[]"),
        "mf": json.loads(cached.get("mf_holdings_json") or "[]"),
    }


def get_all_portfolio_days() -> list:
    """Return all portfolio_daily rows with MF holdings for NAV history building."""
    conn = get_db()
    rows = conn.execute(
        "SELECT date, mf_portfolio_value FROM portfolio_daily ORDER BY date ASC"
    ).fetchall()
    
    result = []
    for row in rows:
        d = row["date"]
        # Get MF holdings for this date
        mf_rows = conn.execute(
            "SELECT tradingsymbol, folio, data_json FROM holdings_mf_daily WHERE date = ?",
            (d,)
        ).fetchall()
        
        mf_holdings = []
        for mr in mf_rows:
            try:
                mf_holdings.append(json.loads(mr["data_json"]))
            except (json.JSONDecodeError, TypeError):
                pass
        
        result.append({
            "date": d,
            "mf_portfolio_value": row["mf_portfolio_value"],
            "mf_holdings_json": json.dumps(mf_holdings),
        })
    
    conn.close()
    return result


def save_transactions(transactions: list) -> None:
    """Save a list of transactions to the database. Each trans is a dict."""
    conn = get_db()
    now = datetime.now().isoformat()
    for t in transactions:
        # id is broker_id or hash
        tid = str(t.get("id") or t.get("order_id") or "")
        if not tid: continue
        conn.execute(
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
                float(t.get("quantity") or 0),
                float(t.get("price") or 0),
                float(t.get("amount") or 0),
                json.dumps(t),
                now
            )
        )
    conn.commit()
    conn.close()
    ts = _turso_sync()
    if ts:
        try:
            ts.save_transactions_turso(transactions)
        except Exception as e:
            log.warning("Turso save_transactions failed: %s", e)


def get_transactions(instrument_type: str = "EQUITY", limit: int = 500) -> list:
    """Return recent transactions of a given type."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM transactions WHERE instrument_type = ? ORDER BY date DESC, created_at DESC LIMIT ?",
        (instrument_type.upper(), limit)
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def get_monthly_transaction_totals() -> dict:
    """Return monthly buy/sell totals grouped by instrument type and month."""
    conn = get_db()
    query = """
        SELECT 
            SUBSTR(date, 1, 7) as month,
            instrument_type,
            type,
            SUM(amount) as total_amount,
            COUNT(*) as count
        FROM transactions
        WHERE type IN ('BUY', 'SELL')
        GROUP BY SUBSTR(date, 1, 7), instrument_type, type
        ORDER BY month DESC, instrument_type
    """
    rows = conn.execute(query).fetchall()
    conn.close()
    
    result = {}
    total_eq_count = 0
    total_mf_count = 0
    for r in rows:
        month = r["month"]
        inst_type = r["instrument_type"]
        tx_type = r["type"]
        amount = r["total_amount"] or 0
        count = r["count"] or 0
        
        if month not in result:
            result[month] = {
                "EQUITY": {"buy": 0, "sell": 0, "buy_count": 0, "sell_count": 0},
                "MF": {"buy": 0, "sell": 0, "buy_count": 0, "sell_count": 0}
            }
        
        if inst_type in result[month]:
            if tx_type == "BUY":
                result[month][inst_type]["buy"] = amount
                result[month][inst_type]["buy_count"] = count
            elif tx_type == "SELL":
                result[month][inst_type]["sell"] = amount
                result[month][inst_type]["sell_count"] = count
        
        if inst_type == "EQUITY":
            total_eq_count += count
        elif inst_type == "MF":
            total_mf_count += count
    
    return {"months": result, "total_eq_count": total_eq_count, "total_mf_count": total_mf_count}


def get_monthly_transaction_details() -> dict:
    """Return monthly transactions with per-symbol breakdown for reconciliation."""
    conn = get_db()
    query = """
        SELECT 
            SUBSTR(date, 1, 7) as month,
            instrument_type,
            type,
            tradingsymbol,
            date,
            quantity,
            price,
            amount,
            data_json
        FROM transactions
        WHERE type IN ('BUY', 'SELL')
        ORDER BY month DESC, instrument_type, tradingsymbol, date
    """
    rows = conn.execute(query).fetchall()
    conn.close()
    
    result = {}
    for r in rows:
        month = r["month"]
        inst_type = r["instrument_type"]
        tx_type = r["type"]
        symbol = r["tradingsymbol"]
        
        if month not in result:
            result[month] = {"EQUITY": {}, "MF": {}}
        
        if inst_type not in result[month]:
            result[month][inst_type] = {}
        
        if symbol not in result[month][inst_type]:
            result[month][inst_type][symbol] = {"buy": [], "sell": [], "buy_total": 0, "sell_total": 0}
        
        tx_data = {
            "date": r["date"],
            "quantity": r["quantity"],
            "price": r["price"],
            "amount": r["amount"]
        }
        
        # Try to get fund name from data_json for MF
        if inst_type == "MF":
            try:
                import json
                data = json.loads(r["data_json"] or "{}")
                tx_data["fund_name"] = data.get("fund", "")
            except:
                tx_data["fund_name"] = ""
        
        if tx_type == "BUY":
            result[month][inst_type][symbol]["buy"].append(tx_data)
            result[month][inst_type][symbol]["buy_total"] += r["amount"] or 0
        else:
            result[month][inst_type][symbol]["sell"].append(tx_data)
            result[month][inst_type][symbol]["sell_total"] += r["amount"] or 0
    
    return result


def get_cached_nav(scheme_code: str, start_date: date) -> list:
    """Get cached NAV data for a scheme from the specified start date."""
    conn = get_db()
    rows = conn.execute("""
        SELECT nav_date, nav FROM mf_nav_cache
        WHERE scheme_code = ? AND nav_date >= ?
        ORDER BY nav_date ASC
    """, (scheme_code, start_date.isoformat())).fetchall()
    conn.close()
    return [{"date": r["nav_date"], "nav": r["nav"]} for r in rows]


def get_nav_cache_info(scheme_code: str) -> dict:
    """Get cache info for a scheme - latest cached date and count."""
    conn = get_db()
    row = conn.execute("""
        SELECT MAX(nav_date) as max_date, MIN(nav_date) as min_date, 
               COUNT(*) as count, MAX(cached_at) as cached_at
        FROM mf_nav_cache WHERE scheme_code = ?
    """, (scheme_code,)).fetchone()
    conn.close()
    if row and row["count"] > 0:
        return {
            "max_date": row["max_date"],
            "min_date": row["min_date"],
            "count": row["count"],
            "cached_at": row["cached_at"]
        }
    return None


def save_nav_cache(scheme_code: str, nav_data: list):
    """Save NAV data to cache."""
    if not nav_data:
        return
    conn = get_db()
    now = datetime.now().isoformat()
    for entry in nav_data:
        try:
            conn.execute("""
                INSERT OR REPLACE INTO mf_nav_cache (scheme_code, nav_date, nav, cached_at)
                VALUES (?, ?, ?, ?)
            """, (scheme_code, entry["date"], entry["nav"], now))
        except Exception:
            pass
    conn.commit()
    conn.close()

