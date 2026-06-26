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
    require_record_kind,
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
