"""Unit tests for intraday T+0 signal engine (Phase 1 gate)."""

from __future__ import annotations

import pytest

from services import intraday_t0_signals as t0
from tests.unit.test_market_insight_engine import FakeCollection, FakeCursor


@pytest.mark.unit
@pytest.mark.parametrize(
    ("snapshot_time", "expected"),
    [
        ("202608201030", "10:00-11:30"),
        ("202608200930", "09:30-10:00"),
        ("202608201129", "10:00-11:30"),
        ("202608201130", "13:00-14:00"),  # 11:30 is lunch boundary (value >= 1130)
        ("202608201359", "13:00-14:00"),
        ("202608201400", "14:00-14:30"),  # 14:00 starts afternoon sell bucket
        ("202608201420", "14:00-14:30"),
        ("202608201500", "14:30-15:00"),
        ("bad", "unknown"),
    ],
)
def test_time_bucket(snapshot_time, expected):
    assert t0.time_bucket(snapshot_time) == expected


@pytest.mark.unit
def test_calc_bid_ask_imbalance_and_spread():
    position = {
        "realtime_bid_volume1": 700,
        "realtime_ask_volume1": 300,
        "realtime_bid_price1": 9.99,
        "realtime_ask_price1": 10.01,
        "current_price": 10.0,
        "high": 10.5,
        "low": 9.5,
    }
    assert t0.calc_bid_ask_imbalance(position) == pytest.approx(0.4)
    assert t0.calc_spread_pct(position) == pytest.approx(0.2)
    assert t0.calc_intraday_position(position) == pytest.approx(0.5)


@pytest.mark.unit
def test_calc_helpers_return_none_when_data_missing():
    assert t0.calc_bid_ask_imbalance({}) is None
    assert t0.calc_spread_pct({}) is None
    assert t0.calc_intraday_position({"current_price": 10, "high": 10, "low": 10}) is None


@pytest.mark.unit
def test_classify_signal_emits_sell_watch_near_high():
    # pnl_pct=-5.0（深套）也不影响卖出评分：+16 卖压来自日内涨幅 pct_chg，不是持仓成本（P0-1）
    features = {
        "intraday_position": 0.85,
        "pnl_pct": -5.0,
        "bid_ask_imbalance": -0.25,
        "sector_positive_ratio": 0.30,
        "market_yang_spectrum": 0.35,
        "time_of_day": "14:00-14:30",
        "pct_chg": 2.0,
        "avg_trade_size_yuan": 25000,
    }
    signal_type, score, reasons = t0.classify_signal({}, features, "202608201420")
    assert signal_type == "SELL_WATCH"
    assert score >= 58
    assert "near_intraday_high" in reasons
    assert "ask_pressure_stronger" in reasons
    assert "intraday_gain_1pct" in reasons
    assert "position_has_profit" not in reasons


@pytest.mark.unit
def test_classify_signal_emits_buy_watch_near_low():
    features = {
        "intraday_position": 0.20,
        "bid_ask_imbalance": 0.25,
        "above_ma5": True,
        "market_yang_spectrum": 0.55,
        "sector_positive_ratio": 0.55,
        "time_of_day": "14:30-15:00",
        "pct_chg": 0.8,
        "avg_trade_size_yuan": 22000,
    }
    signal_type, score, reasons = t0.classify_signal({}, features, "202608201450")
    assert signal_type == "BUY_WATCH"
    assert score >= 58
    assert "near_intraday_low" in reasons
    assert "late_session_t1_premium_setup" in reasons


@pytest.mark.unit
def test_classify_signal_holds_when_scores_mixed():
    features = {
        "intraday_position": 0.50,
        "pnl_pct": 0.2,
        "bid_ask_imbalance": 0.05,
        "market_yang_spectrum": 0.48,
        "sector_positive_ratio": 0.48,
        "time_of_day": "10:00-11:30",
    }
    signal_type, score, reasons = t0.classify_signal({}, features, "202608201100")
    assert signal_type == "HOLD"
    assert score < 58
    assert "no_high_quality_signal" in reasons


@pytest.mark.unit
def test_cooldown_suppressed_within_window(monkeypatch):
    signals = FakeCollection(
        [
            {
                "user_id": "u1",
                "symbol": "300750.SZ",
                "signal_type": "SELL_WATCH",
                "snapshot_time": "202608201030",
                "price": 100.0,
            }
        ]
    )
    monkeypatch.setattr(
        t0,
        "get_db_collections",
        lambda: {"intraday_t0_signals": signals},
    )
    assert t0.cooldown_suppressed("u1", "300750.SZ", "SELL_WATCH", "202608201040", 100.0) is True
    assert t0.cooldown_suppressed("u1", "300750.SZ", "SELL_WATCH", "202608201100", 100.0) is False


@pytest.mark.unit
def test_cooldown_reset_when_price_moves(monkeypatch):
    signals = FakeCollection(
        [
            {
                "user_id": "u1",
                "symbol": "300750.SZ",
                "signal_type": "BUY_WATCH",
                "snapshot_time": "202608201030",
                "price": 100.0,
            }
        ]
    )
    monkeypatch.setattr(
        t0,
        "get_db_collections",
        lambda: {"intraday_t0_signals": signals},
    )
    # 1% move resets cooldown even within 15 minutes
    assert t0.cooldown_suppressed("u1", "300750.SZ", "BUY_WATCH", "202608201035", 101.5) is False


@pytest.mark.unit
def test_cooldown_ignored_for_hold():
    assert t0.cooldown_suppressed("u1", "300750.SZ", "HOLD", "202608201035", 100.0) is False


@pytest.mark.unit
def test_compact_trade_date_accepts_multiple_formats():
    assert t0.compact_trade_date("2026-08-20") == "20260820"
    assert t0.compact_trade_date("20260820") == "20260820"


@pytest.mark.unit
def test_performance_summary_groups_by_signal_and_horizon(monkeypatch):
    evals = FakeCollection(
        [
            {
                "user_id": "u1",
                "signal_id": "sig1",
                "symbol": "300750.SZ",
                "signal_type": "SELL_WATCH",
                "horizon": "15m",
                "return_pct": -0.5,
            },
            {
                "user_id": "u1",
                "signal_id": "sig1",
                "symbol": "300750.SZ",
                "signal_type": "SELL_WATCH",
                "horizon": "15m",
                "return_pct": -0.2,
            },
            {
                "user_id": "u1",
                "signal_id": "sig2",
                "symbol": "600000.SH",
                "signal_type": "BUY_WATCH",
                "horizon": "15m",
                "return_pct": 0.3,
            },
        ]
    )
    signals = FakeCollection(
        [
            {
                "_id": "sig1",
                "user_id": "u1",
                "trade_date": "20260820",
                "signal_type": "SELL_WATCH",
                "reason_codes": ["near_intraday_high"],
            },
            {
                "_id": "sig2",
                "user_id": "u1",
                "trade_date": "20260820",
                "signal_type": "BUY_WATCH",
                "reason_codes": ["near_intraday_low"],
            },
        ]
    )
    monkeypatch.setattr(
        t0,
        "get_db_collections",
        lambda: {
            "intraday_t0_signal_evaluations": evals,
            "intraday_t0_signals": signals,
        },
    )
    monkeypatch.setattr(t0, "_INDEXES_READY", True)
    result = t0.performance_summary("u1", trade_date=None)
    assert result["success"] is True
    assert result["raw_count"] == 3
    sell_row = next(r for r in result["data"] if r["signal_type"] == "SELL_WATCH")
    assert sell_row["horizon"] == "15m"
    assert sell_row["count"] == 2
    assert sell_row["win_rate"] == 1.0
    symbol_row = next(r for r in result["by_symbol"] if r["symbol"] == "300750.SZ")
    assert symbol_row["count"] == 2
    reason_row = next(r for r in result["by_reason"] if r["reason_code"] == "near_intraday_high")
    assert reason_row["signal_type"] == "SELL_WATCH"


@pytest.mark.unit
def test_export_signals_csv(monkeypatch):
    class _Signals(FakeCollection):
        def find(self, query=None, projection=None):
            rows = super().find(query, projection)
            return rows.sort([("snapshot_time", -1)])

    signals = _Signals(
        [
            {
                "user_id": "u1",
                "trade_date": "20260820",
                "snapshot_time": "202608201030",
                "symbol": "300750.SZ",
                "name": "CATL",
                "signal_type": "SELL_WATCH",
                "signal_score": 72.5,
                "price": 100.0,
                "position_source": "mock",
                "reason_codes": ["near_intraday_high"],
                "features": {"market_yang_spectrum": 0.4, "bid_ask_imbalance": -0.2, "intraday_position": 0.8},
            }
        ]
    )
    monkeypatch.setattr(
        t0,
        "get_db_collections",
        lambda: {"intraday_t0_signals": signals},
    )
    monkeypatch.setattr(t0, "_INDEXES_READY", True)
    csv_text = t0.export_signals_csv("u1", trade_date="20260820")
    assert "300750.SZ" in csv_text
    assert "near_intraday_high" in csv_text
    assert csv_text.splitlines()[0].startswith("trade_date,")


@pytest.mark.unit
def test_latest_market_spectrum_pins_ma5_when_newer_ma30_exists(monkeypatch):
    """Realtime snapshots carry both ma_period=5 and ma_period=30 docs per
    minute; without a pin find_one may return the MA30 doc whose yang_spectrum
    differs wildly (0.37 vs 0.73 on 2026-09-18)."""
    rows = [
        {
            "snapshot_trade_date": "20260918",
            "snapshot_time": "202609181459",
            "ma_period": 30,
            "yang_spectrum": 0.3734,
        },
        {
            "snapshot_trade_date": "20260918",
            "snapshot_time": "202609181459",
            "ma_period": 5,
            "yang_spectrum": 0.7296,
        },
        # Legacy doc without ma_period must still be readable (treated as MA5).
        {
            "snapshot_trade_date": "20260917",
            "snapshot_time": "202609171459",
            "yang_spectrum": 0.55,
        },
    ]
    fake_get_data_db = lambda: {"market_spectrum_realtime_daily": FakeCollection(rows)}  # noqa: E731
    monkeypatch.setattr(t0, "get_data_db", fake_get_data_db)
    today = t0.latest_market_spectrum("2026-09-18")
    assert today["ma_period"] == 5
    assert today["yang_spectrum"] == pytest.approx(0.7296)
    legacy = t0.latest_market_spectrum("2026-09-17")
    assert legacy["yang_spectrum"] == pytest.approx(0.55)


# 生产 securities_accounts 的费率：万1 双边 + 最低 5 元 + 卖方印花税 0.05%
FEE_MODEL = {
    "buy_commission_rate": 0.0001,
    "sell_commission_rate": 0.0001,
    "min_commission": 5.0,
    "stamp_tax_rate": 0.0005,
    "transfer_fee_rate": 0.0,
}


@pytest.mark.unit
def test_round_trip_cost_pct_includes_min_commission_step():
    # ≥5 万：佣金不被最低值顶掉，往返 0.07%
    assert t0.round_trip_cost_pct(FEE_MODEL, 100_000) == pytest.approx(0.07)
    # 1 万：最低佣金 5 元咬住，成本升到 0.15% —— 成本不是定值
    assert t0.round_trip_cost_pct(FEE_MODEL, 10_000) == pytest.approx(0.15)
    # 没有费率模型时不假装有成本
    assert t0.round_trip_cost_pct({}, 100_000) == 0.0


@pytest.mark.unit
def test_net_return_pct_signs_cost_by_direction():
    """成本要按方向折：BUY 直接扣，SELL 是"要多跌一个成本"。

    一律写 return - cost 会让 SELL_WATCH 净胜率虚高（实测 59% → 78%）。
    """
    assert t0.net_return_pct(0.03, 0.07, "BUY_WATCH") == pytest.approx(-0.04)
    # 跌了 0.30%：超过成本，先卖后买仍然赚
    assert t0.net_return_pct(-0.30, 0.07, "SELL_WATCH") == pytest.approx(-0.23)
    # 只跌 0.02%：毛算赢，但没跌够成本 → 实际亏
    assert t0.net_return_pct(-0.02, 0.07, "SELL_WATCH") == pytest.approx(0.05)
    # 无方向（HOLD）：保守按成本是拖累
    assert t0.net_return_pct(0.10, 0.07, "HOLD") == pytest.approx(0.03)


@pytest.mark.unit
def test_reference_notional_uses_median_position_value(monkeypatch):
    monkeypatch.setattr(
        t0,
        "get_user_positions",
        lambda uid, trade_date=None: (
            "auto",
            [
                {"quantity": 100, "current_price": 10.0},  # 1,000
                {"quantity": 1000, "current_price": 10.0},  # 10,000
                {"quantity": 10000, "current_price": 10.0},  # 100,000
            ],
        ),
    )
    assert t0._reference_notional("u1", "20260820") == pytest.approx(10_000)


@pytest.mark.unit
def test_reference_notional_falls_back_without_positions(monkeypatch):
    monkeypatch.setattr(t0, "get_user_positions", lambda uid, trade_date=None: ("auto", []))
    assert t0._reference_notional("u1", "20260820") == t0.REFERENCE_NOTIONAL_FALLBACK


def _patch_performance(monkeypatch, eval_rows, signal_rows):
    monkeypatch.setattr(
        t0,
        "get_db_collections",
        lambda: {
            "intraday_t0_signal_evaluations": FakeCollection(eval_rows),
            "intraday_t0_signals": FakeCollection(signal_rows),
        },
    )
    monkeypatch.setattr(t0, "_INDEXES_READY", True)


@pytest.mark.unit
def test_performance_summary_omits_win_rate_for_hold(monkeypatch):
    """HOLD 无方向：`v<0` 算赢会把“跌了”读成高胜率，故该行不带 win_rate。

    删键而非置 None —— 前端 percent() 走 Number(v)，Number(null)=0 会渲染成
    “0.0%”，键缺失才是 “-”。
    """
    _patch_performance(
        monkeypatch,
        eval_rows=[
            {
                "user_id": "u1",
                "signal_id": "s1",
                "symbol": "300750.SZ",
                "signal_type": "HOLD",
                "horizon": "5m",
                "return_pct": -1.0,
                "net_return_pct": -1.07,
            },
            {
                "user_id": "u1",
                "signal_id": "s2",
                "symbol": "600000.SH",
                "signal_type": "BUY_WATCH",
                "horizon": "5m",
                "return_pct": 0.2,
                "net_return_pct": 0.13,
            },
        ],
        signal_rows=[
            {"_id": "s1", "user_id": "u1", "signal_type": "HOLD", "reason_codes": []},
            {"_id": "s2", "user_id": "u1", "signal_type": "BUY_WATCH", "reason_codes": []},
        ],
    )
    result = t0.performance_summary("u1", trade_date=None)
    hold_row = next(r for r in result["data"] if r["signal_type"] == "HOLD")
    assert "win_rate" not in hold_row
    assert "net_win_rate" not in hold_row
    assert hold_row["count"] == 1
    assert hold_row["avg_return_pct"] == pytest.approx(-1.0)
    buy_row = next(r for r in result["data"] if r["signal_type"] == "BUY_WATCH")
    assert buy_row["win_rate"] == 1.0
    assert buy_row["net_win_rate"] == 1.0


@pytest.mark.unit
def test_net_win_rate_exposes_cost_that_gross_win_rate_hides(monkeypatch):
    """毛 +0.03% 算赢，扣掉往返 0.07% 后净 -0.04% —— 实际是亏的。

    这正是 Phase 1 验收只看毛 win_rate 会读错的地方。
    """
    _patch_performance(
        monkeypatch,
        eval_rows=[
            {
                "user_id": "u1",
                "signal_id": "s1",
                "symbol": "600000.SH",
                "signal_type": "BUY_WATCH",
                "horizon": "15m",
                "return_pct": 0.03,
                "net_return_pct": -0.04,
            }
        ],
        signal_rows=[
            {"_id": "s1", "user_id": "u1", "signal_type": "BUY_WATCH", "reason_codes": []}
        ],
    )
    row = t0.performance_summary("u1", trade_date=None)["data"][0]
    assert row["win_rate"] == 1.0
    assert row["net_win_rate"] == 0.0
    assert row["avg_net_return_pct"] == pytest.approx(-0.04)


@pytest.mark.unit
def test_sell_watch_cost_makes_net_win_harder(monkeypatch):
    """SELL_WATCH 的对称面：跌了 0.02% 毛算赢，但没跌够 0.07% 成本 → 净是亏的。

    net_return_pct 存的是价格变动空间（-0.02 + 0.07 = +0.05），
    故 win 判据（<0）此时为假。
    """
    _patch_performance(
        monkeypatch,
        eval_rows=[
            {
                "user_id": "u1",
                "signal_id": "s1",
                "symbol": "300750.SZ",
                "signal_type": "SELL_WATCH",
                "horizon": "15m",
                "return_pct": -0.02,
                "net_return_pct": 0.05,
            }
        ],
        signal_rows=[
            {"_id": "s1", "user_id": "u1", "signal_type": "SELL_WATCH", "reason_codes": []}
        ],
    )
    row = t0.performance_summary("u1", trade_date=None)["data"][0]
    assert row["win_rate"] == 1.0
    assert row["net_win_rate"] == 0.0


@pytest.mark.unit
def test_evaluate_writes_cost_and_net_return(monkeypatch):
    """评估落库时带上成本假设，验收才能读净口径。"""
    signals = FakeCollection(
        [
            {
                "_id": "s1",
                "user_id": "u1",
                "symbol": "300750.SZ",
                "trade_date": "20260820",
                "snapshot_time": "202608201030",
                "price": 100.0,
                "signal_type": "BUY_WATCH",
            }
        ]
    )
    evals = FakeCollection([])
    monkeypatch.setattr(
        t0,
        "get_db_collections",
        lambda: {
            "intraday_t0_signals": signals,
            "intraday_t0_signal_evaluations": evals,
        },
    )
    monkeypatch.setattr(t0, "_INDEXES_READY", True)
    monkeypatch.setattr(
        t0,
        "_snapshot_after",
        lambda uid, symbol, target: {"price": 101.0, "snapshot_time": "202608201035"},
    )
    monkeypatch.setattr(t0, "latest_daily_record", lambda symbol, trade_date=None: None)
    monkeypatch.setattr(t0, "_next_daily", lambda symbol, trade_date: None)
    monkeypatch.setattr(
        t0,
        "get_user_positions",
        lambda uid, trade_date=None: ("auto", [{"quantity": 10000, "current_price": 10.0}]),
    )
    monkeypatch.setattr("services.intraday_t0_trades._t0_fee_model", lambda: FEE_MODEL)

    result = t0.evaluate_signals_for_user("u1", "20260820")
    assert result["ref_notional"] == pytest.approx(100_000)
    assert result["cost_pct"] == pytest.approx(0.07)
    assert result["evaluations_written"] == 4  # 四个分钟级 horizon；close/t1 无数据
    doc = evals.upserts[0][1]["$set"]
    assert doc["return_pct"] == pytest.approx(1.0)
    assert doc["cost_pct"] == pytest.approx(0.07)
    assert doc["net_return_pct"] == pytest.approx(0.93)
    assert doc["ref_notional"] == pytest.approx(100_000)
