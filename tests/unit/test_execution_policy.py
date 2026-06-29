from services.execution_policy import (
    BALANCED_MAX_SLIPPAGE_BPS,
    OPEN_AUCTION_MAX_SLIPPAGE_BPS,
    STYLE_BALANCED,
    STYLE_OPEN_AUCTION,
    build_price_guard,
    protected_limit_price,
    resolve_execution_policy,
)


def test_resolve_execution_policy_balanced_for_realtime_source():
    policy = resolve_execution_policy(reference_price_source="realtime")

    assert policy.style == STYLE_BALANCED
    assert policy.max_slippage_bps == BALANCED_MAX_SLIPPAGE_BPS


def test_resolve_execution_policy_open_auction_for_daily_close():
    policy = resolve_execution_policy(reference_price_source="daily_close")

    assert policy.style == STYLE_OPEN_AUCTION
    assert policy.max_slippage_bps == OPEN_AUCTION_MAX_SLIPPAGE_BPS


def test_resolve_execution_policy_account_override():
    policy = resolve_execution_policy(
        {"risk_limits": {"max_slippage_bps": 50}},
        reference_price_source="daily_close",
    )

    assert policy.max_slippage_bps == 50


def test_build_price_guard_buy_balanced_example():
    policy = resolve_execution_policy(reference_price_source="estimated_price")
    guard = build_price_guard(66.71, "buy", policy)

    assert guard.max_slippage_bps == 30
    assert guard.reference_price == 66.71
    assert guard.price_ceiling == 66.92
    assert guard.effective_limit_price == 66.92


def test_build_price_guard_sell_balanced_example():
    policy = resolve_execution_policy(reference_price_source="realtime")
    guard = build_price_guard(66.71, "sell", policy)

    assert guard.price_floor == 66.50
    assert guard.effective_limit_price == 66.50


def test_build_price_guard_open_auction_buy_example():
    policy = resolve_execution_policy(reference_price_source="daily_close")
    guard = build_price_guard(66.71, "buy", policy)

    assert guard.max_slippage_bps == 100
    assert guard.price_ceiling == 67.38
    assert protected_limit_price(66.71, 100, action="buy") == 67.38
