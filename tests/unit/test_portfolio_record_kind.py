"""Unit tests for the portfolio record_kind taxonomy.

Cases mirror the real shapes observed across two environments (50 docs):
monitor observations, cadence/manual rebalances, and legacy external-manual
ledger rows (which carry plan_type == "rebalance").
"""

import pytest

from services.portfolio_record_kind import (
    RECORD_KIND_LEDGER_EVENT,
    RECORD_KIND_OBSERVATION,
    RECORD_KIND_TRADE_PLAN,
    classify_record,
    is_strategy_decision,
    require_record_kind,
    strategy_decision_mongo_filter,
    strategy_generated_record_mongo_filter,
    strategy_rebalance_mongo_filter,
)


def test_monitor_is_observation():
    assert classify_record({"plan_type": "monitor", "status": "generated"}) == (
        RECORD_KIND_OBSERVATION
    )


def test_cadence_rebalance_is_trade_plan():
    assert classify_record(
        {"plan_type": "rebalance", "origin": "strategy", "status": "approved"}
    ) == RECORD_KIND_TRADE_PLAN


def test_manual_rebalance_is_trade_plan():
    # origin=manual is provenance, not kind: a manual rebalance is a trade_plan.
    assert classify_record(
        {"plan_type": "rebalance", "origin": "manual", "manual_action": "rebalance"}
    ) == RECORD_KIND_TRADE_PLAN
    assert classify_record(
        {"plan_type": "rebalance", "origin": "manual", "manual_action": "liquidate"}
    ) == RECORD_KIND_TRADE_PLAN


def test_external_manual_is_ledger_event_even_with_rebalance_plan_type():
    # Legacy ledger rows carry plan_type="rebalance"; manual_action wins.
    assert classify_record(
        {
            "plan_type": "rebalance",
            "origin": "manual",
            "manual_action": "external_manual",
            "status": "filled",
        }
    ) == RECORD_KIND_LEDGER_EVENT


def test_external_liquidation_is_ledger_event():
    assert classify_record({"manual_action": "external_liquidation"}) == (
        RECORD_KIND_LEDGER_EVENT
    )


def test_current_code_manual_plan_type_is_ledger_event():
    assert classify_record({"plan_type": "manual", "manual_action": "external_manual"}) == (
        RECORD_KIND_LEDGER_EVENT
    )


def test_unclassifiable_raises():
    with pytest.raises(ValueError):
        classify_record({"plan_type": "draft", "plan_id": "x"})


def test_require_record_kind_passes_through_valid():
    assert require_record_kind({"record_kind": RECORD_KIND_TRADE_PLAN}) == (
        RECORD_KIND_TRADE_PLAN
    )


def test_require_record_kind_lets_extman_override_bad_stamp():
    assert (
        require_record_kind(
            {
                "plan_id": "growth_cycle_index_enhanced_user_extman_20260624",
                "record_kind": RECORD_KIND_TRADE_PLAN,
            }
        )
        == RECORD_KIND_LEDGER_EVENT
    )


def test_require_record_kind_fails_loud_when_missing():
    with pytest.raises(ValueError):
        require_record_kind({"plan_id": "x"})


def test_is_strategy_decision_true_for_strategy_trade_plan():
    doc = {
        "record_kind": RECORD_KIND_TRADE_PLAN,
        "origin": "strategy",
        "plan_type": "rebalance",
    }
    assert is_strategy_decision(doc) is True


def test_is_strategy_decision_false_for_manual_rebalance():
    doc = {
        "record_kind": RECORD_KIND_TRADE_PLAN,
        "origin": "manual",
        "plan_type": "rebalance",
        "manual_action": "rebalance",
    }
    assert is_strategy_decision(doc) is False


def test_is_strategy_decision_false_for_monitor():
    doc = {
        "record_kind": RECORD_KIND_OBSERVATION,
        "origin": "strategy",
        "plan_type": "monitor",
    }
    assert is_strategy_decision(doc) is False


def test_is_strategy_decision_preserves_ledger_override():
    assert (
        is_strategy_decision(
            {
                "record_kind": RECORD_KIND_TRADE_PLAN,
                "origin": "strategy",
                "manual_action": "external_manual",
            }
        )
        is False
    )


def test_is_strategy_decision_fails_loud_without_record_kind():
    with pytest.raises(ValueError, match="missing/invalid record_kind"):
        is_strategy_decision({"origin": "strategy", "plan_type": "rebalance"})


def test_strategy_generated_record_filter_includes_observations():
    assert strategy_generated_record_mongo_filter(base_date="20250101") == {
        "origin": {"$ne": "manual"},
        "base_date": "20250101",
    }


def test_strategy_decision_mongo_filter_shape():
    assert strategy_decision_mongo_filter(base_date="20250101") == {
        "origin": {"$ne": "manual"},
        "$or": [
            {"record_kind": RECORD_KIND_TRADE_PLAN},
            {"record_kind": {"$exists": False}},
        ],
        "base_date": "20250101",
    }


def test_strategy_rebalance_mongo_filter_adds_plan_type():
    assert strategy_rebalance_mongo_filter(user_id="u1") == {
        "origin": {"$ne": "manual"},
        "$or": [
            {"record_kind": RECORD_KIND_TRADE_PLAN},
            {"record_kind": {"$exists": False}},
        ],
        "plan_type": "rebalance",
        "user_id": "u1",
    }
