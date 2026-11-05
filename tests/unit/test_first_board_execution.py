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


def _sector(symbol: str, l1: float = 0.90, l3: float = 1.0) -> dict:
    return {
        "sector_l1_name": "测试板块",
        "sector_l1_above_ma5_ratio": l1,
        "sector_l3_name": "测试三级",
        "sector_l3_median_pct_chg": l3,
        "sector_snapshot_time": "202611051016",
    }


def _build(candidates, *, equity=500_000.0, cash=200_000.0, positions=None, settings=None, sector=None):
    sector_map = sector if sector is not None else {
        cand["symbol"]: _sector(cand["symbol"]) for cand in candidates
    }
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
        sector_strength=sector_map,
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


def test_sector_gate_l1_breadth_and_l3_median():
    candidates = [
        _cand("600001.SH", 90, 20.0),
        _cand("600002.SH", 80, 20.0),
        _cand("600003.SH", 70, 20.0),
    ]
    sector = {
        "600001.SH": _sector("600001.SH", 0.90, 1.0),
        "600002.SH": _sector("600002.SH", 0.50, 1.0),
        "600003.SH": _sector("600003.SH", 0.90, -0.5),
    }
    intents, dropped = _build(candidates, sector=sector)
    assert [row["symbol"] for row in intents] == ["600001.SH"]
    reasons = {row["symbol"]: row["reason"] for row in dropped}
    assert reasons["600002.SH"] == "sector_l1_breadth_weak"
    assert reasons["600003.SH"] == "sector_l3_median_weak"


def test_sector_data_missing_is_skipped():
    intents, dropped = _build([_cand("600001.SH", 90, 20.0)], sector={})
    assert intents == []
    assert dropped[0]["reason"] == "sector_data_missing"


def test_run_shadow_writes_intents_and_run():
    finance = mongomock.MongoClient().finance
    trading = mongomock.MongoClient().quant_trading
    data = mongomock.MongoClient().quant_data
    realtime = mongomock.MongoClient().quant_realtime
    trading.first_board_execution_settings.insert_one({
        "account_id": "62129066",
        "gates": {"sector": {"enabled": False}},
    })
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
        realtime_db=realtime,
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
    realtime = mongomock.MongoClient().quant_realtime
    trading.first_board_execution_settings.insert_one({
        "account_id": "8885156549",
        "gates": {"sector": {"enabled": False}},
    })
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
        realtime_db=realtime,
        trade_date="20261105",
        account_key="8885156549",
        dry_run=False,
    )
    assert summary["intents"] == 0
    assert summary["dropped_rows"][0]["reason"] == "below_one_lot"
    assert trading.first_board_order_intents.count_documents({}) == 0


def test_run_shadow_sector_gate_drops_weak_sector():
    finance = mongomock.MongoClient().finance
    trading = mongomock.MongoClient().quant_trading
    data = mongomock.MongoClient().quant_data
    realtime = mongomock.MongoClient().quant_realtime
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
        _cand("600002.SH", 80, 20.0),
        _cand("600003.SH", 70, 20.0),
    ])
    data.sw_index_member.insert_many([
        {"con_code": "600001.SH", "src": "SW2021", "is_new": "Y",
         "l1_code": "L1A", "l1_name": "板块A", "l3_code": "L3A", "l3_name": "三级A"},
        {"con_code": "600002.SH", "src": "SW2021", "is_new": "Y",
         "l1_code": "L1B", "l1_name": "板块B", "l3_code": "L3B", "l3_name": "三级B"},
        {"con_code": "600003.SH", "src": "SW2021", "is_new": "Y",
         "l1_code": "L1C", "l1_name": "板块C", "l3_code": "L3C", "l3_name": "三级C"},
    ])
    snap = "202611051016"
    realtime.realtime_sector_strength.insert_many([
        {"snapshot_time": snap, "ma_period": 5, "level": "L1", "sector_code": "L1A", "above_ma5_ratio": 0.90, "median_pct_chg": 1.0},
        {"snapshot_time": snap, "ma_period": 5, "level": "L3", "sector_code": "L3A", "above_ma5_ratio": 0.90, "median_pct_chg": 1.0},
        {"snapshot_time": snap, "ma_period": 5, "level": "L1", "sector_code": "L1B", "above_ma5_ratio": 0.50, "median_pct_chg": 1.0},
        {"snapshot_time": snap, "ma_period": 5, "level": "L3", "sector_code": "L3B", "above_ma5_ratio": 0.90, "median_pct_chg": 1.0},
        {"snapshot_time": snap, "ma_period": 5, "level": "L1", "sector_code": "L1C", "above_ma5_ratio": 0.90, "median_pct_chg": 1.0},
        {"snapshot_time": snap, "ma_period": 5, "level": "L3", "sector_code": "L3C", "above_ma5_ratio": 0.90, "median_pct_chg": -0.5},
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
        realtime_db=realtime,
        trade_date="20261105",
        account_key="62129066",
        dry_run=False,
    )
    assert summary["intents"] == 1
    assert summary["intent_rows"][0]["symbol"] == "600001.SH"
    reasons = {row["symbol"]: row["reason"] for row in summary["dropped_rows"]}
    assert reasons["600002.SH"] == "sector_l1_breadth_weak"
    assert reasons["600003.SH"] == "sector_l3_median_weak"
