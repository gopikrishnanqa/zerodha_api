"""
SQLite persistence for portfolio_daily. DB file lives in data/ folder.
"""
import json
import logging
import sqlite3
from datetime import date, datetime
from pathlib import Path

from config import DB_PATH

log = logging.getLogger(__name__)


def get_db():
    """Get SQLite connection."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create portfolio_daily table if not exists and add MF columns if missing."""
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
        pass
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


def get_cache_status_rows(limit: int = 31) -> list:
    """Return list of rows for cache-status API (date, portfolio_value, ...)."""
    conn = get_db()
    rows = conn.execute(
        "SELECT date, portfolio_value, portfolio_cost, buy_amount, sell_amount, month_buy, month_sell, num_holdings, mf_portfolio_value, created_at FROM portfolio_daily ORDER BY date DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return rows
