"""Unit tests for services.national_team_alert (no Mongo, no network)."""

from services import national_team_alert as nta


class FakeState:
    """Minimal stand-in for a pymongo collection ($set + upsert only)."""

    def __init__(self):
        self.docs = {}

    def find_one(self, query, *args, **kwargs):
        return self.docs.get(query.get("_id"))

    def update_one(self, query, update, upsert=False):
        key = query.get("_id")
        doc = dict(self.docs.get(key) or {})
        doc.update(update.get("$set") or {})
        self.docs[key] = doc


def _snapshot(level="suspect", as_of="1030", tail_ratio=None, trade_date="20261104"):
    tail = {"share": None, "baseline": None, "ratio": None}
    if tail_ratio is not None:
        tail = {"share": 0.46, "baseline": 0.33, "ratio": tail_ratio}
    return {
        "trade_date": trade_date,
        "as_of": as_of,
        "level": level,
        "level_label": nta.LEVEL_LABELS[level],
        "breadth": {"ratio2": 3, "ratio25": 2, "pool_size": 16},
        "pool": {
            "amount_yuan": 12.3e9,
            "baseline_yuan": 8.8e9,
            "ratio": 1.40,
        },
        "tail": tail,
        "share": {
            "avg_chg_1d": 1.2,
            "avg_chg_5d": 3.4,
            "date": "20261103",
            "pool_flow": 1.5,
            "rotation": 1.1,
            "direction": "inflow",
            "direction_label": "净申购",
        },
        "first_signal": {"watch": "1000", "suspect": "1030"},
        "index_moves": [
            {"ts_code": "000300.SH", "name": "沪深300", "pct_chg": -0.20},
            {"ts_code": "000905.SH", "name": "中证500", "pct_chg": None},
        ],
        "etfs": [
            {"ts_code": "510310.SH", "name": "易方达沪深300ETF", "ratio": 3.85, "signal": "suspect", "signal_label": "疑似"},
            {"ts_code": "510300.SH", "name": "华泰柏瑞沪深300ETF", "ratio": 2.16, "signal": "watch", "signal_label": "观察"},
            {"ts_code": "510050.SH", "name": "华夏上证50ETF", "ratio": 1.19, "signal": "calm", "signal_label": "平静"},
        ],
    }


def _collector(ok=True):
    calls = []

    def send(title, lines):
        calls.append((title, lines))
        return ok

    return calls, send


def test_build_alert_includes_evidence_and_close_wording():
    title, lines = nta.build_alert(_snapshot(level="high", tail_ratio=2.16), close=True)
    text = "\n".join(lines)
    assert "收盘确认" in title
    assert "高置信" in title
    assert "尾盘集中度" in text
    assert "46.0% vs 20 日基线 33.0%（2.16x）" in text
    assert "份额确认(T+1)" in text
    assert "量比前三" in text
    assert "510310.SH" in text
    assert "不构成确认" in text
    assert "中证500" not in text  # pct_chg is None


def test_build_alert_before_tail_window():
    _, lines = nta.build_alert(_snapshot(level="watch", as_of="1124"), close=False)
    assert any("未到 14:30" in line for line in lines)


def test_build_alert_marks_redemption_direction():
    snapshot = _snapshot(level="suspect")
    snapshot["share"].update(
        {
            "pool_flow": -3.26,
            "rotation": 1.05,
            "direction": "outflow",
            "direction_label": "净赎回",
        }
    )
    title, lines = nta.build_alert(snapshot)
    text = "\n".join(lines)
    assert "净赎回" in title
    assert "池子净申赎(AUM加权) -3.26%" in text
    assert "相消比 1.05" in text


def test_watch_is_below_threshold():
    calls, send = _collector()
    state = FakeState()
    result = nta.evaluate_snapshot(_snapshot(level="watch"), state, sender=send)
    assert result["sent"] is False
    assert calls == []
    assert state.docs == {}


def test_suspect_sends_once_then_dedupes():
    calls, send = _collector()
    state = FakeState()
    first = nta.evaluate_snapshot(_snapshot(level="suspect"), state, sender=send)
    second = nta.evaluate_snapshot(_snapshot(level="suspect", as_of="1100"), state, sender=send)
    assert first["sent"] is True
    assert second["sent"] is False
    assert len(calls) == 1
    assert state.docs["20261104"]["sent_level"] == "suspect"


def test_upgrade_sends_again():
    calls, send = _collector()
    state = FakeState()
    nta.evaluate_snapshot(_snapshot(level="suspect"), state, sender=send)
    result = nta.evaluate_snapshot(_snapshot(level="high", as_of="1455"), state, sender=send)
    assert result["sent"] is True
    assert len(calls) == 2
    assert state.docs["20261104"]["sent_level"] == "high"


def test_close_does_not_repeat_intraday_level():
    calls, send = _collector()
    state = FakeState()
    nta.evaluate_snapshot(_snapshot(level="suspect"), state, sender=send)
    result = nta.evaluate_snapshot(_snapshot(level="suspect", as_of="1500"), state, close=True, sender=send)
    assert result["sent"] is False
    assert len(calls) == 1
    assert state.docs["20261104"]["close_evaluated"] is True


def test_close_sends_new_higher_level():
    calls, send = _collector()
    state = FakeState()
    nta.evaluate_snapshot(_snapshot(level="suspect"), state, sender=send)
    result = nta.evaluate_snapshot(
        _snapshot(level="high", as_of="1500", tail_ratio=2.16),
        state,
        close=True,
        sender=send,
    )
    assert result["sent"] is True
    assert len(calls) == 2
    assert "收盘确认" in calls[-1][0]
    assert state.docs["20261104"]["close_evaluated"] is True


def test_close_below_threshold_marks_evaluated_without_sending():
    calls, send = _collector()
    state = FakeState()
    result = nta.evaluate_snapshot(_snapshot(level="watch", as_of="1500"), state, close=True, sender=send)
    assert result["sent"] is False
    assert calls == []
    assert state.docs["20261104"]["close_evaluated"] is True


def test_send_failure_keeps_state_for_retry():
    calls, failing = _collector(ok=False)
    state = FakeState()
    result = nta.evaluate_snapshot(_snapshot(level="suspect"), state, sender=failing)
    assert result["sent"] is False
    assert result["reason"] == "send failed"
    assert state.docs == {}

    ok_calls, ok_send = _collector(ok=True)
    retry = nta.evaluate_snapshot(_snapshot(level="suspect"), state, sender=ok_send)
    assert retry["sent"] is True
    assert len(ok_calls) == 1
