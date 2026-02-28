
import sqlite3
import json

def verify():
    conn = sqlite3.connect('data/portfolio.db')
    conn.row_factory = sqlite3.Row
    print("--- Transactions for 2026-02-25 ---")
    rows = conn.execute("SELECT id, type, tradingsymbol, quantity, amount FROM transactions WHERE date = '2026-02-25'").fetchall()
    for r in rows:
        print(dict(r))
    conn.close()

if __name__ == "__main__":
    verify()
