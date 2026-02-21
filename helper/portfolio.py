"""
Portfolio payload building for API responses.
"""
import json
from datetime import date


def build_portfolio_payload(data: dict | None, from_cache: bool, cached: dict | None) -> dict:
    """Build response payload with optional MF and comparison."""
    if from_cache and cached:
        holdings = json.loads(cached["holdings_json"] or "[]")
        month_per_stock = json.loads(cached["month_per_stock_json"] or "{}")
        mf_holdings = json.loads(cached.get("mf_holdings_json") or "[]")
        mf_value = float(cached.get("mf_portfolio_value") or 0)
        return {
            "fromCache": True,
            "date": cached["date"],
            "portfolio_value": float(cached["portfolio_value"]),
            "portfolio_cost": float(cached["portfolio_cost"]),
            "buy_amount": float(cached["buy_amount"]),
            "sell_amount": float(cached["sell_amount"]),
            "month_buy": float(cached["month_buy"]),
            "month_sell": float(cached["month_sell"]),
            "holdings": holdings,
            "month_per_stock": month_per_stock,
            "mf_holdings": mf_holdings,
            "mf_portfolio_value": mf_value,
            "month_name": date.today().strftime("%B %Y"),
        }
    if not data:
        raise ValueError("data required when not from cache")
    return {
        "fromCache": False,
        "date": data["date"],
        "portfolio_value": data["portfolio_value"],
        "portfolio_cost": data["portfolio_cost"],
        "buy_amount": data["buy_amount"],
        "sell_amount": data["sell_amount"],
        "month_buy": data["month_buy"],
        "month_sell": data["month_sell"],
        "holdings": data["holdings"],
        "month_per_stock": data["month_per_stock"],
        "mf_holdings": data.get("mf_holdings", []),
        "mf_portfolio_value": data.get("mf_portfolio_value", 0),
        "month_name": data["month_name"],
    }
