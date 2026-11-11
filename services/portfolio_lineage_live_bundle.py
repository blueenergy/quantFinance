"""Shared lineage live signal/execution bundle fetch for routers and snapshots."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from services.portfolio_lineage_state import EXECUTION_VENUE_LIVE, resolve_execution_venue
from services.trade_ledger import group_executions_by_order

SIGNAL_PROJECTION = {
    "_id": 0,
    "order_id": 1,
    "plan_id": 1,
    "user_id": 1,
    "mode": 1,
    "symbol": 1,
    "name": 1,
    "stock_name": 1,
    "action": 1,
    "size": 1,
    "status": 1,
    "securities_account_id": 1,
    "created_at": 1,
    "updated_at": 1,
    "timestamp": 1,
    "signal_time": 1,
}

EXECUTION_PROJECTION = {
    "_id": 0,
    "order_id": 1,
    "plan_id": 1,
    "user_id": 1,
    "securities_account_id": 1,
    "symbol": 1,
    "name": 1,
    "stock_name": 1,
    "side": 1,
    "action": 1,
    "status": 1,
    "execution_status": 1,
    "filled_size": 1,
    "filled_qty": 1,
    "quantity": 1,
    "size": 1,
    "filled_price": 1,
    "traded_price": 1,
    "avg_price": 1,
    "price": 1,
    "target_price": 1,
    "total_fee": 1,
    "commission": 1,
    "fee": 1,
    "timestamp": 1,
    "created_at": 1,
    "updated_at": 1,
    "book_attribution": 1,
}


def lineage_plan_ids_for_plan(
    trading_collections: Dict[str, Any],
    plan: Dict[str, Any],
    user_id: str,
) -> List[str]:
    params_hash = plan.get("params_hash")
    if not params_hash:
        return [str(plan.get("plan_id"))] if plan.get("plan_id") else []
    rows = trading_collections["portfolio_trade_plans"].find(
        {
            "strategy_template_id": plan.get("strategy_template_id"),
            "user_id": user_id,
            "params_hash": params_hash,
        },
        {"_id": 0, "plan_id": 1},
    )
    plan_ids = {str(row.get("plan_id")) for row in rows if row.get("plan_id")}
    if plan.get("plan_id"):
        plan_ids.add(str(plan.get("plan_id")))
    return sorted(plan_ids)


def fetch_lineage_plan_meta(
    trading_collections: Dict[str, Any],
    user_id: str,
    lineage_ids: List[str],
) -> Dict[str, Dict[str, Any]]:
    if not lineage_ids:
        return {}
    plan_rows = list(
        trading_collections["portfolio_trade_plans"].find(
            {"user_id": user_id, "plan_id": {"$in": lineage_ids}},
            {"_id": 0, "plan_id": 1, "base_date": 1, "params_snapshot": 1, "initial_capital": 1, "summary": 1},
        )
    )
    return {str(row.get("plan_id")): dict(row) for row in plan_rows if row.get("plan_id")}


def _account_id_from_filled_signals(
    signals: List[Dict[str, Any]],
    executions: List[Dict[str, Any]],
) -> str:
    for row in reversed(executions):
        account_id = str(row.get("securities_account_id") or "")
        if account_id:
            return account_id
    for row in reversed(signals):
        account_id = str(row.get("securities_account_id") or "")
        if account_id:
            return account_id
    return ""


def resolve_lineage_account_and_venue(
    trading_collections: Dict[str, Any],
    *,
    user_id: str,
    strategy_template_id: str,
    params_hash: str,
    signals: List[Dict[str, Any]],
    executions: List[Dict[str, Any]],
) -> Tuple[str, str]:
    controls = trading_collections.get("portfolio_lineage_controls")
    lineage_control: Dict[str, Any] = {}
    if controls is not None:
        lineage_control = (
            controls.find_one(
                {
                    "user_id": user_id,
                    "strategy_template_id": strategy_template_id,
                    "params_hash": params_hash,
                },
                {"_id": 0, "securities_account_id": 1, "execution_venue": 1},
            )
            or {}
        )
    account_id = str(lineage_control.get("securities_account_id") or "")
    if not account_id:
        account_id = _account_id_from_filled_signals(signals, executions)
    venue = resolve_execution_venue(lineage_control, has_live_signals=bool(signals))
    if signals and venue != EXECUTION_VENUE_LIVE:
        venue = EXECUTION_VENUE_LIVE
    return account_id, venue


def _book_attribution_entry(
    execution: Dict[str, Any], params_hash: str
) -> Optional[Dict[str, Any]]:
    """The portion of this execution owned by the requested params_hash."""

    for entry in execution.get("book_attribution") or []:
        if isinstance(entry, dict) and str(entry.get("params_hash") or "") == params_hash:
            return entry
    return None


def _synthetic_attribution_execution(
    execution: Dict[str, Any],
    entry: Dict[str, Any],
    params_hash: str,
) -> Optional[Dict[str, Any]]:
    """A lineage-local copy of a fill that was issued by another plan."""

    order_id = str(execution.get("order_id") or "")
    symbol = str(execution.get("symbol") or "")
    try:
        size = int(entry.get("filled_size") or entry.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    if size <= 0 or not symbol:
        return None
    synthetic_order_id = (
        f"{order_id}#book:{params_hash}"
        if order_id
        else f"attr:{params_hash}:{symbol}:{execution.get('timestamp')}"
    )
    row = dict(execution)
    row.update(
        {
            "order_id": synthetic_order_id,
            "original_order_id": order_id or None,
            "plan_id": str(entry.get("lineage_plan_id") or execution.get("plan_id") or ""),
            "book_attribution": [dict(entry)],
            "execution_source": "book_attribution",
            "filled_size": size,
            "quantity": size,
            "filled_qty": size,
        }
    )
    if row.get("filled_price") is None and row.get("avg_price") is not None:
        row["filled_price"] = row["avg_price"]
    return row


def _merge_book_attributions(
    executions: List[Dict[str, Any]],
    attributed_executions: List[Dict[str, Any]],
    params_hash: str,
) -> List[Dict[str, Any]]:
    """Apply execution-level ``book_attribution`` to this lineage's bundle.

    The account-level ledger keeps reading the raw execution (full filled_size
    and its single ``plan_id``), so this never double-counts. Only the lineage
    replay sees the attributed portion.
    """

    if not attributed_executions or not params_hash:
        return executions
    out = list(executions)
    by_order = {str(row.get("order_id") or ""): row for row in out if row.get("order_id")}
    seen_orders: set[str] = set()
    for execution in attributed_executions:
        status = str(execution.get("status") or "").lower()
        if status and status not in {
            "filled",
            "executed",
            "partial_filled",
            "partially_filled",
        }:
            continue
        entry = _book_attribution_entry(execution, params_hash)
        if entry is None:
            continue
        order_id = str(execution.get("order_id") or "")
        if order_id and order_id in seen_orders:
            continue
        if order_id:
            seen_orders.add(order_id)
        try:
            size = int(entry.get("filled_size") or entry.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        if order_id and order_id in by_order:
            # The lineage issued the order as well. Keep one execution row and
            # shrink it to the portion this book actually owned.
            row = by_order[order_id]
            row["filled_size"] = size
            row["quantity"] = size
            row["filled_qty"] = size
            row["book_attribution"] = [dict(entry)]
            continue
        synthetic = _synthetic_attribution_execution(execution, entry, params_hash)
        if synthetic is not None:
            out.append(synthetic)
    return out


def fetch_lineage_live_bundle(
    trading_collections: Dict[str, Any],
    finance_collections: Dict[str, Any],
    plan: Dict[str, Any],
    user_id: str,
) -> Dict[str, Any]:
    """Load live signals + executions for all plans in the same params_hash lineage."""

    params_hash = str(plan.get("params_hash") or "")
    lineage_ids = lineage_plan_ids_for_plan(trading_collections, plan, user_id)
    if not lineage_ids:
        return {
            "lineage_ids": [],
            "signals": [],
            "executions": [],
            "grouped_executions": [],
            "plan_meta": {},
            "securities_account_id": "",
            "execution_venue": "",
        }

    signals = list(
        finance_collections["trade_signals"]
        .find(
            {
                "user_id": user_id,
                "plan_id": {"$in": lineage_ids},
                "mode": "live",
            },
            SIGNAL_PROJECTION,
        )
        .sort("created_at", 1)
    )
    order_ids = [row.get("order_id") for row in signals if row.get("order_id")]
    executions: List[Dict[str, Any]] = []
    if order_ids:
        executions = list(
            finance_collections["trade_executions"]
            .find(
                {"user_id": user_id, "order_id": {"$in": order_ids}},
                EXECUTION_PROJECTION,
            )
            .sort("timestamp", 1)
        )
    attributed_executions: List[Dict[str, Any]] = []
    if params_hash:
        attributed_executions = list(
            finance_collections["trade_executions"]
            .find(
                {"user_id": user_id, "book_attribution.params_hash": params_hash},
                EXECUTION_PROJECTION,
            )
            .sort("timestamp", 1)
        )
    executions = _merge_book_attributions(executions, attributed_executions, params_hash)

    strategy_template_id = str(plan.get("strategy_template_id") or "")
    securities_account_id, execution_venue = resolve_lineage_account_and_venue(
        trading_collections,
        user_id=user_id,
        strategy_template_id=strategy_template_id,
        params_hash=params_hash,
        signals=signals,
        executions=executions,
    )
    plan_meta = fetch_lineage_plan_meta(trading_collections, user_id, lineage_ids)
    grouped_executions = group_executions_by_order(executions)

    return {
        "lineage_ids": lineage_ids,
        "signals": signals,
        "executions": executions,
        "grouped_executions": grouped_executions,
        "plan_meta": plan_meta,
        "securities_account_id": securities_account_id,
        "execution_venue": execution_venue,
    }


def fetch_account_level_executions(
    finance_collections: Dict[str, Any],
    *,
    user_id: str,
    securities_account_id: str,
) -> List[Dict[str, Any]]:
    """All live executions for a securities account (for reconcile ledger cache)."""

    if not securities_account_id:
        return []
    executions = list(
        finance_collections["trade_executions"]
        .find(
            {"user_id": user_id, "securities_account_id": securities_account_id},
            EXECUTION_PROJECTION,
        )
        .sort("timestamp", 1)
    )
    if executions:
        return executions

    signals = list(
        finance_collections["trade_signals"].find(
            {
                "user_id": user_id,
                "mode": "live",
                "securities_account_id": securities_account_id,
            },
            {"_id": 0, "order_id": 1},
        )
    )
    order_ids = [row.get("order_id") for row in signals if row.get("order_id")]
    if not order_ids:
        return []
    return list(
        finance_collections["trade_executions"]
        .find({"user_id": user_id, "order_id": {"$in": order_ids}}, EXECUTION_PROJECTION)
        .sort("timestamp", 1)
    )


def compute_execution_watermark(
    signals: List[Dict[str, Any]],
    executions: List[Dict[str, Any]],
) -> Dict[str, Any]:
    max_ts = 0.0
    last_updated_at = ""
    for row in executions:
        ts = row.get("timestamp")
        if isinstance(ts, (int, float)):
            max_ts = max(max_ts, float(ts))
        updated = str(row.get("updated_at") or "")
        if updated:
            last_updated_at = max(last_updated_at, updated)
    return {
        "signal_count": len(signals),
        "execution_count": len(executions),
        "max_execution_ts": max_ts,
        "last_execution_updated_at": last_updated_at,
    }
