from __future__ import annotations

import mongomock
import pytest

from services.first_board_execution import (
    DEFAULT_SETTINGS,
    build_intents,
    run_shadow,
)


def _cand(symbol: str, score: float, price: float = 20.0, gap: float = 3.0, name: str = "") -> dict:
    return {
        "trade_date": "20261105",
        "symbol": symbol,
        "name": name or symbol,
        "score": score,
        "price_0925": price,
        "gap_pct": gap,
        "auction_volume_ratio": 0.05,
        "seal_ratio": 0.08,
    }


def _build(candidates, *, equity=500_000.0, cash=200_000.0, positions=None, settings=None):
    return build_intents(
        candidates,
        settings=settings or DEFAULT_SETTINGS,
        equity=equity,
        available_cash=cash,
        current_positions=positions or {},
        trade_date="20261105",
        account_id="62129066",
        securities_account_id="acc1",
        user_id="u1",
    )


def test_fixed_amount_and_max_positions():
    intents, dropped = _build([
        _cand("600001.SH", 90, 20.0),
        _cand("600002.SH", 80, 10.0),
        _cand("600003.SH", 70, 20.0),
        _cand("600004.SH", 60, 10.0),
    ])
    assert [row["symbol"] for row in intents] == ["600001.SH", "600002.SH", "600003.SH"]
    assert [row["target_shares"] for row in intents] == [2500, 5000, 2500]
    assert all(row["order_amount"] == pytest.approx(50_000.0) for row in intents)
    assert dropped[-1]["reason"] == "max_positions"


def test_cash_reserve_caps_second_order():
    settings = {
        **DEFAULT_SETTINGS,
        "sizing": {**DEFAULT_SETTINGS["sizing"], "max_position_pct": 1.0},
    }
    intents, dropped = _build(
        [_cand("600001.SH", 90, 20.0), _cand("600002.SH", 80, 20.0)],
        equity=100_000.0,
        cash=60_000.0,
        settings=settings,
    )
    assert len(intents) == 1
    assert intents[0]["target_shares"] == 2500
    assert dropped[0]["reason"] == "below_one_lot"


def test_max_position_pct_caps_target_amount():
    intents, _ = _build(
        [_cand("600001.SH", 90, 20.0)],
        equity=200_000.0,
        cash=200_000.0,
    )
    assert len(intents) == 1
    assert intents[0]["target_amount"] == pytest.approx(40_000.0)
    assert intents[0]["target_shares"] == 2000


def test_below_one_lot_is_dropped():
    intents, dropped = _build([_cand("600001.SH", 90, 1000.0)])
    assert intents == []
    assert dropped[0]["reason"] == "below_one_lot"


def test_limit_up_at_auction_is_skipped():
    intents, dropped = _build([_cand("600001.SH", 90, 11.0, gap=10.0)])
    assert intents == []
    assert dropped[0]["reason"] == "limit_up_at_auction"


def test_excludes_bj_st_and_existing_position():
    intents, dropped = _build(
        [
            _cand("920001.BJ", 99, 10.0),
            _cand("600002.SH", 98, 10.0, name="ST某某"),
            _cand("600003.SH", 97, 10.0),
        ],
        positions={"600003.SH": {"qty": 100}},
    )
    reasons = {row["symbol"]: row["reason"] for row in dropped}
    assert reasons["920001.BJ"] == "excluded_bj"
    assert reasons["600002.SH"] == "excluded_st"
    assert reasons["600003.SH"] == "existing_position"
    assert intents == []


def test_run_shadow_writes_intents_and_run():
    finance = mongomock.MongoClient().finance
    trading = mongomock.MongoClient().quant_trading
    data = mongomock.MongoClient().quant_data
    finance.trader_accounts.insert_one({
        "user_id": "u1",
        "account_id": "62129066",
        "securities_account_id": "acc1",
        "total_asset": 500_000.0,
        "cash": 200_000.0,
        "available_cash": 200_000.0,
        "market_value": 300_000.0,
    })
    data.first_board_candidates.insert_many([
        _cand("600001.SH", 90, 20.0),
        _cand("600002.SH", 80, 10.0),
        _cand("600003.SH", 70, 20.0),
        _cand("600004.SH", 60, 10.0),
    ])
    summary = run_shadow(
        finance_collections={
            "trader_accounts": finance.trader_accounts,
            "trader_positions": finance.trader_positions,
        },
        trading_collections={
            "first_board_execution_settings": trading.first_board_execution_settings,
            "first_board_order_intents": trading.first_board_order_intents,
            "first_board_execution_runs": trading.first_board_execution_runs,
        },
        data_db=data,
        trade_date="20261105",
        account_key="62129066",
        dry_run=False,
    )
    assert summary["intents"] == 3
    assert summary["target_amount"] == pytest.approx(150_000.0)
    assert trading.first_board_order_intents.count_documents({}) == 3
    run = trading.first_board_execution_runs.find_one({})
    assert run is not None
    assert run["intents"] == 3
    assert run["mode"] == "shadow"


def test_run_shadow_low_cash_account_has_no_intents():
    finance = mongomock.MongoClient().finance
    trading = mongomock.MongoClient().quant_trading
    data = mongomock.MongoClient().quant_data
    finance.trader_accounts.insert_one({
        "user_id": "u1",
        "account_id": "8885156549",
        "securities_account_id": "acc2",
        "total_asset": 486_302.58,
        "cash": 4_594.28,
        "available_cash": 4_594.28,
        "market_value": 481_708.30,
    })
    data.first_board_candidates.insert_one(_cand("600001.SH", 90, 20.0))
    summary = run_shadow(
        finance_collections={
            "trader_accounts": finance.trader_accounts,
            "trader_positions": finance.trader_positions,
        },
        trading_collections={
            "first_board_execution_settings": trading.first_board_execution_settings,
            "first_board_order_intents": trading.first_board_order_intents,
            "first_board_execution_runs": trading.first_board_execution_runs,
        },
        data_db=data,
        trade_date="20261105",
        account_key="8885156549",
        dry_run=False,
    )
    assert summary["intents"] == 0
    assert summary["dropped_rows"][0]["reason"] == "below_one_lot"
    assert trading.first_board_order_intents.count_documents({}) == 0
