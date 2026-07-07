"""Unit tests for live trade latency rollup service."""

from __future__ import annotations

from datetime import datetime, timezone

from services.trade_latency_metrics import build_trade_latency_rollup


class _Cursor:
    def __init__(self, rows):
        self.rows = list(rows)

    def sort(self, key, direction=1):
        reverse = direction == -1
        self.rows.sort(key=lambda row: row.get(key) or 0, reverse=reverse)
        return self

    def limit(self, n):
        self.rows = self.rows[:n]
        return self

    def __iter__(self):
        return iter(self.rows)


class _Collection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])
        self.last_query = None

    def find(self, query=None, projection=None):
        self.last_query = query or {}
        rows = [doc for doc in self.docs if self._matches(doc, self.last_query)]
        if projection:
            include = {key for key, value in projection.items() if value}
            rows = [{key: value for key, value in row.items() if key in include} for row in rows]
        return _Cursor(rows)

    def _matches(self, doc, query):
        for key, value in (query or {}).items():
            if isinstance(value, dict):
                if "$in" in value and doc.get(key) not in value["$in"]:
                    return False
                if "$gte" in value and (doc.get(key) is None or doc.get(key) < value["$gte"]):
                    return False
                if "$lte" in value and (doc.get(key) is None or doc.get(key) > value["$lte"]):
                    return False
                continue
            if doc.get(key) != value:
                return False
        return True


def test_trade_latency_rollup_computes_stage_percentiles():
    finance = {
        "trade_signals": _Collection(
            [
                {
                    "user_id": "user-1",
                    "mode": "live",
                    "order_id": "order-1",
                    "plan_id": "plan-1",
                    "strategy_template_id": "stx",
                    "params_hash": "hx",
                    "securities_account_id": "acct-1",
                    "symbol": "600000.SH",
                    "action": "buy",
                    "status": "filled",
                    "timestamp": 100.0,
                    "first_seen_at": 101.0,
                    "first_seen_by": "quantTrader:acct-1",
                    "submitted_at": 103.0,
                    "executed_at": 110.0,
                },
                {
                    "user_id": "user-1",
                    "mode": "live",
                    "order_id": "order-2",
                    "plan_id": "plan-1",
                    "strategy_template_id": "stx",
                    "params_hash": "hx",
                    "securities_account_id": "acct-1",
                    "symbol": "000001.SZ",
                    "action": "sell",
                    "status": "pending",
                    "timestamp": 200.0,
                },
            ]
        ),
        "trade_executions": _Collection(
            [
                {
                    "user_id": "user-1",
                    "order_id": "order-1",
                    "plan_id": "plan-1",
                    "status": "filled",
                    "timestamp": 110.0,
                    "ingested_at": 112.0,
                    "ingest_source": "api",
                }
            ]
        ),
    }
    trading = {
        "portfolio_live_snapshots": _Collection(
            [
                {
                    "user_id": "user-1",
                    "strategy_template_id": "stx",
                    "params_hash": "hx",
                    "updated_at": datetime(1970, 1, 1, 0, 2, 0),
                    "build_ms": 8.5,
                    "watermark": {"execution_count": 1},
                    "last_rebuild_reason": "execution",
                }
            ]
        )
    }

    result = build_trade_latency_rollup(
        finance,
        trading,
        user_id="user-1",
        plan_id="plan-1",
        since=50.0,
        until=250.0,
    )

    assert result["order_count"] == 2
    assert result["execution_count"] == 1
    assert result["snapshot_count"] == 1
    assert result["stages"]["publish_to_first_seen_ms"]["p50"] == 1000.0
    assert result["stages"]["first_seen_to_submitted_ms"]["p95"] == 2000.0
    assert result["stages"]["submitted_to_executed_ms"]["p99"] == 7000.0
    assert result["stages"]["executed_to_ingested_ms"]["avg"] == 2000.0
    assert result["stages"]["ingested_to_snapshot_ms"]["p50"] == 8000.0
    assert result["stages"]["publish_to_first_seen_ms"]["missing_count"] == 1
    order = next(row for row in result["orders"] if row["order_id"] == "order-1")
    assert order["durations_ms"]["publish_to_ingested_ms"] == 12000.0
    assert order["snapshot_rebuild_reason"] == "execution"


def test_trade_latency_rollup_can_omit_order_rows():
    finance = {
        "trade_signals": _Collection(
            [
                {
                    "user_id": "user-1",
                    "mode": "live",
                    "order_id": "order-1",
                    "timestamp": 100.0,
                }
            ]
        ),
        "trade_executions": _Collection(),
    }
    trading = {"portfolio_live_snapshots": _Collection()}

    result = build_trade_latency_rollup(
        finance,
        trading,
        user_id="user-1",
        order_id="order-1",
        include_orders=False,
    )

    assert result["order_count"] == 1
    assert result["orders"] == []
    assert finance["trade_signals"].last_query["order_id"] == "order-1"


def test_trade_latency_rollup_prefers_created_at_for_publish_time():
    created_at = datetime(2026, 7, 7, 5, 50, 39, tzinfo=timezone.utc)
    first_seen_at = created_at.timestamp() + 2
    finance = {
        "trade_signals": _Collection(
            [
                {
                    "user_id": "user-1",
                    "mode": "live",
                    "order_id": "order-1",
                    "timestamp": created_at.timestamp() - 8 * 3600,
                    "created_at": created_at.replace(tzinfo=None),
                    "first_seen_at": first_seen_at,
                }
            ]
        ),
        "trade_executions": _Collection(),
    }
    trading = {"portfolio_live_snapshots": _Collection()}

    result = build_trade_latency_rollup(finance, trading, user_id="user-1")

    order = result["orders"][0]
    assert order["timestamps"]["publish_at"] == created_at.timestamp()
    assert order["durations_ms"]["publish_to_first_seen_ms"] == 2000.0
