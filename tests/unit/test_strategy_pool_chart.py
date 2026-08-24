"""Unit tests for strategy-pool chart context helpers."""

import pytest

from services.strategy_pool_chart import (
    build_chart_context,
    build_markers,
    forward_returns,
    kline_window,
    pair_rounds,
    select_recent_rounds,
    symbol_candidates,
    yyyymmdd,
)


def test_symbol_candidates_adds_exchange_suffix():
    assert symbol_candidates("000001")[0] == "000001"
    assert "000001.SZ" in symbol_candidates("000001")
    assert symbol_candidates("000001.SZ")[0] == "000001.SZ"
    assert "000001" in symbol_candidates("000001.SZ")


def test_pair_rounds_closes_buy_sell_and_leaves_open_buy():
    trades = [
        {"date": "20260105", "action": "buy", "price": 10.0, "datetime": "a"},
        {"date": "20260106", "action": "sell", "price": 11.0, "datetime": "b", "pnl": 1.0},
        {"date": "20260109", "action": "buy", "price": 12.0, "datetime": "c"},
    ]
    rounds = pair_rounds(trades)
    assert len(rounds) == 2
    assert rounds[0]["sell"]["price"] == 11.0
    assert rounds[1]["sell"] is None
    assert rounds[1]["buy"]["date"] == "20260109"


def test_select_recent_rounds_keeps_signal_and_prior_history():
    rounds = [
        {"buy": {"date": f"2026010{i}"}, "sell": None}
        for i in range(1, 6)
    ]
    chosen = select_recent_rounds(rounds, "20260104", max_rounds=2)
    assert [r["buy"]["date"] for r in chosen] == ["20260103", "20260104"]


def test_forward_returns_uses_signal_close_and_marks_missing_horizon():
    records = [
        {"trade_date": "20260108", "close": 10.0},
        {"trade_date": "20260109", "close": 10.0},
        {"trade_date": "20260110", "close": 11.0},
    ]
    result = forward_returns(records, "20260109", horizons=(1, 5))
    assert result["entry_date"] == "20260109"
    assert result["horizons"][0]["available"] is True
    assert result["horizons"][0]["return"] == pytest.approx(0.1)
    assert result["horizons"][1]["available"] is False


def test_build_markers_highlights_signal_buy_and_uses_bar_high_low():
    rounds = [
        {
            "buy": {"date": "20260109", "price": 10.2, "pnl": 0},
            "sell": {"date": "20260110", "price": 11.0, "pnl": 80},
        }
    ]
    records = [
        {"trade_date": "20260109", "high": 10.5, "low": 9.8, "close": 10.1},
        {"trade_date": "20260110", "high": 11.2, "low": 10.8, "close": 11.0},
    ]
    markers = build_markers(rounds, records, "20260109")
    assert markers[0]["kind"] == "buy"
    assert markers[0]["highlighted"] is True
    assert markers[0]["low"] == 9.8
    assert markers[1]["kind"] == "sell"
    assert markers[1]["highlighted"] is False
    assert markers[1]["label"] == "卖"


def test_kline_window_does_not_look_past_as_of():
    start, end = kline_window(
        "20260109",
        lookback_days=10,
        lookahead_days=90,
        as_of="20260112",
    )
    assert start == "20251230"
    assert end == "20260112"


def test_build_chart_context_payload_shape():
    trades = [
        {
            "date": "20260108",
            "datetime": "2026-01-08 10:00:00",
            "action": "BUY",
            "price": 9.5,
            "pnl": 0,
        },
        {
            "date": "20260109",
            "datetime": "2026-01-09 10:00:00",
            "action": "buy",
            "price": 10.0,
            "pnl": 0,
        },
    ]
    records = [
        {"trade_date": "20260108", "open": 9, "high": 10, "low": 9, "close": 9.5, "volume": 1},
        {"trade_date": "20260109", "open": 10, "high": 11, "low": 9.8, "close": 10.2, "volume": 2},
        {"trade_date": "20260110", "open": 10.2, "high": 11, "low": 10, "close": 10.5, "volume": 2},
    ]
    payload = build_chart_context(
        trades=trades,
        records=records,
        signal_date="20260109",
        symbol="000001.SZ",
        strategy="hidden_dragon",
        preset="dragon_default",
        kline_start="20251201",
        kline_end="20260112",
    )
    assert payload["success"] is True
    assert payload["signal_date"] == "20260109"
    assert "事后验证" in payload["disclaimer"]
    assert payload["kline"]["count"] == 3
    assert any(m["highlighted"] for m in payload["markers"])
    assert payload["forward_returns"]["horizons"][0]["available"] is True
    assert yyyymmdd("2026-01-09") == "20260109"
