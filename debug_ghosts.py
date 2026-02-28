
import sqlite3
import json
from pathlib import Path
from datetime import date

DB_PATH = Path('data/portfolio.db')

def debug():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    
    print("--- Available dates in portfolio_daily ---")
    for r in conn.execute("SELECT date FROM portfolio_daily ORDER BY date DESC LIMIT 10").fetchall():
        print(r['date'])

    print("\n--- Available dates in portfolio_archive ---")
    for r in conn.execute("SELECT date FROM portfolio_archive ORDER BY date DESC LIMIT 10").fetchall():
        print(r['date'])

    today_str = '2026-02-25'
    
    # Simulate get_previous_date
    query = """
        SELECT MAX(date) as last_date FROM (
            SELECT date FROM portfolio_daily WHERE date < ?
            UNION ALL
            SELECT date FROM portfolio_archive WHERE date < ?
        )
    """
    row = conn.execute(query, (today_str, today_str)).fetchone()
    prev_date = row['last_date']
    print(f"\nPrevious date detected for {today_str}: {prev_date}")

    print("\n--- Current transactions for 2026-02-25 ---")
    rows = conn.execute("SELECT id, type, tradingsymbol, quantity, date FROM transactions WHERE date = ?", (today_str,)).fetchall()
    for r in rows:
        print(f"ID: {r['id']}, Type: {r['type']}, Symbol: {r['tradingsymbol']}, Qty: {r['quantity']}")

    conn.close()

if __name__ == "__main__":
    debug()
