
import sqlite3
import json
from pathlib import Path

DB_PATH = Path('data/portfolio.db')

def debug():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    
    today = '2026-02-25'
    print(f"--- Database Status for {today} ---")
    
    # 1. Check IDFCFIRSTB in transactions
    rows = conn.execute("SELECT * FROM transactions WHERE date = ? AND tradingsymbol = 'IDFCFIRSTB'", (today,)).fetchall()
    print(f"Transactions for IDFCFIRSTB on {today}:")
    for r in rows:
        print(f"  {r['type']} {r['quantity']} (ID: {r['id']}) Price: {r['price']}")

    # 2. Check holdings data stored today
    h_row = conn.execute("SELECT data_json FROM holdings_equity_daily WHERE date = ? AND tradingsymbol = 'IDFCFIRSTB'", (today,)).fetchone()
    if h_row:
        data = json.loads(h_row['data_json'])
        print(f"\nStored Holding Data for {today}:")
        print(f"  Quantity: {data.get('quantity')}")
        print(f"  Realised Qty: {data.get('realised_quantity')}")
        print(f"  T1 Qty: {data.get('t1_quantity')}")
    else:
        print(f"\nNo holding record found for IDFCFIRSTB on {today}")

    # 3. Check previous snapshot
    prev = conn.execute("SELECT date FROM portfolio_daily WHERE date < ? ORDER BY date DESC LIMIT 1", (today,)).fetchone()
    if not prev:
        prev = conn.execute("SELECT date FROM portfolio_archive WHERE date < ? ORDER BY date DESC LIMIT 1", (today,)).fetchone()
    
    if prev:
        pdate = prev['date']
        print(f"\nPrevious Snapshot Date: {pdate}")
        p_row = conn.execute("SELECT data_json FROM holdings_equity_daily WHERE date = ? AND tradingsymbol = 'IDFCFIRSTB'", (pdate,)).fetchone()
        if not p_row:
            p_row = conn.execute("SELECT data_json FROM holdings_equity_archive WHERE date = ? AND tradingsymbol = 'IDFCFIRSTB'", (pdate,)).fetchone()
        
        if p_row:
            p_data = json.loads(p_row['data_json'])
            print(f"Previous Quantity: {p_data.get('quantity')}")
        else:
            print("No previous holding record found.")
    else:
        print("\nNo previous snapshot found at all.")

    conn.close()

if __name__ == "__main__":
    debug()
