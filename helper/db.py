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

    # Archive tables for weekly snapshots (storing older fetches from same week)
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
    """Return cached data for date: aggregates from portfolio_daily + holdings from holdings_equity_daily and holdings_mf_daily."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM portfolio_daily WHERE date = ?",
        (d.isoformat(),)
    ).fetchone()
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
    # Build holdings from normalized tables (fallback to legacy holdings_json if present and new tables empty)
    equity_rows = conn.execute(
        "SELECT tradingsymbol, exchange, data_json FROM holdings_equity_daily WHERE date = ? ORDER BY tradingsymbol",
        (d.isoformat(),),
    ).fetchall()
    mf_rows = conn.execute(
        "SELECT tradingsymbol, folio, data_json FROM holdings_mf_daily WHERE date = ? ORDER BY tradingsymbol",
        (d.isoformat(),),
    ).fetchall()
    if equity_rows:
        holdings = [json.loads(r["data_json"]) for r in equity_rows]
    else:
        hj = _row_key(row, "holdings_json")
        try:
            holdings = json.loads(hj) if hj else []
        except Exception:
            holdings = []
    if mf_rows:
        mf_holdings = [json.loads(r["data_json"]) for r in mf_rows]
    else:
        mj = _row_key(row, "mf_holdings_json")
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
    """Compare current holdings with previous day to detect implicit transactions."""
    detected = []
    today_str = d.isoformat()

    # 1. Primary Detection: T1 holdings (Quantity - Realised Quantity)
    # This catches buys from today/yesterday even without history.
    for h in holdings:
        qty = float(h.get("quantity") or 0)
        realised = float(h.get("realised_quantity") or 0)
        t1 = qty - realised
        if t1 > 0.0001:
            sym = h["tradingsymbol"]
            log.info("Detected T1 hold for %s: %f", sym, t1)
            detected.append({
                "id": f"HOLDING_T1_{sym}_{today_str}",
                "date": today_str,
                "type": "BUY",
                "instrument_type": "EQUITY",
                "tradingsymbol": sym,
                "exchange": h.get("exchange", "NSE"),
                "quantity": t1,
                "price": float(h.get("average_price") or 0),
                "amount": t1 * float(h.get("average_price") or 0),
            })

    # 2. Secondary Detection: Database Diff (only if previous date is the IMMEDIATE preceding snap)
    prev_date = get_previous_date(d)
    is_recent = False
    if prev_date:
        days_diff = (d - prev_date).days
        # Only diff if we have a very fresh snapshot (1-2 days max) 
        # to avoid attributing old history changes to today.
        if days_diff <= 2: 
            is_recent = True
        else:
            log.info("Previous snapshot for %s is %d days old (%s). Skipping diff to avoid ghost trades.", today_str, days_diff, prev_date.isoformat())

    if is_recent and prev_date:
        pdate_str = prev_date.isoformat()
        log.info("Performing archive-aware diff with %s", pdate_str)
        
        # Equity
        prev_equity_rows = conn.execute(
            "SELECT tradingsymbol, data_json FROM holdings_equity_daily WHERE date = ?", (pdate_str,)
        ).fetchall()
        if not prev_equity_rows:
            prev_equity_rows = conn.execute(
                "SELECT tradingsymbol, data_json FROM holdings_equity_archive WHERE date = ?", (pdate_str,)
            ).fetchall()
        prev_equity = {r["tradingsymbol"]: json.loads(r["data_json"]) for r in prev_equity_rows}

        # MF
        prev_mf_rows = conn.execute(
            "SELECT tradingsymbol, folio, data_json FROM holdings_mf_daily WHERE date = ?", (pdate_str,)
        ).fetchall()
        if not prev_mf_rows:
            prev_mf_rows = conn.execute(
                "SELECT tradingsymbol, folio, data_json FROM holdings_mf_archive WHERE date = ?", (pdate_str,)
            ).fetchall()
        prev_mf = {(r["tradingsymbol"], r["folio"]): json.loads(r["data_json"]) for r in prev_mf_rows}

        # Check Equity Diff
        for h in holdings:
            sym = h["tradingsymbol"]
            new_qty = float(h.get("quantity") or 0)
            old_h = prev_equity.get(sym)
            old_qty = float(old_h.get("quantity") or 0) if old_h else 0
            
            if abs(new_qty - old_qty) > 0.0001:
                diff = new_qty - old_qty
                if diff > 0:
                    # We already caught T1 buys above. This diff logic catches buys that 
                    # happened between the last fetch and now, which might ALREADY BE realised.
                    # To avoid double counting, we only record the diff if it's not already T1.
                    # But wait: t1 is (qty - realised). If we bought 100 on Feb 22, and it settled 
                    # by Feb 25, then Feb 25 realised=100. If Feb 20 realised=0, then diff=100.
                    # We should subtract the T1 amount we already found to avoid double counting.
                    t1_amount = next((x["quantity"] for x in detected if x["tradingsymbol"] == sym), 0)
                    real_diff = diff - t1_amount
                    if real_diff > 0.0001:
                        log.info("Detecting BUY for %s: diff=%f", sym, real_diff)
                        detected.append({
                            "id": f"DIF_BUY_{sym}_{today_str}",
                            "date": today_str,
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
                    log.info("Detecting SELL for %s: diff=%f", sym, sell_qty)
                    detected.append({
                        "id": f"DIF_SELL_{sym}_{today_str}",
                        "date": today_str,
                        "type": "SELL",
                        "instrument_type": "EQUITY",
                        "tradingsymbol": sym,
                        "exchange": h.get("exchange", "NSE"),
                        "quantity": sell_qty,
                        "price": float(h.get("last_price") or 0),
                        "amount": sell_qty * float(h.get("last_price") or 0),
                    })

        # Check MF Diff
        for h in mf_holdings:
            key = (h["tradingsymbol"], h.get("folio", ""))
            new_qty = float(h.get("quantity") or 0)
            old_h = prev_mf.get(key)
            old_qty = float(old_h.get("quantity") or 0) if old_h else 0
            
            if abs(new_qty - old_qty) > 0.0001:
                diff = new_qty - old_qty
                if diff > 0:
                    detected.append({
                        "id": f"DIF_MF_BUY_{h['tradingsymbol']}_{today_str}",
                        "date": today_str,
                        "type": "BUY",
                        "instrument_type": "MF",
                        "tradingsymbol": h["tradingsymbol"],
                        "exchange": "MF",
                        "quantity": diff,
                        "price": float(h.get("average_price") or 0),
                        "amount": diff * float(h.get("average_price") or 0),
                    })
                else:
                    sell_qty = abs(diff)
                    detected.append({
                        "id": f"DIF_MF_SELL_{h['tradingsymbol']}_{today_str}",
                        "date": today_str,
                        "type": "SELL",
                        "instrument_type": "MF",
                        "tradingsymbol": h["tradingsymbol"],
                        "exchange": "MF",
                        "quantity": sell_qty,
                        "price": float(h.get("last_price") or 0),
                        "amount": sell_qty * float(h.get("last_price") or 0),
                    })
            
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
    Archiving logic: If a record for the same ISO week already exists in portfolio_daily, move it and its holdings
    to the archive tables before saving this new fetch as the primary record for that week.
    """
    mf_holdings = mf_holdings or []
    price_changes = price_changes or {}
    conn = get_db()
    date_str = d.isoformat()
    now = datetime.now().isoformat()

    # Identify existing record in the same ISO week
    year, week, weekday = d.isocalendar()
    # Find all records in portfolio_daily
    all_dates = conn.execute("SELECT date FROM portfolio_daily").fetchall()
    to_archive = []
    for row in all_dates:
        try:
            rd = date.fromisoformat(row["date"])
            ry, rw, rwd = rd.isocalendar()
            if ry == year and rw == week and row["date"] != date_str:
                to_archive.append(row["date"])
        except ValueError:
            continue

    # Move existing same-week records to archive
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
        log.info("Archived existing record for %s (same week as %s)", adate, date_str)

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
        # Cleanup: Remove any previously detected transactions for THIS DATE
        # This prevents duplicate/ghost entries if the user refreshes multiple times
        # before the weekly archive logic kicks in.
        conn2 = get_db()
        # id starts with HOLDING_T1_ or DIF_
        conn2.execute("DELETE FROM transactions WHERE date = ? AND (id LIKE 'HOLDING_T1_%' OR id LIKE 'DIF_%')", (date_str,))
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
    """Return list of dates (YYYY-MM-DD) we have data for, newest first."""
    conn = get_db()
    rows = conn.execute(
        "SELECT date FROM portfolio_daily ORDER BY date DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [r["date"] for r in rows]


def get_monthly_summary_rows() -> list:
    """Return the latest snapshot for each month from portfolio_daily."""
    conn = get_db()
    # Use SUBSTR(date, 1, 7) to group by YYYY-MM and pick the latest date in each group
    query = """
        SELECT * FROM portfolio_daily 
        WHERE date IN (SELECT MAX(date) FROM portfolio_daily GROUP BY SUBSTR(date, 1, 7))
        ORDER BY date DESC
    """
    rows = conn.execute(query).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def get_weekly_summary_rows_for_month(year_month: str) -> list:
    """Return all snapshots (one per week) for a given YYYY-MM from portfolio_daily."""
    conn = get_db()
    query = """
        SELECT * FROM portfolio_daily 
        WHERE date LIKE ? 
        ORDER BY date DESC
    """
    rows = conn.execute(query, (year_month + "%",)).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def get_holdings_for_date(d: date) -> dict | None:
    """Return equity and MF holdings for a given date (from normalized tables). Same shape as get_cached_day but only holdings."""
    cached = get_cached_day(d)
    if cached is None:
        return None
    return {
        "date": cached["date"],
        "portfolio_value": float(cached.get("portfolio_value", 0)),
        "mf_portfolio_value": float(cached.get("mf_portfolio_value", 0)),
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

