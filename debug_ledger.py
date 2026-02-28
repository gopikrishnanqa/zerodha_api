
import sqlite3
import json
from datetime import date

def debug_db():
    conn = sqlite3.connect('data/portfolio.db')
    conn.row_factory = sqlite3.Row
    
    print("--- portfolio_daily (latest 5) ---")
    rows = conn.execute("SELECT date, portfolio_value, num_holdings FROM portfolio_daily ORDER BY date DESC LIMIT 5").fetchall()
    for r in rows:
        print(f"Date: {r['date']}, Value: {r['portfolio_value']}, Holdings: {r['num_holdings']}")
        
    print(f"\n--- Total transactions found: {conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0]} ---")
    
    print("\n--- transactions (latest 10) ---")
    rows = conn.execute("SELECT id, date, type, tradingsymbol, quantity, instrument_type FROM transactions ORDER BY date DESC, created_at DESC LIMIT 10").fetchall()
    for r in rows:
        print(f"ID: {r['id']}, Date: {r['date']}, Type: {r['type']}, Symbol: {r['tradingsymbol']}, Qty: {r['quantity']} ({r['instrument_type']})")

    # Check IDFCFIRSTB specifically if it exists in holdings
    print("\n--- Checking IDFCFIRSTB in holdings_equity_daily ---")
    rows = conn.execute("SELECT date, tradingsymbol, data_json FROM holdings_equity_daily WHERE tradingsymbol = 'IDFCFIRSTB' ORDER BY date DESC").fetchall()
    for r in rows:
        data = json.loads(r['data_json'])
        print(f"Date: {r['date']}, Qty: {data.get('quantity')}, T1: {data.get('t1_quantity')}")

    conn.close()

if __name__ == "__main__":
    debug_db()
