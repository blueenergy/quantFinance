"""公募抄作业快照对外字段。"""
from __future__ import annotations

from typing import Any, Dict, Optional

YI = 1e8

ACTION_LABELS = {
    "new": "新买",
    "increased": "加仓",
    "decreased": "减仓",
    "exited": "清仓",
    "unchanged": "不变",
}


def yuan_to_yi(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return round(float(value) / YI, 2)
    except (TypeError, ValueError):
        return None


def public_product(row: Dict[str, Any], *, include_holdings: bool = False) -> Dict[str, Any]:
    holdings = row.get("holdings") or []
    out = {
        "fund_key": row.get("fund_key"),
        "ts_code": row.get("ts_code"),
        "name": row.get("name"),
        "management": row.get("management"),
        "period_prev": row.get("period_prev"),
        "period_curr": row.get("period_curr"),
        "compare_mode": row.get("compare_mode"),
        "rank": row.get("rank"),
        "source": row.get("source"),
        "aum_prev_yi": yuan_to_yi(row.get("aum_prev")),
        "aum_curr_yi": yuan_to_yi(row.get("aum_curr")),
        "n_new": int(row.get("n_new") or 0),
        "n_increased": int(row.get("n_increased") or 0),
        "n_decreased": int(row.get("n_decreased") or 0),
        "n_exited": int(row.get("n_exited") or 0),
        "n_unchanged": int(row.get("n_unchanged") or 0),
    }
    if include_holdings:
        out["holdings"] = [
            {
                **item,
                "action_label": item.get("action_label") or ACTION_LABELS.get(str(item.get("action") or ""), item.get("action")),
                "mkv_prev_yi": yuan_to_yi(item.get("mkv_prev")),
                "mkv_curr_yi": yuan_to_yi(item.get("mkv_curr")),
            }
            for item in holdings
        ]
    return out


def public_stock(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "symbol": row.get("symbol"),
        "name": row.get("name"),
        "period_prev": row.get("period_prev"),
        "period_curr": row.get("period_curr"),
        "compare_mode": row.get("compare_mode"),
        "n_operated": int(row.get("n_operated") or 0),
        "n_new": int(row.get("n_new") or 0),
        "n_increased": int(row.get("n_increased") or 0),
        "n_decreased": int(row.get("n_decreased") or 0),
        "n_exited": int(row.get("n_exited") or 0),
        "cards": row.get("cards") or [],
    }


def public_meta(row: Dict[str, Any]) -> Dict[str, Any]:
    compare_mode = row.get("compare_mode")
    quality = dict(row.get("quality") or {})
    quality_status = str(
        row.get("quality_status") or quality.get("status") or (
            "provisional" if compare_mode == "top10_vs_top10" else "official"
        )
    )
    comparison_scope = row.get("comparison_scope") or quality.get("comparison_scope") or (
        "top10" if quality_status == "provisional" else "full"
    )
    if "status" not in quality:
        quality["status"] = quality_status
    if "comparison_scope" not in quality:
        quality["comparison_scope"] = comparison_scope
    return {
        "period_prev": row.get("period_prev"),
        "period_curr": row.get("period_curr"),
        "compare_mode": compare_mode,
        "disclosure_prev": row.get("disclosure_prev"),
        "disclosure_curr": row.get("disclosure_curr"),
        "quality_status": quality_status,
        "comparison_scope": comparison_scope,
        "aum_basis": row.get("aum_basis") or (
            "top10_mkv" if quality_status == "provisional" else "full_mkv"
        ),
        "quality": quality,
        "lag_note": row.get("lag_note"),
        "n_watchlist": row.get("n_watchlist"),
        "n_consensus": row.get("n_consensus"),
    }
