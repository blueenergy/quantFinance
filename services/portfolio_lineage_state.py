"""Materialized portfolio/lineage identity and execution_venue state."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

EXECUTION_VENUE_PAPER = "paper"
EXECUTION_VENUE_LIVE = "live"
VALID_EXECUTION_VENUES = (EXECUTION_VENUE_PAPER, EXECUTION_VENUE_LIVE)

LINEAGE_SUMMARY_FIELDS = (
    "execution_venue",
    "latest_plan_id",
    "latest_plan_status",
    "latest_plan_type",
    "first_base_date",
    "last_base_date",
    "plan_count",
    "strategy_name",
    "param_summary",
    "universe_index",
    "top_n",
    "rebalance_days",
    "construction_mode",
    "initial_capital",
    "securities_account_id",
)


def lineage_key(
    user_id: str,
    strategy_template_id: str,
    params_hash: str,
) -> Dict[str, str]:
    return {
        "user_id": user_id,
        "strategy_template_id": strategy_template_id,
        "params_hash": params_hash,
    }


def resolve_execution_venue(
    lineage_control: Optional[Dict[str, Any]],
    *,
    has_live_signals: bool,
) -> str:
    stored = (lineage_control or {}).get("execution_venue")
    if stored in VALID_EXECUTION_VENUES:
        return str(stored)
    return EXECUTION_VENUE_LIVE if has_live_signals else EXECUTION_VENUE_PAPER


def upsert_lineage_summary(
    controls_coll,
    *,
    user_id: str,
    strategy_template_id: str,
    params_hash: str,
    summary: Dict[str, Any],
) -> None:
    if controls_coll is None:
        return
    key = lineage_key(user_id, strategy_template_id, params_hash)
    now = datetime.utcnow()
    set_fields = {
        key_name: summary[key_name]
        for key_name in LINEAGE_SUMMARY_FIELDS
        if key_name not in {"execution_venue"}
        and key_name in summary
        and summary[key_name] is not None
    }
    if summary.get("execution_venue") in VALID_EXECUTION_VENUES:
        proposed = summary["execution_venue"]
        existing = controls_coll.find_one(key, {"_id": 0, "execution_venue": 1}) or {}
        current = existing.get("execution_venue")
        if not (current == EXECUTION_VENUE_LIVE and proposed == EXECUTION_VENUE_PAPER):
            set_fields["execution_venue"] = proposed
    controls_coll.update_one(
        key,
        {
            "$set": {**key, **set_fields, "updated_at": now},
            "$setOnInsert": {"paused": False, "excluded_symbols": []},
        },
        upsert=True,
    )


def mark_lineage_live(
    controls_coll,
    *,
    user_id: str,
    strategy_template_id: str,
    params_hash: str,
    securities_account_id: Optional[str] = None,
) -> None:
    if controls_coll is None:
        return
    key = lineage_key(user_id, strategy_template_id, params_hash)
    now = datetime.utcnow()
    payload: Dict[str, Any] = {
        **key,
        "execution_venue": EXECUTION_VENUE_LIVE,
        "updated_at": now,
    }
    if securities_account_id:
        payload["securities_account_id"] = str(securities_account_id)
    controls_coll.update_one(
        key,
        {
            "$set": payload,
            "$setOnInsert": {"paused": False, "excluded_symbols": []},
        },
        upsert=True,
    )


def build_summary_from_lineage_plans(
    all_lineage: List[Dict[str, Any]],
    *,
    strategy_template_id: str,
    params_hash: str,
    param_summary_fn,
    record_kind_fn,
    trade_plan_kind: str,
    has_live_signals: bool,
    securities_account_id: str = "",
    lineage_control: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if not all_lineage:
        return {}
    latest = max(all_lineage, key=lambda row: str(row.get("base_date") or ""))
    earliest = min(all_lineage, key=lambda row: str(row.get("base_date") or ""))
    params = dict(latest.get("params_snapshot") or {})
    execution_venue = resolve_execution_venue(
        lineage_control,
        has_live_signals=has_live_signals,
    )
    return {
        "execution_venue": execution_venue,
        "latest_plan_id": latest.get("plan_id"),
        "latest_plan_status": latest.get("status"),
        "latest_plan_type": latest.get("plan_type"),
        "first_base_date": earliest.get("base_date"),
        "last_base_date": latest.get("base_date"),
        "plan_count": sum(1 for row in all_lineage if record_kind_fn(row) == trade_plan_kind),
        "strategy_name": latest.get("strategy_name") or earliest.get("strategy_name") or "",
        "param_summary": param_summary_fn(params),
        "universe_index": params.get("universe_index"),
        "top_n": params.get("top_n"),
        "rebalance_days": params.get("rebalance_days"),
        "construction_mode": params.get("construction_mode"),
        "initial_capital": params.get("initial_capital"),
        "securities_account_id": securities_account_id or (lineage_control or {}).get("securities_account_id") or "",
    }
