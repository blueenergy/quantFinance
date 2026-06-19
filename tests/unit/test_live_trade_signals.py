from datetime import datetime, timezone

from services.live_trade_signals import (
    PHASE_BARRIER_PLAN_SELLS_TERMINAL,
    PLAN_PHASE_APPROVED_PUBLISHED,
    PLAN_PHASE_BUYING,
    PLAN_PHASE_DONE,
    PLAN_PHASE_SELL_SETTLED,
    PLAN_PHASE_SELLING,
    build_plan_sells_terminal_barrier,
    default_activate_after,
    derive_execution_phase_state,
    sells_barrier_satisfied,
)


def test_default_activate_after_anchors_pre_open_to_0925_cn_time():
    now = datetime(2026, 6, 19, 1, 0, tzinfo=timezone.utc)  # 09:00 Asia/Shanghai

    activate_after = default_activate_after(now)

    assert activate_after == datetime(2026, 6, 19, 1, 25)


def test_default_activate_after_after_close_uses_next_weekday_0925():
    now = datetime(2026, 6, 19, 8, 0, tzinfo=timezone.utc)  # Friday 16:00 Asia/Shanghai

    activate_after = default_activate_after(now)

    assert activate_after == datetime(2026, 6, 22, 1, 25)


def test_build_plan_sells_terminal_barrier_shape():
    barrier = build_plan_sells_terminal_barrier("plan-1")

    assert barrier == {"type": PHASE_BARRIER_PLAN_SELLS_TERMINAL, "plan_id": "plan-1"}


def test_sells_barrier_satisfied_when_no_sells():
    signals = [{"execution_phase": "buy", "status": "pending", "mode": "live"}]

    assert sells_barrier_satisfied(signals) is True


def test_sells_barrier_blocks_on_in_flight_sell():
    signals = [
        {"execution_phase": "sell", "status": "submitted", "mode": "live"},
        {"execution_phase": "buy", "status": "pending", "mode": "live"},
    ]

    assert sells_barrier_satisfied(signals) is False


def test_sells_barrier_satisfied_when_cancelled_sell_counts_as_terminal():
    signals = [
        {"execution_phase": "sell", "status": "cancelled", "mode": "live"},
        {"execution_phase": "sell", "status": "filled", "mode": "live"},
    ]

    assert sells_barrier_satisfied(signals) is True


def test_derive_phase_state_no_signals_is_approved_published():
    assert derive_execution_phase_state([]) == PLAN_PHASE_APPROVED_PUBLISHED


def test_derive_phase_state_selling_while_sell_in_flight():
    signals = [
        {"execution_phase": "sell", "status": "submitted", "mode": "live"},
        {"execution_phase": "buy", "status": "pending", "mode": "live"},
    ]

    assert derive_execution_phase_state(signals) == PLAN_PHASE_SELLING


def test_derive_phase_state_sell_settled_when_buys_still_pending():
    signals = [
        {"execution_phase": "sell", "status": "filled", "mode": "live"},
        {"execution_phase": "buy", "status": "pending", "mode": "live"},
    ]

    assert derive_execution_phase_state(signals) == PLAN_PHASE_SELL_SETTLED


def test_derive_phase_state_buying_when_buy_submitted():
    signals = [
        {"execution_phase": "sell", "status": "filled", "mode": "live"},
        {"execution_phase": "buy", "status": "submitted", "mode": "live"},
    ]

    assert derive_execution_phase_state(signals) == PLAN_PHASE_BUYING


def test_derive_phase_state_done_when_all_terminal():
    signals = [
        {"execution_phase": "sell", "status": "filled", "mode": "live"},
        {"execution_phase": "buy", "status": "filled", "mode": "live"},
    ]

    assert derive_execution_phase_state(signals) == PLAN_PHASE_DONE


def test_derive_phase_state_sell_only_plan_is_done_when_sells_terminal():
    signals = [{"execution_phase": "sell", "status": "filled", "mode": "live"}]

    assert derive_execution_phase_state(signals) == PLAN_PHASE_DONE
