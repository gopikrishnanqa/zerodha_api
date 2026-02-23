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
    """Create tables: portfolio_daily (aggregates per day), holdings_equity_daily, holdings_mf_daily (one row per holding per day)."""
    conn = get_db()
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

    # Add mf_portfolio_cost if missing (for category-wise invested amount)
    try:
        conn.execute("ALTER TABLE portfolio_daily ADD COLUMN mf_portfolio_cost REAL DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    # Migrate old schema: if portfolio_daily has holdings_json, backfill holdings tables for each date
    try:
        rows = conn.execute("SELECT date, holdings_json, mf_holdings_json FROM portfolio_daily").fetchall()
        for row in rows:
            hj = row["holdings_json"] if "holdings_json" in row.keys() else None
            mj = row["mf_holdings_json"] if "mf_holdings_json" in row.keys() else None
            if row["date"] and (hj or mj):
                _migrate_holdings_from_json(conn, row["date"], hj or "[]", mj or "[]")
        if rows:
            conn.commit()
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
            SELECT *, ? as archived_at FROM portfolio_daily WHERE date = ?
        """, (now, adate))
        # Move equity holdings
        conn.execute("""
            INSERT INTO holdings_equity_archive
            SELECT *, ? as archived_at FROM holdings_equity_daily WHERE date = ?
        """, (now, adate))
        # Move MF holdings
        conn.execute("""
            INSERT INTO holdings_mf_archive
            SELECT *, ? as archived_at FROM holdings_mf_daily WHERE date = ?
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
    conn.close()
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
