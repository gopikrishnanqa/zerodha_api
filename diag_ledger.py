
import sqlite3
import json
from pathlib import Path

DB_PATH = Path('data/portfolio.db')

def debug():
    if not DB_PATH.exists():
        print(f"ERROR: {DB_PATH} does not exist!")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    
    # 1. Check Previous Date Logic
    today = '2026-02-25'
    query = """
        SELECT MAX(date) as last_date FROM (
            SELECT date FROM portfolio_daily WHERE date < ?
            UNION ALL
            SELECT date FROM portfolio_archive WHERE date < ?
        )
    """
    row = conn.execute(query, (today, today)).fetchone()
    prev_date = row['last_date']
    print(f"Detected Previous Date for {today}: {prev_date}")

    # 2. Check holdings on that previous date
    if prev_date:
        print(f"\n--- Holdings on {prev_date} ---")
        # Check daily
        rows = conn.execute("SELECT tradingsymbol, data_json FROM holdings_equity_daily WHERE date = ?", (prev_date,)).fetchall()
        if not rows:
            # Check archive
            rows = conn.execute("SELECT tradingsymbol, data_json FROM holdings_equity_archive WHERE date = ?", (prev_date,)).fetchall()
            print("(Found in ARCHIVE)")
        else:
            print("(Found in DAILY)")
        
        print(f"Total symbols on {prev_date}: {len(rows)}")
        for r in rows[:5]: # Print first 5
            data = json.loads(r['data_json'])
            print(f"  {r['tradingsymbol']}: Qty {data.get('quantity')}")

    # 3. Check transactions on 2026-02-25
    print(f"\n--- Transactions on {today} ---")
    rows = conn.execute("SELECT id, type, tradingsymbol, quantity, amount FROM transactions WHERE date = ?", (today,)).fetchall()
    print(f"Total transactions today: {len(rows)}")
    for r in rows:
        print(f"  ID: {r['id']}, {r['type']} {r['tradingsymbol']}, Qty: {r['quantity']}, Amt: {r['amount']}")

    # 4. Check IDFCFIRSTB history
    print("\n--- IDFCFIRSTB Holding History ---")
    history_query = """
        SELECT date, data_json, 'Daily' as tab FROM holdings_equity_daily WHERE tradingsymbol = 'IDFCFIRSTB'
        UNION ALL
        SELECT date, data_json, 'Archive' as tab FROM holdings_equity_archive WHERE tradingsymbol = 'IDFCFIRSTB'
        ORDER BY date DESC
    """
    for r in conn.execute(history_query).fetchall():
        data = json.loads(r['data_json'])
        print(f"  {r['date']} ({r['tab']}): Qty {data.get('quantity')}, T1 {data.get('t1_quantity')}, Avg {data.get('average_price')}")

    conn.close()

if __name__ == "__main__":
    debug()
