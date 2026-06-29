"""Unit tests for portfolio plan gap-fill (top-up) remainder math."""

from __future__ import annotations

from datetime import datetime

from services import portfolio_plan_remainder as rem


def _plan(lot_size: int = 100):
    return {
        "plan_id": "plan-1",
        "user_id": "u1",
        "strategy_template_id": "st1",
        "params_snapshot": {"lot_size": lot_size},
    }


def _item(**over):
    base = {
        "symbol": "000001.SZ",
        "name": "X",
        "rank": 1,
        "target_shares": 1000,
        "delta_shares": 1000,
        "estimated_price": 10.0,
        "account_current_shares": 0,
        "account_available_shares": 0,
    }
    base.update(over)
    return base


def _price(px, source="realtime"):
    return lambda symbol: (px, source)


def _spec_for(specs, symbol="000001.SZ"):
    return next(s for s in specs if s.symbol == symbol)


def test_buy_gap_fill_no_caps():
    specs = rem.build_remainder_specs(
        plan=_plan(),
        enriched_items=[_item(account_current_shares=400)],
        available_cash=1_000_000,
        account_synced=True,
        price_resolver=_price(10.0),
    )
    spec = _spec_for(specs)
    assert spec.action == "buy"
    assert spec.gap_shares == 600
    assert spec.topup_shares == 600
    assert spec.actionable is True
    assert rem.REASON_BUDGET_CAPPED not in spec.reasons


def test_buy_budget_capped_when_price_rises():
    specs = rem.build_remainder_specs(
        plan=_plan(),
        enriched_items=[_item(account_current_shares=400)],
        available_cash=1_000_000,
        account_synced=True,
        price_resolver=_price(15.0),
    )
    spec = _spec_for(specs)
    # planned budget 10000, current value 6000, remaining 4000 -> 4000/15 -> 200 lots
    assert spec.topup_shares == 200
    assert rem.REASON_BUDGET_CAPPED in spec.reasons


def test_buy_cash_capped():
    specs = rem.build_remainder_specs(
        plan=_plan(),
        enriched_items=[_item(account_current_shares=0)],
        available_cash=3000,
        account_synced=True,
        price_resolver=_price(10.0),
    )
    spec = _spec_for(specs)
    assert spec.topup_shares == 300
    assert spec.estimated_amount == 3000
    assert rem.REASON_CASH_CAPPED in spec.reasons


def test_already_at_target_not_actionable():
    specs = rem.build_remainder_specs(
        plan=_plan(),
        enriched_items=[_item(target_shares=500, account_current_shares=500)],
        available_cash=1_000_000,
        account_synced=True,
        price_resolver=_price(10.0),
    )
    spec = _spec_for(specs)
    assert spec.actionable is False
    assert rem.REASON_ALREADY_AT_TARGET in spec.reasons


def test_sell_remainder_fill():
    specs = rem.build_remainder_specs(
        plan=_plan(),
        enriched_items=[
            _item(
                target_shares=0,
                delta_shares=-1000,
                account_current_shares=300,
                account_available_shares=300,
            )
        ],
        available_cash=0,
        account_synced=True,
        price_resolver=_price(10.0),
    )
    spec = _spec_for(specs)
    assert spec.action == "sell"
    assert spec.topup_shares == 300
    assert spec.actionable is True


def test_sell_residual_odd_lot_when_available_below_gap():
    specs = rem.build_remainder_specs(
        plan=_plan(),
        enriched_items=[
            _item(
                target_shares=0,
                delta_shares=-1000,
                account_current_shares=300,
                account_available_shares=50,
            )
        ],
        available_cash=0,
        account_synced=True,
        price_resolver=_price(10.0),
    )
    spec = _spec_for(specs)
    assert spec.topup_shares == 50
    assert rem.REASON_AVAILABLE_BELOW_GAP in spec.reasons


def test_sell_uses_lineage_book_not_shared_account_aggregate():
    # Shared account: this plan's lineage book holds 200, but the broker account
    # aggregate (incl. another plan) shows 500. A full exit (target 0) must only
    # sell THIS plan's 200, never the other plan's shares.
    specs = rem.build_remainder_specs(
        plan=_plan(),
        enriched_items=[
            _item(
                target_shares=0,
                delta_shares=-1000,
                strategy_current_shares=200,
                current_shares=200,
                account_current_shares=500,
                account_available_shares=500,
            )
        ],
        available_cash=0,
        account_synced=True,
        price_resolver=_price(10.0),
    )
    spec = _spec_for(specs)
    assert spec.action == "sell"
    assert spec.baseline_current_shares == 200
    assert spec.gap_shares == 200
    assert spec.topup_shares == 200


def test_buy_gap_uses_lineage_book_not_shared_account_aggregate():
    # Shared account holds 2000 of the symbol for OTHER plans; this plan's lineage
    # book holds 0. Topping up to target 500 must buy 500, not treat the position
    # as already-at-target because of the contaminated account aggregate.
    specs = rem.build_remainder_specs(
        plan=_plan(),
        enriched_items=[
            _item(
                target_shares=500,
                delta_shares=500,
                strategy_current_shares=0,
                current_shares=0,
                account_current_shares=2000,
                account_available_shares=2000,
            )
        ],
        available_cash=1_000_000,
        account_synced=True,
        price_resolver=_price(10.0),
    )
    spec = _spec_for(specs)
    assert spec.action == "buy"
    assert spec.baseline_current_shares == 0
    assert spec.gap_shares == 500
    assert spec.topup_shares == 500


def test_account_not_synced_blocks():
    specs = rem.build_remainder_specs(
        plan=_plan(),
        enriched_items=[_item(account_current_shares=None)],
        available_cash=None,
        account_synced=False,
        price_resolver=_price(10.0),
    )
    spec = _spec_for(specs)
    assert spec.actionable is False
    assert rem.REASON_ACCOUNT_NOT_SYNCED in spec.reasons


def test_active_signal_symbol_skipped():
    specs = rem.build_remainder_specs(
        plan=_plan(),
        enriched_items=[_item(account_current_shares=0)],
        available_cash=1_000_000,
        account_synced=True,
        active_symbols={"000001"},
        price_resolver=_price(10.0),
    )
    spec = _spec_for(specs)
    assert spec.actionable is False
    assert rem.REASON_LIVE_SIGNAL_ACTIVE in spec.reasons


def test_non_planned_trade_skipped():
    specs = rem.build_remainder_specs(
        plan=_plan(),
        enriched_items=[_item(delta_shares=0, account_current_shares=0)],
        available_cash=1_000_000,
        account_synced=True,
        price_resolver=_price(10.0),
    )
    spec = _spec_for(specs)
    assert spec.actionable is False
    assert rem.REASON_NOT_PLANNED_TRADE in spec.reasons


def test_build_topup_signal_doc_buy():
    specs = rem.build_remainder_specs(
        plan=_plan(),
        enriched_items=[_item(account_current_shares=400)],
        available_cash=1_000_000,
        account_synced=True,
        price_resolver=_price(10.0),
    )
    spec = _spec_for(specs)
    account = {
        "_id": "acc-oid",
        "account_id": "62666676",
        "broker": "miniQMT",
        "fee_model": {},
    }
    doc = rem.build_topup_signal_doc(
        plan=_plan(),
        account=account,
        spec=spec,
        now=datetime.utcnow(),
        user_id="u1",
        reason="finish underfilled buy",
        replaces_order_ids=["live-plan-abc"],
    )
    assert doc["source"] == rem.LIVE_SIGNAL_SOURCE_PLAN_TOPUP
    assert doc["action"] == "buy"
    assert doc["size"] == 600
    assert doc["order_id"].startswith("live-topup-")
    assert doc["replaces_order_ids"] == ["live-plan-abc"]
    assert doc["topup_baseline"]["gap_shares"] == 600
    assert doc["price_ceiling"] is not None
    assert doc["mode"] == "live"


def test_running_cash_split_across_two_buys():
    specs = rem.build_remainder_specs(
        plan=_plan(),
        enriched_items=[
            _item(symbol="000001.SZ", rank=1, target_shares=1000, account_current_shares=0),
            _item(symbol="000002.SZ", rank=2, target_shares=1000, account_current_shares=0),
        ],
        available_cash=12000,
        account_synced=True,
        price_resolver=_price(10.0),
    )
    first = _spec_for(specs, "000001.SZ")
    second = _spec_for(specs, "000002.SZ")
    # rank 1 takes 1000 shares (10000), leaving 2000 -> rank 2 only 200 shares.
    assert first.topup_shares == 1000
    assert second.topup_shares == 200
    assert rem.REASON_CASH_CAPPED in second.reasons
