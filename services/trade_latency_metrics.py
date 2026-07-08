"""Read-only live trading latency rollups.

This intentionally derives metrics from Mongo timestamps first. It gives us a
baseline before deciding whether Change Stream, broker callbacks, or Prometheus
instrumentation should become the next bottleneck-specific investment.

Data-quality rules (so percentiles reflect real fills, not artifacts):

* Execution-derived timestamps (``executed_at``, ``ingested_at``) are only used
  when the latest execution doc is an actual fill. Cancel / reject docs carry an
  end-of-day timestamp that would otherwise inflate ``submitted_to_executed_ms``
  and ``publish_to_ingested_ms`` into multi-hour garbage.
* A snapshot only scores the order named in its ``last_rebuild_trigger_order_id``.
  Each lineage keeps a single upserted snapshot doc, so attributing its
  ``updated_at`` to every order in the lineage would smear one (often manual /
  end-of-day) rebuild across unrelated fills.

Known limitation: under ``mongo_direct`` ingestion the fill and the ingest share
a single timestamp, so ``executed_to_ingested_ms`` collapses to ~0. Splitting it
needs quantTrader to persist the broker fill time separately from the backend
ingest time.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

SIGNAL_PROJECTION = {
    "_id": 0,
    "order_id": 1,
    "plan_id": 1,
    "strategy_template_id": 1,
    "params_hash": 1,
    "securities_account_id": 1,
    "symbol": 1,
    "action": 1,
    "status": 1,
    "timestamp": 1,
    "created_at": 1,
    "first_seen_at": 1,
    "first_seen_by": 1,
    "submitted_at": 1,
    "executed_at": 1,
    "updated_at": 1,
    "qmt_order_id": 1,
    "broker_order_id": 1,
}

EXECUTION_PROJECTION = {
    "_id": 0,
    "order_id": 1,
    "plan_id": 1,
    "status": 1,
    "timestamp": 1,
    "ingested_at": 1,
    "ingest_source": 1,
    "updated_at": 1,
    "filled_size": 1,
    "broker_order_id": 1,
    "qmt_order_id": 1,
}

SNAPSHOT_PROJECTION = {
    "_id": 0,
    "strategy_template_id": 1,
    "params_hash": 1,
    "updated_at": 1,
    "build_ms": 1,
    "watermark": 1,
    "last_rebuild_reason": 1,
    "last_rebuild_trigger_order_id": 1,
}

FILL_STATUSES = {"filled", "partially_filled", "partial", "partial_filled"}


def _execution_is_fill(execution: Dict[str, Any]) -> bool:
    """Whether a trade_execution doc represents an actual (partial) fill.

    Cancel/reject docs carry an end-of-day ``timestamp`` that must not be
    treated as an execution time, otherwise ``submitted_to_executed_ms``
    picks up multi-hour garbage for orders that never filled.
    """

    if str(execution.get("status") or "").lower() in FILL_STATUSES:
        return True
    try:
        return int(execution.get("filled_size") or 0) > 0
    except (TypeError, ValueError):
        return False

STAGE_DEFINITIONS: Tuple[Tuple[str, str, str], ...] = (
    ("publish_to_first_seen_ms", "publish_at", "first_seen_at"),
    ("first_seen_to_submitted_ms", "first_seen_at", "submitted_at"),
    ("submitted_to_executed_ms", "submitted_at", "executed_at"),
    ("executed_to_ingested_ms", "executed_at", "ingested_at"),
    ("ingested_to_snapshot_ms", "ingested_at", "snapshot_updated_at"),
    ("publish_to_ingested_ms", "publish_at", "ingested_at"),
    ("publish_to_snapshot_ms", "publish_at", "snapshot_updated_at"),
)


def _to_epoch(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _duration_ms(start: Optional[float], end: Optional[float]) -> Optional[float]:
    if start is None or end is None:
        return None
    delta = (end - start) * 1000.0
    if delta < 0:
        return None
    return round(delta, 1)


def _percentile(sorted_values: List[float], percentile: float) -> Optional[float]:
    if not sorted_values:
        return None
    idx = max(0, min(len(sorted_values) - 1, math.ceil(percentile * len(sorted_values)) - 1))
    return round(sorted_values[idx], 1)


def _stats(values: Iterable[float], total_count: int) -> Dict[str, Any]:
    rows = sorted(float(v) for v in values if v is not None)
    if not rows:
        return {
            "count": 0,
            "missing_count": total_count,
            "min": None,
            "p50": None,
            "p95": None,
            "p99": None,
            "max": None,
            "avg": None,
        }
    return {
        "count": len(rows),
        "missing_count": max(0, total_count - len(rows)),
        "min": round(rows[0], 1),
        "p50": _percentile(rows, 0.50),
        "p95": _percentile(rows, 0.95),
        "p99": _percentile(rows, 0.99),
        "max": round(rows[-1], 1),
        "avg": round(sum(rows) / len(rows), 1),
    }


def _signal_query(
    *,
    user_id: str,
    plan_id: str = "",
    order_id: str = "",
    securities_account_id: str = "",
    since: Optional[float] = None,
    until: Optional[float] = None,
) -> Dict[str, Any]:
    query: Dict[str, Any] = {"user_id": user_id, "mode": "live"}
    if plan_id:
        query["plan_id"] = plan_id
    if order_id:
        query["order_id"] = order_id
    if securities_account_id:
        query["securities_account_id"] = securities_account_id
    time_query: Dict[str, Any] = {}
    if since is not None:
        time_query["$gte"] = float(since)
    if until is not None:
        time_query["$lte"] = float(until)
    if time_query:
        query["timestamp"] = time_query
    return query


def _latest_execution_by_order(executions: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    by_order: Dict[str, Dict[str, Any]] = {}
    for row in executions:
        order_id = str(row.get("order_id") or "")
        if not order_id:
            continue
        current = by_order.get(order_id)
        row_ts = (
            _to_epoch(row.get("ingested_at"))
            or _to_epoch(row.get("timestamp"))
            or _to_epoch(row.get("updated_at"))
            or 0.0
        )
        current_ts = 0.0
        if current:
            current_ts = (
                _to_epoch(current.get("ingested_at"))
                or _to_epoch(current.get("timestamp"))
                or _to_epoch(current.get("updated_at"))
                or 0.0
            )
        if current is None or row_ts >= current_ts:
            by_order[order_id] = row
    return by_order


def _snapshot_key(row: Dict[str, Any]) -> Tuple[str, str]:
    return (str(row.get("strategy_template_id") or ""), str(row.get("params_hash") or ""))


def _latest_snapshot_by_lineage(snapshots: Iterable[Dict[str, Any]]) -> Dict[Tuple[str, str], Dict[str, Any]]:
    by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in snapshots:
        key = _snapshot_key(row)
        if not key[0] or not key[1]:
            continue
        current = by_key.get(key)
        row_ts = _to_epoch(row.get("updated_at")) or 0.0
        current_ts = _to_epoch(current.get("updated_at")) if current else 0.0
        if current is None or row_ts >= (current_ts or 0.0):
            by_key[key] = row
    return by_key


def build_trade_latency_rollup(
    finance_collections: Dict[str, Any],
    trading_collections: Dict[str, Any],
    *,
    user_id: str,
    plan_id: str = "",
    order_id: str = "",
    securities_account_id: str = "",
    since: Optional[float] = None,
    until: Optional[float] = None,
    limit: int = 500,
    include_orders: bool = True,
) -> Dict[str, Any]:
    """Build a latency rollup from persisted signal/execution/snapshot docs."""

    bounded_limit = max(1, min(int(limit or 500), 2000))
    query = _signal_query(
        user_id=user_id,
        plan_id=plan_id,
        order_id=order_id,
        securities_account_id=securities_account_id,
        since=since,
        until=until,
    )
    cursor = finance_collections["trade_signals"].find(query, SIGNAL_PROJECTION).sort("timestamp", -1).limit(bounded_limit)
    signals = list(cursor)
    order_ids = [str(row.get("order_id") or "") for row in signals if row.get("order_id")]

    executions: List[Dict[str, Any]] = []
    if order_ids:
        executions = list(
            finance_collections["trade_executions"].find(
                {"user_id": user_id, "order_id": {"$in": order_ids}},
                EXECUTION_PROJECTION,
            )
        )
    execution_by_order = _latest_execution_by_order(executions)

    lineage_keys = {
        _snapshot_key(row)
        for row in signals
        if row.get("strategy_template_id") and row.get("params_hash")
    }
    snapshots: List[Dict[str, Any]] = []
    if lineage_keys:
        snapshots = list(
            trading_collections["portfolio_live_snapshots"].find(
                {
                    "user_id": user_id,
                    "strategy_template_id": {"$in": sorted({key[0] for key in lineage_keys})},
                    "params_hash": {"$in": sorted({key[1] for key in lineage_keys})},
                },
                SNAPSHOT_PROJECTION,
            )
        )
    snapshot_by_lineage = _latest_snapshot_by_lineage(snapshots)

    order_rows: List[Dict[str, Any]] = []
    stage_values: Dict[str, List[float]] = {name: [] for name, _start, _end in STAGE_DEFINITIONS}
    for signal in signals:
        oid = str(signal.get("order_id") or "")
        execution = execution_by_order.get(oid) or {}
        snapshot = snapshot_by_lineage.get(_snapshot_key(signal)) or {}
        # Only trust a snapshot's timing for the order that actually triggered
        # its rebuild. A lineage keeps a single upserted snapshot doc, so
        # attributing its ``updated_at`` to every order would smear one (often
        # manual / end-of-day) rebuild across unrelated fills.
        snapshot_attributed = bool(
            snapshot and str(snapshot.get("last_rebuild_trigger_order_id") or "") == oid
        )
        # Fill-gate both execution-derived timestamps: cancel / reject docs
        # carry an end-of-day timestamp + ingested_at that would otherwise
        # inflate submitted_to_executed_ms / publish_to_ingested_ms into
        # multi-hour garbage for orders that never filled.
        execution_filled = _execution_is_fill(execution)
        executed_at = _to_epoch(signal.get("executed_at"))
        if executed_at is None and execution_filled:
            executed_at = _to_epoch(execution.get("timestamp"))
        ingested_at = _to_epoch(execution.get("ingested_at")) if execution_filled else None
        timestamps = {
            "publish_at": _to_epoch(signal.get("created_at")) or _to_epoch(signal.get("timestamp")),
            "first_seen_at": _to_epoch(signal.get("first_seen_at")),
            "submitted_at": _to_epoch(signal.get("submitted_at")),
            "executed_at": executed_at,
            "ingested_at": ingested_at,
            "snapshot_updated_at": _to_epoch(snapshot.get("updated_at")) if snapshot_attributed else None,
        }
        durations: Dict[str, Optional[float]] = {}
        for name, start_key, end_key in STAGE_DEFINITIONS:
            value = _duration_ms(timestamps.get(start_key), timestamps.get(end_key))
            durations[name] = value
            if value is not None:
                stage_values[name].append(value)
        order_rows.append(
            {
                "order_id": oid,
                "plan_id": signal.get("plan_id") or execution.get("plan_id") or "",
                "strategy_template_id": signal.get("strategy_template_id") or "",
                "params_hash": signal.get("params_hash") or "",
                "securities_account_id": signal.get("securities_account_id") or "",
                "symbol": signal.get("symbol") or "",
                "action": signal.get("action") or "",
                "status": signal.get("status") or execution.get("status") or "",
                "first_seen_by": signal.get("first_seen_by") or "",
                "ingest_source": execution.get("ingest_source") or "",
                "snapshot_attributed": snapshot_attributed,
                "snapshot_build_ms": snapshot.get("build_ms") if snapshot_attributed else None,
                "snapshot_rebuild_reason": (snapshot.get("last_rebuild_reason") or "") if snapshot_attributed else "",
                "timestamps": timestamps,
                "durations_ms": durations,
            }
        )

    total_count = len(order_rows)
    stages = {name: _stats(values, total_count) for name, values in stage_values.items()}
    return {
        "filters": {
            "user_id": user_id,
            "plan_id": plan_id,
            "order_id": order_id,
            "securities_account_id": securities_account_id,
            "since": since,
            "until": until,
            "limit": bounded_limit,
        },
        "order_count": total_count,
        "execution_count": len(execution_by_order),
        "snapshot_count": len(snapshot_by_lineage),
        "stages": stages,
        "orders": order_rows if include_orders else [],
    }
