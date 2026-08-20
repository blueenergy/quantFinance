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
    features = {
        "intraday_position": 0.85,
        "pnl_pct": 2.0,
        "bid_ask_imbalance": -0.25,
        "sector_positive_ratio": 0.30,
        "market_yang_spectrum": 0.35,
        "time_of_day": "14:00-14:30",
        "pct_chg": -0.5,
        "avg_trade_size_yuan": 25000,
    }
    signal_type, score, reasons = t0.classify_signal({}, features, "202608201420")
    assert signal_type == "SELL_WATCH"
    assert score >= 58
    assert "near_intraday_high" in reasons
    assert "ask_pressure_stronger" in reasons


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
            {"user_id": "u1", "signal_type": "SELL_WATCH", "horizon": "15m", "return_pct": -0.5},
            {"user_id": "u1", "signal_type": "SELL_WATCH", "horizon": "15m", "return_pct": -0.2},
            {"user_id": "u1", "signal_type": "BUY_WATCH", "horizon": "15m", "return_pct": 0.3},
        ]
    )
    monkeypatch.setattr(
        t0,
        "get_db_collections",
        lambda: {"intraday_t0_signal_evaluations": evals},
    )
    monkeypatch.setattr(t0, "_INDEXES_READY", True)
    result = t0.performance_summary("u1", trade_date=None)
    assert result["success"] is True
    assert result["raw_count"] == 3
    sell_row = next(r for r in result["data"] if r["signal_type"] == "SELL_WATCH")
    assert sell_row["horizon"] == "15m"
    assert sell_row["count"] == 2
    assert sell_row["win_rate"] == 1.0
    assert sell_row["avg_return_pct"] == pytest.approx(-0.35)
