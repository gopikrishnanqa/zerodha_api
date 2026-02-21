# Zerodha Portfolio Holdings & Price Comparison

A web app that connects to Zerodha Kite Connect, fetches your equity holdings, shows price comparison (% change from 7d, 30d, 6m, 1y) in the UI, and exports holdings to CSV with date (only when you click Export).

## Features

- **Connect with Zerodha** – OAuth login using Kite Connect
- **Holdings table** – Symbol, exchange, quantity, avg price, LTP, value, P&L, P&L %, Company (Market), Invested (Month), Sold (Month), 7d/30d/6m/1y
- **Sortable columns** – Click any column header to sort (Symbol, Exchange, Qty, Value, P&L, P&L %, Invested, Sold)
- **Total value** – Total portfolio value row and summary cards
- **Monthly activity** – Today's bought/sold and current month invested & sold totals (persisted daily)
- **Per-stock monthly** – Invested (Month) and Sold (Month) per holding
- **Company (Market)** – Company name from market data for comparison
- **Price comparison (UI only)** – % up/down from 7 days, 30 days, 6 months, 1 year
- **Export CSV** – Saves holdings (with Value, Invested, Sold) to dated CSV when you click Export

## Setup

### 1. Get Zerodha API credentials

1. Go to [Kite Connect](https://kite.trade/)
2. Create an app and get your **API Key** and **API Secret**
3. Set the **Redirect URL** in your app settings to:  
   `http://127.0.0.1:5000/api/callback` (for local use)

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and add:

```
KITE_API_KEY=your_api_key
KITE_API_SECRET=your_api_secret
REDIRECT_URL=http://127.0.0.1:5000/api/callback
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Run the app

```bash
python app.py
```

Open [http://127.0.0.1:5000](http://127.0.0.1:5000)

## Usage

1. Click **Connect with Zerodha** and log in
2. Your holdings will load with P&L
3. Price comparison columns (7d, 30d, 6m, 1y) show % change vs market data
4. Click **Export CSV** to download `zerodha_holdings_YYYY-MM-DD.csv` only when you need it

## CSV export

The CSV contains: Date, Trading Symbol, Exchange, Quantity, Average Price, Last Price, P&L, P&L %, Product, ISIN.

Export happens only when you click the Export button; data is not saved automatically.

## SQLite cache

- Portfolio data is stored in `portfolio.db` (one row per day: date, portfolio value, cost, buy/sell amounts, holdings snapshot).
- **On open:** If today's row exists, data is loaded from the DB (no Kite API call). If not, data is fetched and saved for today.
- **Refresh data:** Use the "Refresh data" button to force-fetch from Zerodha and overwrite today's record.
- **Comparison:** When a previous stored date exists, the UI shows the change in invested amount, portfolio value, buy amount, and sell amount vs that date.

## Importing historical holdings from Excel

You can import older equity and mutual fund holdings from a Zerodha holdings Excel file (e.g. tax report export) into the SQLite cache so that date appears in the app’s “By Date” view.

**Command:**

```bash
python scripts/import_holdings_excel.py "path\to\holdings-YYYY-MM-DD.xlsx" YYYY-MM-DD
```

**Example (Windows):**

```bash
python scripts/import_holdings_excel.py "H:\Gopi\NSE\holdings-DG0997-Aug-2025.xlsx" 2025-08-31
```

- **First argument:** Full path to the `.xlsx` file.
- **Second argument:** Date for that snapshot in `YYYY-MM-DD` format.

The script reads **Equity** and **Mutual Funds** sheets separately. That sheet should contain both equity and mutual fund rows with a row that has a “Symbol” header; the script detects the header row automatically. Rows are treated as mutual funds if a Segment/Type column is present and set to MF (or similar); otherwise they are treated as equity. It reads Symbol, Quantity Available, Average Price, and Previous Closing Price, plus Invested Value and Present Value from the summary, Data is written to `portfolio_daily`, `holdings_equity_daily`, and `holdings_mf_daily` for that date.

## Notes

- Price comparison and Company use market data (yfinance) for NSE/BSE equities; MCX/other segments may show – if not available
- Monthly invested/sold totals are built from daily order snapshots: run the app daily so `monthly_activity.json` accumulates data (Kite API returns only today's orders)
- Access token expires at 6 AM next day (Zerodha policy); reconnect if needed
- Use only on your own machine; never expose API secret or access token
