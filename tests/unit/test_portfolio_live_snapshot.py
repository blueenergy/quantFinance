"""Unit tests for materialized live lineage snapshots."""

from __future__ import annotations

from services.portfolio_lineage_live import build_lineage_live_positions, build_lineage_live_trades
from services.portfolio_live_snapshot import (
    overlay_realtime,
    probe_stale,
    rebuild_live_snapshot,
    reconcile_live_snapshots_with_broker_positions,
)


class _Collection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])
        self.writes = []

    def find(self, query=None, projection=None):
        query = query or {}

        def _match(doc):
            for key, value in query.items():
                if key == "$or":
                    if not any(_match({**doc, **branch}) for branch in value):
                        return False
                    continue
                if isinstance(value, dict) and "$in" in value:
                    if doc.get(key) not in value["$in"]:
                        return False
                elif doc.get(key) != value:
                    return False
            return True

        rows = [doc for doc in self.docs if _match(doc)]
        return _Cursor(rows)

    def find_one(self, query=None, projection=None, sort=None):
        rows = list(self.find(query, projection))
        if sort:
            key = sort[0][0]
            reverse = sort[0][1] == -1
            rows.sort(key=lambda row: str(row.get(key) or ""), reverse=reverse)
        return rows[0] if rows else None

    def count_documents(self, query=None):
        return len(list(self.find(query or {})))

    def update_one(self, query, update, upsert=False):
        self.writes.append((query, update, upsert))
        set_fields = (update or {}).get("$set", {})
        for idx, doc in enumerate(self.docs):
            if all(doc.get(k) == v for k, v in query.items()):
                self.docs[idx] = {**doc, **set_fields}
                return
        if upsert:
            self.docs.append({**query, **set_fields})


class _Cursor:
    def __init__(self, rows):
        self._rows = rows

    def sort(self, key, direction=1):
        reverse = direction == -1
        self._rows.sort(key=lambda row: row.get(key) or 0, reverse=reverse)
        return self

    def __iter__(self):
        return iter(self._rows)

    def __list__(self):
        return list(self._rows)


def _trading_collections():
    return {
        "portfolio_trade_plans": _Collection(
            [
                {
                    "plan_id": "pla",
                    "strategy_template_id": "stx",
                    "user_id": "user-1",
                    "base_date": "20250101",
                    "params_hash": "hx",
                    "params_snapshot": {"initial_capital": 1_000_000},
                }
            ]
        ),
        "portfolio_live_snapshots": _Collection(),
        "portfolio_lineage_controls": _Collection(),
    }


def _finance_collections():
    return {
        "trade_signals": _Collection(
            [
                {
                    "order_id": "oa",
                    "user_id": "user-1",
                    "mode": "live",
                    "plan_id": "pla",
                    "symbol": "000001.SZ",
                    "name": "Ping",
                    "securities_account_id": "acct-1",
                    "created_at": 1,
                }
            ]
        ),
        "trade_executions": _Collection(
            [
                {
                    "order_id": "oa",
                    "user_id": "user-1",
                    "securities_account_id": "acct-1",
                    "symbol": "000001.SZ",
                    "side": "BUY",
                    "filled_size": 100,
                    "filled_price": 10.0,
                    "timestamp": 1.0,
                    "updated_at": "2025-01-01T10:00:00",
                }
            ]
        ),
    }


def test_rebuild_live_snapshot_writes_structural_positions_and_trades():
    trading = _trading_collections()
    finance = _finance_collections()
    plan = trading["portfolio_trade_plans"].docs[0]

    doc = rebuild_live_snapshot(trading, finance, plan, "user-1", anchor_plan_id="pla")

    assert doc is not None
    assert doc["positions"][0]["symbol"] == "000001.SZ"
    assert doc["positions"][0]["shares"] == 100
    assert doc["trades"]
    assert doc["watermark"]["execution_count"] == 1
    assert trading["portfolio_live_snapshots"].docs


def test_overlay_realtime_matches_direct_position_builder():
    executions = [
        {
            "order_id": "oa",
            "symbol": "000001.SZ",
            "name": "Ping",
            "side": "BUY",
            "filled_size": 100,
            "filled_price": 10.0,
            "timestamp": 1.0,
        }
    ]
    price_map = {"000001.SZ": 12.0}
    direct = build_lineage_live_positions(executions, price_map, name_map={"000001.SZ": "Ping"})
    snapshot = {
        "positions": [
            {
                "symbol": "000001.SZ",
                "name": "Ping",
                "shares": 100,
                "avg_cost": 10.0,
                "cost_basis": 1000.0,
                "realized_pnl": 0.0,
            }
        ],
        "trades": build_lineage_live_trades(executions, price_map, name_map={"000001.SZ": "Ping"}),
        "book_equity": {
            "initial_capital": 1_000_000,
            "cash": 999_000,
            "realized_pnl": 0.0,
            "execution_count": 1,
        },
        "lineage_plan_ids": ["pla"],
    }
    overlay = overlay_realtime(snapshot, price_map)

    assert overlay["positions"][0]["last_price"] == direct["positions"][0]["last_price"]
    assert overlay["positions"][0]["market_value"] == direct["positions"][0]["market_value"]
    assert overlay["positions"][0]["unrealized_pnl"] == direct["positions"][0]["unrealized_pnl"]
    assert overlay["current_book_equity"]["market_value"] == direct["summary"]["total_market_value"]


def test_probe_stale_detects_newer_execution():
    snapshot = {
        "user_id": "user-1",
        "securities_account_id": "acct-1",
        "watermark": {
            "execution_count": 1,
            "last_execution_updated_at": "2025-01-01T10:00:00",
            "max_execution_ts": 1.0,
        },
    }
    finance = {
        "trade_executions": _Collection(
            [
                {
                    "user_id": "user-1",
                    "securities_account_id": "acct-1",
                    "updated_at": "2025-01-02T10:00:00",
                    "timestamp": 2.0,
                }
            ]
        )
    }
    assert probe_stale(finance, snapshot) is True

    finance["trade_executions"].docs[0]["updated_at"] = "2025-01-01T10:00:00"
    finance["trade_executions"].docs[0]["timestamp"] = 1.0
    assert probe_stale(finance, snapshot) is False


def test_probe_stale_detects_newer_execution_without_updated_at():
    # Regression: executions written by the trader upload historically carried
    # no updated_at. Sorting by updated_at surfaced an arbitrary old doc, so a
    # fresh fill (larger timestamp) was missed and holdings never refreshed.
    snapshot = {
        "user_id": "user-1",
        "securities_account_id": "acct-1",
        "watermark": {
            "execution_count": 1,
            "last_execution_updated_at": "",
            "max_execution_ts": 1780000000.0,
        },
    }
    finance = {
        "trade_executions": _Collection(
            [
                {"user_id": "user-1", "securities_account_id": "acct-1", "timestamp": 1770000000.0},
                {"user_id": "user-1", "securities_account_id": "acct-1", "timestamp": 1783319619.0},
            ]
        )
    }
    assert probe_stale(finance, snapshot) is True


def test_reconcile_live_snapshots_rebuilds_and_resolves_stale_snapshot():
    trading = _trading_collections()
    finance = _finance_collections()
    trading["portfolio_live_snapshots"].docs.append(
        {
            "user_id": "user-1",
            "strategy_template_id": "stx",
            "params_hash": "hx",
            "anchor_plan_id": "pla",
            "securities_account_id": "acct-1",
            "account_positions": [],
        }
    )
    finance["trader_positions"] = _Collection(
        [
            {
                "user_id": "user-1",
                "securities_account_id": "acct-1",
                "symbol": "000001.SZ",
                "qty": 100,
            }
        ]
    )
    finance["job_status"] = _Collection()

    result = reconcile_live_snapshots_with_broker_positions(trading, finance)

    assert result["checked"] == 1
    assert result["rebuilt"] == 1
    assert result["resolved"] == 1
    assert result["mismatched"] == 0
    assert finance["job_status"].docs[0]["status"] == "ok"


def test_reconcile_live_snapshots_records_residual_broker_mismatch():
    trading = _trading_collections()
    finance = _finance_collections()
    trading["portfolio_live_snapshots"].docs.append(
        {
            "user_id": "user-1",
            "strategy_template_id": "stx",
            "params_hash": "hx",
            "anchor_plan_id": "pla",
            "securities_account_id": "acct-1",
            "account_positions": [{"symbol": "000001.SZ", "shares": 100}],
        }
    )
    finance["trader_positions"] = _Collection(
        [
            {
                "user_id": "user-1",
                "securities_account_id": "acct-1",
                "symbol": "000001.SZ",
                "qty": 200,
            }
        ]
    )
    finance["job_status"] = _Collection()

    result = reconcile_live_snapshots_with_broker_positions(trading, finance)

    assert result["checked"] == 1
    assert result["rebuilt"] == 1
    assert result["resolved"] == 0
    assert result["mismatched"] == 1
    assert result["diffs"][0]["diffs"][0]["broker_shares"] == 200
    assert finance["job_status"].docs[0]["status"] == "warning"


def test_probe_stale_ignores_execution_count_when_watermark_current():
    # Count differs from the watermark (e.g. a rare deletion) but the latest
    # execution is not newer: we intentionally skip the expensive full-account
    # count_documents and treat the snapshot as fresh (EOD rebuild reconciles).
    snapshot = {
        "user_id": "user-1",
        "securities_account_id": "acct-1",
        "watermark": {
            "execution_count": 5,
            "last_execution_updated_at": "2025-01-01T10:00:00",
            "max_execution_ts": 1.0,
        },
    }

    class _NoCountCollection(_Collection):
        def count_documents(self, query=None):
            raise AssertionError("probe_stale must not run a full-account count")

    finance = {
        "trade_executions": _NoCountCollection(
            [
                {
                    "user_id": "user-1",
                    "securities_account_id": "acct-1",
                    "updated_at": "2025-01-01T10:00:00",
                    "timestamp": 1.0,
                }
            ]
        )
    }
    assert probe_stale(finance, snapshot) is False
