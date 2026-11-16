import pytest

from services.intraday_line import SLOT_COUNT, build_intraday_line, time_slots


def test_time_slots_cover_both_sessions_without_lunch_gap():
    slots = time_slots()
    assert SLOT_COUNT == 241
    assert len(slots) == 241
    assert slots[0] == "09:30"
    assert slots[120] == "11:30"
    assert slots[121] == "13:01"  # QMT's first afternoon bar; there is no 13:00 bar
    assert slots[-1] == "15:00"


def test_vwap_price_and_pct_from_1m_bars():
    rows = [
        {"trade_date": "202611160930", "close": 19.92, "volume": 278, "amount": 553776},
        {"trade_date": "202611160931", "close": 19.70, "volume": 3656, "amount": 7223477},
    ]
    line = build_intraday_line(rows, pre_close=20.11)
    bars = line["bars"]
    assert bars[0]["price"] == pytest.approx(19.92)
    assert bars[0]["avg"] == pytest.approx(19.92)
    assert bars[1]["price"] == pytest.approx(19.70)
    # Σamount / Σ(volume × 100) = 7,777,253 / 393,400
    assert bars[1]["avg"] == pytest.approx(7777253 / 393400, abs=5e-4)  # rounded to 3 dp
    assert bars[1]["pct"] == pytest.approx((19.70 - 20.11) / 20.11 * 100, abs=0.01)
    # minutes with no bar stay null so a partial/halted session shows a gap
    assert bars[2]["price"] is None
    assert bars[2]["avg"] is None
    summary = line["summary"]
    assert summary["last"] == pytest.approx(19.70)
    assert summary["high"] == pytest.approx(19.92)
    assert summary["low"] == pytest.approx(19.70)
    assert summary["volume"] == pytest.approx(278 + 3656)
    assert summary["amount"] == pytest.approx(553776 + 7223477)
    assert summary["count"] == 2


def test_afternoon_and_close_slots_are_kept():
    rows = [
        {"trade_date": "202611161301", "close": 18.94, "volume": 1653, "amount": 3124128},
        {"trade_date": "202611161500", "close": 18.54, "volume": 1855, "amount": 3439170},
    ]
    line = build_intraday_line(rows, pre_close=20.11)
    by_time = {bar["time"]: bar for bar in line["bars"]}
    assert by_time["13:01"]["price"] == pytest.approx(18.94)
    assert by_time["15:00"]["price"] == pytest.approx(18.54)
    assert by_time["13:01"]["volume"] == pytest.approx(1653)


def test_no_pre_close_keeps_pct_null():
    rows = [{"trade_date": "202611160930", "close": 10.0, "volume": 100, "amount": 100000}]
    line = build_intraday_line(rows, pre_close=None)
    assert line["bars"][0]["pct"] is None
    assert line["bars"][0]["avg"] == pytest.approx(10.0)


def test_empty_session_returns_full_grid():
    line = build_intraday_line([], pre_close=10.0)
    assert len(line["bars"]) == 241
    assert all(bar["price"] is None for bar in line["bars"])
    assert line["summary"]["count"] == 0
    assert line["summary"]["volume"] == 0
