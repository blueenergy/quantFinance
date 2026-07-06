"""Idempotent trade execution upsert used by quantTrader via POST /api/trader/executions."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Dict, Optional, Tuple


def upsert_trader_execution(
    execution: Dict[str, Any],
    user_id: str,
    *,
    executions_coll: Any,
    signals_coll: Any,
) -> Tuple[str, bool]:
    """Merge execution into ``trade_executions`` and sync ``trade_signals``.

    Returns ``(execution_id, inserted)`` where ``inserted`` is True on first insert.
    """
    order_id = execution.get("order_id")
    if not order_id:
        raise ValueError("order_id is required")

    execution = dict(execution)
    execution["user_id"] = user_id

    ts = execution.get("timestamp")
    if not isinstance(ts, (int, float)):
        ts = time.time()
        execution["timestamp"] = ts

    dt = datetime.fromtimestamp(float(ts))
    execution.setdefault("date", dt.strftime("%Y%m%d"))
    execution.setdefault("datetime", dt.strftime("%Y-%m-%d %H:%M:%S"))
    execution.setdefault("mode", "live")
    # Stamp updated_at on every write so snapshot freshness detection
    # (probe_stale) and the execution watermark have a reliable, sortable
    # marker. Keep it aligned with the event timestamp for monotonic ordering.
    execution["updated_at"] = dt.strftime("%Y-%m-%d %H:%M:%S")

    broker_order_id = execution.get("broker_order_id") or execution.get("qmt_order_id")
    execution_status = execution.get("status") or "filled"

    signal = signals_coll.find_one({"order_id": order_id, "user_id": user_id})
    if signal:
        for field in (
            "plan_id",
            "strategy_template_id",
            "source",
            "securities_account_id",
            "account_id",
            "broker",
        ):
            if signal.get(field) is not None:
                execution.setdefault(field, signal.get(field))

    if broker_order_id:
        execution["broker_order_id"] = broker_order_id
        execution.setdefault("qmt_order_id", broker_order_id)

    execution_key: Dict[str, Any] = {
        "user_id": user_id,
        "order_id": order_id,
        "status": execution_status,
    }
    if broker_order_id:
        execution_key["broker_order_id"] = broker_order_id
    if execution.get("filled_size") is not None:
        execution_key["filled_size"] = execution.get("filled_size")

    existing = executions_coll.find_one(execution_key)
    if existing:
        executions_coll.update_one({"_id": existing["_id"]}, {"$set": execution})
        execution_id = str(existing["_id"])
        inserted = False
    else:
        insert_result = executions_coll.insert_one(execution)
        execution_id = str(insert_result.inserted_id)
        inserted = True

    if order_id:
        signal_update: Dict[str, Any] = {
            "status": execution_status,
            "updated_at": execution["timestamp"],
        }
        if execution_status in {
            "filled",
            "executed",
            "rejected",
            "cancelled",
            "canceled",
            "partial_cancelled",
            "failed",
        }:
            signal_update["executed_at"] = execution["timestamp"]
        if execution.get("filled_size") is not None:
            signal_update["filled_qty"] = execution.get("filled_size")
        if execution.get("filled_price") is not None:
            signal_update["avg_price"] = execution.get("filled_price")
        if (
            signal
            and signal.get("status") == "cancel_requested"
            and execution_status in {"submitted", "partial_filled"}
        ):
            signal_update.pop("status", None)
            signal_update.pop("executed_at", None)
        signals_coll.update_one(
            {"order_id": order_id, "user_id": user_id},
            {"$set": signal_update},
        )

    return execution_id, inserted
