"""Shared limit-up ladder payload builder for API routes."""

from datetime import datetime
from typing import Optional

from db import get_data_db


def build_daily_ladder_payload(date: Optional[str] = None) -> dict:
    """连板天梯 JSON，供 /api/ladder/daily 与 Hermes 内部工具共用。"""
    data_collections = get_data_db()
    ladder_col = data_collections["limit_up_ladder"]
    daily_col = data_collections["daily_limit_up"]

    requested_date = date
    if not requested_date:
        requested_date = datetime.now().strftime("%Y%m%d")

    ladder = ladder_col.find_one({"date": requested_date})
    is_fallback = False
    actual_date = requested_date

    if not ladder:
        latest_ladder = ladder_col.find_one(
            {"date": {"$lte": requested_date}},
            sort=[("date", -1)],
        )
        if latest_ladder:
            ladder = latest_ladder
            actual_date = latest_ladder["date"]
            is_fallback = True
        else:
            return {
                "success": False,
                "error": f"{requested_date} 暂无数据",
                "suggestion": "请确保数据已采集，或选择其他日期",
            }

    stocks = list(
        daily_col.find(
            {
                "date": actual_date,
                "$or": [
                    {"status": {"$ne": "broken"}},
                    {"status": {"$exists": False}},
                ],
            }
        ).sort("streak", -1)
    )

    tiers = {"4+": [], "3": [], "2": [], "1": []}

    for stock in stocks:
        streak = stock["streak"]
        tier_key = "4+" if streak >= 4 else str(streak)
        tiers[tier_key].append(
            {
                "symbol": stock["symbol"],
                "name": stock["name"],
                "streak": streak,
                "sector": stock.get("primary_sector", ""),
                "sectors": stock.get("sectors", []),
                "seal_amount": stock.get("seal_amount", 0),
                "limit_up_time": stock.get("limit_up_time", ""),
                "open_count": stock.get("open_count", 0),
                "turnover_ratio": stock.get("turnover_ratio", 0),
                "analysis": stock.get("analysis", None),
            }
        )

    broken_stocks_cursor = daily_col.find(
        {"date": actual_date, "status": "broken"}
    ).sort("updated_at", -1)

    broken_limit_ups = []
    for stock in broken_stocks_cursor:
        broken_limit_ups.append(
            {
                "symbol": stock["symbol"],
                "name": stock["name"],
                "streak": stock["streak"],
                "sector": stock.get("primary_sector", ""),
                "sectors": stock.get("sectors", []),
                "limit_up_time": stock.get("limit_up_time", ""),
                "last_seal_time": stock.get("last_seal_time", ""),
                "broken_time": stock.get("updated_at", "").isoformat()
                if isinstance(stock.get("updated_at"), datetime)
                else "",
                "limit_reason": stock.get("limit_reason", ""),
                "analysis": stock.get("analysis", None),
            }
        )

    sector_aggregation = {}
    for tier_key, tier_data in ladder.get("tiers", {}).items():
        for sector, sector_data in tier_data.get("sectors", {}).items():
            if sector not in sector_aggregation:
                sector_aggregation[sector] = {
                    "1": 0,
                    "2": 0,
                    "3": 0,
                    "4+": 0,
                    "total": 0,
                }
            sector_aggregation[sector][tier_key] = sector_data.get("count", 0)
            sector_aggregation[sector]["total"] += sector_data.get("count", 0)

    display_date = f"{actual_date[:4]}-{actual_date[4:6]}-{actual_date[6:]}"
    tier_counts = {tier_key: len(tier_list) for tier_key, tier_list in tiers.items()}
    total_from_tiers = sum(tier_counts.values())
    indicators = dict(ladder.get("indicators", {}) or {})
    indicators["total_limit_up"] = total_from_tiers

    return {
        "success": True,
        "date": actual_date,
        "requested_date": requested_date,
        "display_date": display_date,
        "is_fallback": is_fallback,
        "fallback_note": f"显示 {display_date} 数据（最近交易日）"
        if is_fallback
        else None,
        "tier_counts": tier_counts,
        "tiers": tiers,
        "broken_limit_ups": broken_limit_ups,
        "sector_aggregation": sector_aggregation,
        "indicators": indicators,
        "sector_ranking": ladder.get("sector_ranking", []),
    }
