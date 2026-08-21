"""Daily gold picks panel builder (read-only, Mongo-backed)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from bson import ObjectId

DEFAULT_RECIPE_ID = "daily_gold_v1_csi1000_g60c40"

RECIPES: Dict[str, Dict[str, Any]] = {
    "daily_gold_v1_csi1000_g60c40": {
        "recipe_id": "daily_gold_v1_csi1000_g60c40",
        "label": "中证1000 · 成长周期 60:40",
        "universe_index": "csi1000",
        "growth_weight": 0.6,
        "cycle_weight": 0.4,
        "entry_top_n": 10,
        "hold_top_n": 20,
        "rebalance_trading_days": 20,
        "trailing_stop_pct": 0.08,
        "loss_stop_cooldown_trading_days": 5,
    },
    "daily_gold_v1_hs300_g60c40": {
        "recipe_id": "daily_gold_v1_hs300_g60c40",
        "label": "沪深300 · 成长周期 60:40",
        "universe_index": "hs300",
        "growth_weight": 0.6,
        "cycle_weight": 0.4,
        "entry_top_n": 10,
        "hold_top_n": 20,
        "rebalance_trading_days": 20,
        "trailing_stop_pct": 0.08,
        "loss_stop_cooldown_trading_days": 5,
    },
}


def normalize_score_date(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = str(value).replace("-", "").replace("/", "")[:8]
    if len(text) != 8 or not text.isdigit():
        return None
    try:
        datetime.strptime(text, "%Y%m%d")
    except ValueError:
        return None
    return text


def resolve_recipe_id(recipe_id: Optional[str]) -> str:
    key = (recipe_id or DEFAULT_RECIPE_ID).strip()
    if key not in RECIPES:
        raise ValueError(f"Unknown recipe_id: {recipe_id}")
    return key


def _jsonify(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, dict):
        return {k: _jsonify(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonify(v) for v in value]
    return value


def _outcome_label(outcome: Optional[str], return_pct: Optional[float]) -> str:
    if outcome == "expire":
        return "调仓日掉出观察池"
    if outcome == "trailing_stop":
        if return_pct is not None and float(return_pct) > 0:
            return "回撤止盈"
        return "回撤止损"
    if outcome == "insufficient_data":
        return "数据不足"
    return str(outcome or "")


def _lot_public(lot: Dict[str, Any]) -> Dict[str, Any]:
    row = _jsonify(dict(lot))
    row["outcome_label"] = _outcome_label(row.get("outcome"), row.get("return_pct"))
    return row


def latest_snapshot_date(snapshots_col, recipe_id: str, end_date: Optional[str] = None) -> Optional[str]:
    query: Dict[str, Any] = {"recipe_id": recipe_id}
    if end_date:
        query["score_date"] = {"$lte": end_date}
    doc = snapshots_col.find_one(query, sort=[("score_date", -1)], projection={"_id": 0, "score_date": 1})
    return normalize_score_date((doc or {}).get("score_date"))


def previous_snapshot_date(snapshots_col, recipe_id: str, score_date: str) -> Optional[str]:
    doc = snapshots_col.find_one(
        {"recipe_id": recipe_id, "score_date": {"$lt": score_date}},
        sort=[("score_date", -1)],
        projection={"_id": 0, "score_date": 1},
    )
    return normalize_score_date((doc or {}).get("score_date"))


def aggregate_stats(closed_lots: List[Dict[str, Any]]) -> Dict[str, Any]:
    terminal = [lot for lot in closed_lots if lot.get("outcome") in {"trailing_stop", "expire", "insufficient_data"}]
    scored = [
        lot
        for lot in terminal
        if lot.get("outcome") != "insufficient_data" and lot.get("return_pct") is not None
    ]
    rets = [float(lot["return_pct"]) for lot in scored]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    avg_win = sum(wins) / len(wins) if wins else None
    avg_loss = sum(abs(r) for r in losses) / len(losses) if losses else None
    payoff_ratio = (avg_win / avg_loss) if avg_win is not None and avg_loss and avg_loss > 0 else None
    gross_loss = sum(abs(r) for r in losses)
    profit_factor = (sum(wins) / gross_loss) if gross_loss > 0 else None
    trailing = [lot for lot in scored if lot.get("outcome") == "trailing_stop"]
    return {
        "closed_count": len(closed_lots),
        "scored_count": len(scored),
        "insufficient_data_count": sum(1 for lot in terminal if lot.get("outcome") == "insufficient_data"),
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": (len(wins) / len(scored)) if scored else None,
        "avg_return_pct": (sum(rets) / len(rets)) if rets else None,
        "payoff_ratio": payoff_ratio,
        "profit_factor": profit_factor,
        "trailing_stop_count": len(trailing),
        "trailing_stop_win_count": sum(1 for lot in trailing if float(lot.get("return_pct") or 0) > 0),
        "trailing_stop_loss_count": sum(1 for lot in trailing if float(lot.get("return_pct") or 0) <= 0),
        "expire_count": sum(1 for lot in scored if lot.get("outcome") == "expire"),
    }


def build_daily_gold_panel(
    *,
    data_db,
    recipe_id: Optional[str] = None,
    score_date: Optional[str] = None,
    closed_limit: int = 50,
) -> Dict[str, Any]:
    rid = resolve_recipe_id(recipe_id)
    recipe = RECIPES[rid]
    snapshots_col = data_db["daily_gold_snapshots"]
    lots_col = data_db["daily_gold_lots"]
    twr_col = data_db["daily_gold_twr"]

    requested = normalize_score_date(score_date)
    if score_date and not requested:
        raise ValueError(f"Invalid score_date: {score_date}; expected YYYYMMDD or YYYY-MM-DD")
    latest_date = latest_snapshot_date(snapshots_col, rid)
    resolved_date = latest_snapshot_date(snapshots_col, rid, requested) if requested else latest_date
    snapshot = (
        snapshots_col.find_one(
            {"recipe_id": rid, "score_date": resolved_date},
            projection={"_id": 0},
        )
        if resolved_date
        else None
    )

    prev_date = previous_snapshot_date(snapshots_col, rid, resolved_date) if resolved_date else None
    prev_snapshot = (
        snapshots_col.find_one(
            {"recipe_id": rid, "score_date": prev_date},
            projection={"_id": 0},
        )
        if prev_date
        else None
    )
    prev_rank = {
        str(row.get("symbol") or ""): int(row.get("rank") or 0)
        for row in (prev_snapshot or {}).get("rows") or []
        if row.get("symbol")
    }

    top10_raw = list((snapshot or {}).get("top10") or [])
    top10 = []
    for row in top10_raw:
        item = dict(row)
        sym = str(item.get("symbol") or "")
        prev = prev_rank.get(sym)
        item["prev_rank"] = prev
        item["rank_delta"] = (prev - int(item.get("rank") or 0)) if prev else None
        top10.append(item)

    all_lots = (
        list(
            lots_col.find(
                {
                    "recipe_id": rid,
                    "entry_score_date": {"$lte": resolved_date},
                },
                {"_id": 0},
            )
        )
        if resolved_date
        else []
    )
    def terminal_date(lot: Dict[str, Any]) -> str:
        if lot.get("status") != "closed":
            return ""
        return str(lot.get("exit_date") or lot.get("exit_trigger_date") or "")

    closed_as_of = [lot for lot in all_lots if terminal_date(lot) and terminal_date(lot) <= resolved_date]
    open_lots = [
        dict(lot)
        for lot in all_lots
        if not terminal_date(lot) or terminal_date(lot) > resolved_date
    ]
    historical = bool(latest_date and resolved_date and resolved_date < latest_date)
    for lot in open_lots:
        entry_date = str(lot.get("entry_date") or "")
        trigger_date = str(lot.get("exit_trigger_date") or "")
        if not entry_date or entry_date > resolved_date:
            lot["status"] = "pending_entry"
        elif trigger_date and trigger_date <= resolved_date:
            lot["status"] = "pending_exit"
        else:
            lot["status"] = "open"
        if historical:
            # Current lot documents only retain the latest peak. Do not leak a
            # future high into a historical as-of view.
            lot["peak_high"] = None
    open_lots.sort(key=lambda lot: (str(lot.get("entry_score_date") or ""), str(lot.get("symbol") or "")), reverse=True)
    closed_as_of.sort(
        key=lambda lot: (str(lot.get("exit_date") or ""), str(lot.get("updated_at") or "")),
        reverse=True,
    )
    closed_lots = closed_as_of[: max(1, int(closed_limit))]
    stats = aggregate_stats(closed_as_of)
    strategy_twr = (
        twr_col.find_one(
            {
                "recipe_id": rid,
                "score_date": {"$lte": resolved_date},
            },
            projection={"_id": 0},
            sort=[("score_date", -1)],
        )
        if resolved_date
        else None
    )

    return {
        "success": True,
        "recipe_id": rid,
        "recipes": list(RECIPES.values()),
        "recipe": recipe,
        "score_date": resolved_date,
        "requested_score_date": requested,
        "is_historical": historical,
        "previous_score_date": prev_date,
        "snapshot": _jsonify(snapshot) if snapshot else None,
        "top10": _jsonify(top10),
        "open_lots": [_lot_public(lot) for lot in open_lots],
        "recent_closed": [_lot_public(lot) for lot in closed_lots],
        "stats": stats,
        "strategy_twr": _jsonify(strategy_twr) if strategy_twr else None,
    }
