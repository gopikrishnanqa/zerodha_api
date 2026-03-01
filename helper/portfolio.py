"""
Portfolio payload building for API responses.
"""
import json
from datetime import date, datetime

from helper import activity as activity_helper


def build_portfolio_payload(
    data: dict | None,
    from_cache: bool,
    cached: dict | None,
    cached_first_of_prev_month: dict | None = None,
) -> dict:
    """Build response payload with optional MF and comparison."""
    if from_cache and cached:
        holdings = json.loads(cached["holdings_json"] or "[]")
        month_per_stock = json.loads(cached["month_per_stock_json"] or "{}")
        mf_holdings = json.loads(cached.get("mf_holdings_json") or "[]")
        mf_value = float(cached.get("mf_portfolio_value") or 0)
        price_changes = json.loads(cached["price_changes_json"] or "{}") if cached.get("price_changes_json") else {}
        today = date.today()
        try:
            cached_date = datetime.strptime(cached["date"], "%Y-%m-%d").date()
        except (ValueError, TypeError, KeyError):
            cached_date = today
        same_month = cached_date.month == today.month and cached_date.year == today.year
        if same_month:
            month_buy = float(cached["month_buy"])
            month_sell = float(cached["month_sell"])
            previous_month_buy = None
            previous_month_sell = None
            previous_month_name = None
        else:
            # Current month (e.g. Feb) totals from activity file; previous month from cache
            month_buy, month_sell = activity_helper.month_totals_for(today)
            previous_month_buy = float(cached["month_buy"])
            previous_month_sell = float(cached["month_sell"])
            previous_month_name = cached_date.strftime("%B %Y")
        out = {
            "fromCache": True,
            "date": cached["date"],
            "created_at": cached.get("created_at"),
            "portfolio_value": float(cached["portfolio_value"]),
            "portfolio_cost": float(cached["portfolio_cost"]),
            "buy_amount": float(cached["buy_amount"]),
            "sell_amount": float(cached["sell_amount"]),
            "month_buy": month_buy,
            "month_sell": month_sell,
            "holdings": holdings,
            "month_per_stock": month_per_stock,
            "mf_holdings": mf_holdings,
            "mf_portfolio_value": mf_value,
            "price_changes": price_changes,
            "month_name": today.strftime("%B %Y"),
        }
        if previous_month_name is not None:
            out["previous_month_buy"] = previous_month_buy
            out["previous_month_sell"] = previous_month_sell
            out["previous_month_name"] = previous_month_name
        if cached_first_of_prev_month:
            pv = float(cached_first_of_prev_month.get("portfolio_value") or 0)
            pc = float(cached_first_of_prev_month.get("portfolio_cost") or 0)
            mfv = float(cached_first_of_prev_month.get("mf_portfolio_value") or 0)
            mfc = float(cached_first_of_prev_month.get("mf_portfolio_cost") or 0)
            out["last_month_value_date"] = cached_first_of_prev_month.get("date")
            out["last_month_portfolio_value"] = pv + mfv
            out["last_month_portfolio_cost"] = pc + mfc
        return out
    if not data:
        raise ValueError("data required when not from cache")
    return {
        "fromCache": False,
        "date": data["date"],
        "created_at": datetime.now().isoformat(),
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
        "price_changes": data.get("price_changes", {}),
        "month_name": data["month_name"],
    }
