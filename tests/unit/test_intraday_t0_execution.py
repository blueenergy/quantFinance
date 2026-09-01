"""Unit tests for intraday T+0 live execution."""

from __future__ import annotations

import pytest
from bson import ObjectId

from services import intraday_t0_execution as execution
from tests.unit.test_market_insight_engine import FakeCollection


@pytest.fixture(autouse=True)
def _keep_settings_off_the_trading_db(monkeypatch):
    """Stub the lineage account lookup that ``get_user_settings`` performs.

    That lookup reaches the trading db directly, so without this every test
    with a stored ``plan_id`` waits out a MongoDB connection timeout. Returning
    an empty id keeps the stored ``securities_account_id`` in play, which is
    what these tests assert against.
    """
    monkeypatch.setattr(execution, "_resolve_account_from_plan", lambda *_a, **_k: "")


@pytest.mark.unit
def test_settings_defaults(monkeypatch):
    settings_coll = FakeCollection([])
    monkeypatch.setattr(
        execution,
        "get_db_collections",
        lambda: {
            "intraday_t0_execution_settings": settings_coll,
            "intraday_t0_executions": FakeCollection([]),
        },
    )
    monkeypatch.setattr(execution, "ensure_intraday_t0_execution_indexes", lambda: None)
    body = execution.get_user_settings("u1")
    assert body["data"]["auto_execute_enabled"] is False
    assert body["data"]["sell_qty_ratio"] == 0.5
    assert body["data"]["buy_qty_ratio"] == 0.5
    assert body["data"]["buy_first_enabled"] is False


@pytest.mark.unit
def test_build_sell_intent(monkeypatch):
    signal_id = str(ObjectId())
    signals = FakeCollection(
        [
            {
                "_id": ObjectId(signal_id),
                "user_id": "u1",
                "symbol": "300750.SZ",
                "signal_type": "SELL_WATCH",
                "signal_score": 85,
            }
        ]
    )
    settings_coll = FakeCollection(
        [
            {
                "user_id": "u1",
                "plan_id": "plan-1",
                "securities_account_id": "acct-1",
                "sell_qty_ratio": 0.5,
            }
        ]
    )
    monkeypatch.setattr(
        execution,
        "get_db_collections",
        lambda: {
            "intraday_t0_signals": signals,
            "intraday_t0_execution_settings": settings_coll,
            "intraday_t0_executions": FakeCollection([]),
            "trader_positions": FakeCollection(
                [{"user_id": "u1", "symbol": "300750.SZ", "available_qty": 400}]
            ),
        },
    )
    monkeypatch.setattr(execution, "ensure_intraday_t0_execution_indexes", lambda: None)
    monkeypatch.setattr(
        execution,
        "preview_trade_from_signal",
        lambda *_a, **_k: {
            "available_qty": 400,
            "suggested_qty": 200,
            "warnings": [],
        },
    )
    monkeypatch.setattr(execution, "_lineage_shares", lambda *_a, **_k: 500)
    monkeypatch.setattr(execution, "find_open_pair_for_symbol", lambda *_a, **_k: None)
    monkeypatch.setattr(execution, "list_open_pairs", lambda *_a, **_k: [])

    intent = execution.build_execution_intent("u1", signal_id)
    assert intent["action"] == "sell"
    assert intent["pair_style"] == "sell_first"
    assert intent["quantity"] == 200
    assert intent["target_shares"] == 300
    assert intent["plan_id"] == "plan-1"


@pytest.mark.unit
def test_build_buy_intent_rejected_when_buy_first_disabled(monkeypatch):
    signal_id = str(ObjectId())
    signals = FakeCollection(
        [
            {
                "_id": ObjectId(signal_id),
                "user_id": "u1",
                "symbol": "300750.SZ",
                "signal_type": "BUY_WATCH",
                "signal_score": 82,
            }
        ]
    )
    settings_coll = FakeCollection(
        [{"user_id": "u1", "plan_id": "plan-1", "securities_account_id": "acct-1", "buy_first_enabled": False}]
    )
    monkeypatch.setattr(
        execution,
        "get_db_collections",
        lambda: {
            "intraday_t0_signals": signals,
            "intraday_t0_execution_settings": settings_coll,
            "intraday_t0_executions": FakeCollection([]),
            "trader_positions": FakeCollection([]),
            "intraday_t0_trades": FakeCollection([]),
        },
    )
    monkeypatch.setattr(execution, "ensure_intraday_t0_execution_indexes", lambda: None)
    monkeypatch.setattr(
        execution,
        "preview_trade_from_signal",
        lambda *_a, **_k: {"available_qty": 400, "suggested_qty": 200, "warnings": []},
    )
    monkeypatch.setattr(execution, "_lineage_shares", lambda *_a, **_k: 300)
    monkeypatch.setattr(execution, "find_open_pair_for_symbol", lambda *_a, **_k: None)

    with pytest.raises(ValueError, match="buy_first disabled"):
        execution.build_execution_intent("u1", signal_id)


@pytest.mark.unit
def test_build_buy_first_open_intent(monkeypatch):
    signal_id = str(ObjectId())
    signals = FakeCollection(
        [
            {
                "_id": ObjectId(signal_id),
                "user_id": "u1",
                "symbol": "300750.SZ",
                "signal_type": "BUY_WATCH",
                "signal_score": 82,
            }
        ]
    )
    settings_coll = FakeCollection(
        [
            {
                "user_id": "u1",
                "plan_id": "plan-1",
                "securities_account_id": "acct-1",
                "buy_first_enabled": True,
                "buy_qty_ratio": 0.5,
            }
        ]
    )
    monkeypatch.setattr(
        execution,
        "get_db_collections",
        lambda: {
            "intraday_t0_signals": signals,
            "intraday_t0_execution_settings": settings_coll,
            "intraday_t0_executions": FakeCollection([]),
            "trader_positions": FakeCollection(
                [{"user_id": "u1", "symbol": "300750.SZ", "available_qty": 400}]
            ),
            "intraday_t0_trades": FakeCollection([]),
        },
    )
    monkeypatch.setattr(execution, "ensure_intraday_t0_execution_indexes", lambda: None)
    monkeypatch.setattr(
        execution,
        "preview_trade_from_signal",
        lambda *_a, **_k: {
            "available_qty": 400,
            "suggested_qty": 200,
            "warnings": [],
            "pair_style": "buy_first",
        },
    )
    monkeypatch.setattr(execution, "_lineage_shares", lambda *_a, **_k: 500)
    monkeypatch.setattr(execution, "find_open_pair_for_symbol", lambda *_a, **_k: None)
    monkeypatch.setattr(execution, "list_open_pairs", lambda *_a, **_k: [])
    monkeypatch.setattr(execution, "_buy_first_open_capacity", lambda *_a, **_k: 400)

    intent = execution.build_execution_intent("u1", signal_id)
    assert intent["quantity"] == 200
    assert intent["target_shares"] == 700


@pytest.mark.unit
def test_build_sell_first_buy_back_intent(monkeypatch):
    signal_id = str(ObjectId())
    signals = FakeCollection(
        [
            {
                "_id": ObjectId(signal_id),
                "user_id": "u1",
                "symbol": "300750.SZ",
                "signal_type": "BUY_WATCH",
                "signal_score": 82,
            }
        ]
    )
    settings_coll = FakeCollection(
        [{"user_id": "u1", "plan_id": "plan-1", "securities_account_id": "acct-1"}]
    )
    open_pair = {
        "pair_id": "pair-1",
        "symbol": "300750.SZ",
        "style": "sell_first",
        "uncovered_qty": 100,
        "status": "open",
    }
    monkeypatch.setattr(
        execution,
        "get_db_collections",
        lambda: {
            "intraday_t0_signals": signals,
            "intraday_t0_execution_settings": settings_coll,
            "intraday_t0_executions": FakeCollection([]),
            "trader_positions": FakeCollection([]),
            "intraday_t0_trades": FakeCollection([]),
        },
    )
    monkeypatch.setattr(execution, "ensure_intraday_t0_execution_indexes", lambda: None)
    monkeypatch.setattr(
        execution,
        "preview_trade_from_signal",
        lambda *_a, **_k: {"available_qty": 0, "suggested_qty": 100, "warnings": []},
    )
    monkeypatch.setattr(execution, "_lineage_shares", lambda *_a, **_k: 400)
    monkeypatch.setattr(execution, "find_open_pair_for_symbol", lambda *_a, **_k: open_pair)
    monkeypatch.setattr(execution, "list_open_pairs", lambda *_a, **_k: [open_pair])

    intent = execution.build_execution_intent("u1", signal_id)
    assert intent["action"] == "buy"
    assert intent["pair_style"] == "sell_first"
    assert intent["quantity"] == 100
    assert intent["target_shares"] == 500


@pytest.mark.unit
def test_build_buy_first_close_intent(monkeypatch):
    signal_id = str(ObjectId())
    signals = FakeCollection(
        [
            {
                "_id": ObjectId(signal_id),
                "user_id": "u1",
                "symbol": "300750.SZ",
                "signal_type": "SELL_WATCH",
                "signal_score": 85,
            }
        ]
    )
    settings_coll = FakeCollection(
        [{"user_id": "u1", "plan_id": "plan-1", "securities_account_id": "acct-1"}]
    )
    open_pair = {
        "pair_id": "pair-1",
        "symbol": "300750.SZ",
        "style": "buy_first",
        "uncovered_qty": 100,
        "status": "open",
    }
    monkeypatch.setattr(
        execution,
        "get_db_collections",
        lambda: {
            "intraday_t0_signals": signals,
            "intraday_t0_execution_settings": settings_coll,
            "intraday_t0_executions": FakeCollection([]),
            "trader_positions": FakeCollection(
                [{"user_id": "u1", "symbol": "300750.SZ", "available_qty": 300}]
            ),
            "intraday_t0_trades": FakeCollection([]),
        },
    )
    monkeypatch.setattr(execution, "ensure_intraday_t0_execution_indexes", lambda: None)
    monkeypatch.setattr(
        execution,
        "preview_trade_from_signal",
        lambda *_a, **_k: {"available_qty": 300, "suggested_qty": 100, "warnings": []},
    )
    monkeypatch.setattr(execution, "_lineage_shares", lambda *_a, **_k: 600)
    monkeypatch.setattr(execution, "find_open_pair_for_symbol", lambda *_a, **_k: open_pair)
    monkeypatch.setattr(execution, "list_open_pairs", lambda *_a, **_k: [open_pair])
    monkeypatch.setattr(execution, "_broker_available_qty", lambda *_a, **_k: 300)

    intent = execution.build_execution_intent("u1", signal_id)
    assert intent["action"] == "sell"
    assert intent["pair_style"] == "buy_first"
    assert intent["quantity"] == 100
    assert intent["target_shares"] == 500


class _InsertCollection:
    """Minimal insert/find double for the intraday_t0_trades ledger."""

    def __init__(self, rows=None):
        self.rows = rows if rows is not None else []

    def find_one(self, query, projection=None):
        for row in self.rows:
            if all(row.get(k) == v for k, v in query.items() if k != "_id"):
                return row
        return None

    def find(self, query):
        class _Cursor:
            def __init__(self, rows):
                self._rows = rows

            def sort(self, *_a, **_k):
                return self

            def __iter__(self):
                return iter(self._rows)

        rows = [r for r in self.rows if r.get("user_id") == query.get("user_id")]
        if "pair_id" in query:
            rows = [r for r in rows if r.get("pair_id") == query.get("pair_id")]
        return _Cursor(rows)

    def insert_one(self, doc):
        doc = dict(doc)
        doc["_id"] = ObjectId()
        self.rows.append(doc)

        class _Result:
            inserted_id = doc["_id"]

        return _Result()


def _patch_trade_ledger_env(monkeypatch, signal_id):
    """Wire the trade-ledger collections for sync_execution_trade_leg tests."""
    signals = FakeCollection(
        [
            {
                "_id": ObjectId(signal_id),
                "user_id": "u1",
                "symbol": "300750.SZ",
                "signal_type": "BUY_WATCH",
                "price": 198.5,
                "trade_date": "20260820",
            }
        ]
    )
    trades_coll = _InsertCollection()
    monkeypatch.setattr(
        execution,
        "get_db_collections",
        lambda: {
            "intraday_t0_trades": trades_coll,
            "intraday_t0_signals": signals,
            "trader_positions": FakeCollection(
                [{"user_id": "u1", "symbol": "300750.SZ", "available_qty": 500}]
            ),
        },
    )
    monkeypatch.setattr("services.intraday_t0_trades.get_db_collections", execution.get_db_collections)
    monkeypatch.setattr(execution, "ensure_intraday_t0_execution_indexes", lambda: None)
    monkeypatch.setattr("services.intraday_t0_trades.ensure_intraday_t0_trade_indexes", lambda: None)
    return trades_coll


@pytest.mark.unit
def test_sync_execution_trade_leg_records_open_pair(monkeypatch):
    signal_id = str(ObjectId())
    _patch_trade_ledger_env(monkeypatch, signal_id)

    intent = {
        "signal_id": signal_id,
        "symbol": "300750.SZ",
        "action": "buy",
        "quantity": 100,
        "pair_id": "pair-buy-first",
        "pair_style": "buy_first",
    }
    signal = {"price": 198.5, "trade_date": "20260820"}
    result = execution.sync_execution_trade_leg("u1", intent, signal)
    assert result is not None
    assert result["data"]["pair_id"] == "pair-buy-first"
    assert result["data"]["leg"] == "buy"
    assert execution.sync_execution_trade_leg("u1", intent, signal) is None


@pytest.mark.unit
def test_submitted_leg_quantity_sums_matching_orders():
    signals = [
        {"symbol": "300750.SZ", "action": "sell", "size": 200},
        {"symbol": "300750", "action": "sell", "size": 100},
        {"symbol": "300750.SZ", "action": "buy", "size": 900},
        {"symbol": "600000.SH", "action": "sell", "size": 700},
    ]
    assert execution.submitted_leg_quantity(signals, "300750.SZ", "sell") == 300
    assert execution.submitted_leg_quantity(signals, "300750.SZ", "buy") == 900
    assert execution.submitted_leg_quantity(signals, "000001.SZ", "sell") == 0


@pytest.mark.unit
def test_sync_execution_trade_leg_records_submitted_not_intended_qty(monkeypatch):
    """A sell capped by available_qty must not leave the pair over-covered."""
    signal_id = str(ObjectId())
    _patch_trade_ledger_env(monkeypatch, signal_id)

    intent = {
        "signal_id": signal_id,
        "symbol": "300750.SZ",
        "action": "sell",
        "quantity": 500,
        "pair_id": "pair-sell-first",
        "pair_style": "sell_first",
    }
    signal = {"price": 198.5, "trade_date": "20260820"}
    result = execution.sync_execution_trade_leg("u1", intent, signal, submitted_qty=200)
    assert result is not None
    assert result["data"]["quantity"] == 200


@pytest.mark.unit
def test_sync_execution_trade_leg_falls_back_to_intent_when_unmatched(monkeypatch):
    signal_id = str(ObjectId())
    _patch_trade_ledger_env(monkeypatch, signal_id)

    intent = {
        "signal_id": signal_id,
        "symbol": "300750.SZ",
        "action": "sell",
        "quantity": 300,
        "pair_id": "pair-sell-first",
        "pair_style": "sell_first",
    }
    signal = {"price": 198.5, "trade_date": "20260820"}
    result = execution.sync_execution_trade_leg("u1", intent, signal, submitted_qty=0)
    assert result is not None
    assert result["data"]["quantity"] == 300


@pytest.mark.unit
def test_execution_exists_for_signal(monkeypatch):
    signal_id = "sig-1"
    executions = FakeCollection([{"signal_id": signal_id, "user_id": "u1"}])
    monkeypatch.setattr(
        execution,
        "get_db_collections",
        lambda: {"intraday_t0_executions": executions},
    )
    monkeypatch.setattr(execution, "ensure_intraday_t0_execution_indexes", lambda: None)
    assert execution.execution_exists_for_signal(signal_id) is True
    assert execution.execution_exists_for_signal("other") is False


@pytest.mark.unit
def test_maybe_auto_execute_skipped_without_global_flag(monkeypatch):
    monkeypatch.setenv("INTRADAY_T0_AUTO_EXECUTE_ENABLED", "false")
    called = {"n": 0}

    async def _fake_submit(*_a, **_k):
        called["n"] += 1
        return {}

    monkeypatch.setattr(execution, "submit_execution_async", _fake_submit)
    monkeypatch.setattr(
        execution,
        "get_user_settings",
        lambda _uid: {"data": {"auto_execute_enabled": True, "auto_min_score": 80}},
    )
    result = execution.maybe_auto_execute_after_signal(
        "u1",
        {"_id": ObjectId(), "signal_type": "SELL_WATCH", "signal_score": 90},
    )
    assert result is None
    assert called["n"] == 0
