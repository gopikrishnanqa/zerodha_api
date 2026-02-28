
import sqlite3
import json
from pathlib import Path

DB_PATH = Path('data/portfolio.db')

def debug():
    if not DB_PATH.exists():
        print(f"ERROR: {DB_PATH} does not exist!")
        return

    print(f"Opening DB at: {DB_PATH.resolve()}")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    
    # 1. List all tables
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    print(f"Tables in DB: {tables}")
    
    if 'transactions' not in tables:
        print("ERROR: 'transactions' table is MISSING!")
    else:
        count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        print(f"Transactions count: {count}")
        if count > 0:
            print("Latest 5 transactions:")
            for r in conn.execute("SELECT * FROM transactions ORDER BY date DESC LIMIT 5").fetchall():
                print(dict(r))

    if 'checklist' not in tables:
        print("ERROR: 'checklist' table is MISSING!")
    
    # 2. Check holdings for comparison
    print("\n--- Check holdings for IDFCFIRSTB ---")
    rows = conn.execute("SELECT date, data_json FROM holdings_equity_daily WHERE tradingsymbol = 'IDFCFIRSTB' ORDER BY date DESC").fetchall()
    for r in rows:
        data = json.loads(r['data_json'])
        print(f"Date: {r['date']}, Qty: {data.get('quantity')}, T1: {data.get('t1_quantity')}")

    # 3. Check what get_previous_date would find for today
    today = '2026-02-25'
    prev = conn.execute("SELECT date FROM portfolio_daily WHERE date < ? ORDER BY date DESC LIMIT 1", (today,)).fetchone()
    print(f"\nPrevious date for {today}: {prev['date'] if prev else 'None'}")

    conn.close()

if __name__ == "__main__":
    debug()
