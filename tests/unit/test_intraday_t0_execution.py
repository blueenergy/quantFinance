"""Unit tests for intraday T+0 live execution."""

from __future__ import annotations

import pytest
from bson import ObjectId

from services import intraday_t0_execution as execution
from tests.unit.test_market_insight_engine import FakeCollection


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

    intent = execution.build_execution_intent("u1", signal_id)
    assert intent["action"] == "sell"
    assert intent["quantity"] == 200
    assert intent["target_shares"] == 300
    assert intent["plan_id"] == "plan-1"


@pytest.mark.unit
def test_build_buy_intent_requires_open_pair(monkeypatch):
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
    monkeypatch.setattr(
        execution,
        "get_db_collections",
        lambda: {
            "intraday_t0_signals": signals,
            "intraday_t0_execution_settings": settings_coll,
            "intraday_t0_executions": FakeCollection([]),
            "trader_positions": FakeCollection([]),
        },
    )
    monkeypatch.setattr(execution, "ensure_intraday_t0_execution_indexes", lambda: None)
    monkeypatch.setattr(
        execution,
        "preview_trade_from_signal",
        lambda *_a, **_k: {"available_qty": 0, "suggested_qty": 0, "warnings": []},
    )
    monkeypatch.setattr(execution, "_lineage_shares", lambda *_a, **_k: 300)
    monkeypatch.setattr(execution, "list_open_pairs", lambda *_a, **_k: [])

    with pytest.raises(ValueError, match="open sell pair"):
        execution.build_execution_intent("u1", signal_id)


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
