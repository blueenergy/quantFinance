from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from auth import get_current_active_user
from main import app
from routers import trade_execution as te
from services.intraday_kline import (
    build_intraday_gs_bars,
    compact_day,
    intraday_query_windows,
    resolve_1m_query_day,
)


def _hhmm_range(start: int, end: int):
    hour, minute = divmod(start, 100)
    end_hour, end_minute = divmod(end, 100)
    current = hour * 60 + minute
    last = end_hour * 60 + end_minute
    while current <= last:
        yield (current // 60) * 100 + (current % 60)
        current += 1


def _session_hhmm():
    times = list(_hhmm_range(931, 1130))
    times.extend(_hhmm_range(1301, 1500))
    return times


def day_1m(ymd: str, symbol: str = "000001.SZ"):
    rows = []
    for idx, hhmm in enumerate(_session_hhmm()):
        close = 10.0 + idx * 0.01
        rows.append(
            {
                "symbol": symbol,
                "trade_date": f"{ymd}{hhmm:04d}",
                "open": close,
                "high": close + 0.05,
                "low": close - 0.05,
                "close": close,
                "volume": 100,
            }
        )
    return rows


class _Cursor(list):
    def sort(self, field, direction=None):
        reverse = direction == -1
        return _Cursor(sorted(self, key=lambda row: str(row.get(field) or ""), reverse=reverse))

    def limit(self, n):
        return _Cursor(list(self)[:n])


class _MinuteCollection:
    def __init__(self, docs):
        self.docs = list(docs)
        self.last_query = None
        self.last_limit = None

    def find(self, query):
        self.last_query = query
        matched = [doc for doc in self.docs if self._matches(doc, query)]
        return _Cursor(matched)

    def find_one(self, query, projection=None, sort=None):
        matched = [doc for doc in self.docs if self._matches(doc, query)]
        if sort:
            field, direction = sort[0]
            reverse = direction == -1
            matched.sort(key=lambda row: str(row.get(field) or ""), reverse=reverse)
        return matched[0] if matched else None

    def _matches(self, doc, query):
        if "$or" in query:
            rest = {key: value for key, value in query.items() if key != "$or"}
            if rest and not self._matches(doc, rest):
                return False
            return any(self._matches(doc, clause) for clause in query["$or"])
        for key, expected in query.items():
            actual = doc.get(key)
            if isinstance(expected, dict):
                text = str(actual)
                if "$gte" in expected and text < str(expected["$gte"]):
                    return False
                if "$lte" in expected and text > str(expected["$lte"]):
                    return False
                continue
            if actual != expected:
                return False
        return True


@pytest.fixture
def authed_client():
    app.dependency_overrides[get_current_active_user] = lambda: {"id": "u1", "is_active": True}
    yield TestClient(app)
    app.dependency_overrides.pop(get_current_active_user, None)


@pytest.mark.unit
def test_intraday_query_windows_adds_warmup_before_visible_start():
    windows = intraday_query_windows(
        "60m",
        "20260901",
        "20260907",
        today=datetime(2026, 9, 7),
    )
    assert windows.visible_start == "20260901"
    assert windows.visible_end == "20260907"
    assert windows.source_end == "20260907"
    assert windows.source_start == "20260718"


@pytest.mark.unit
def test_build_intraday_gs_bars_stamps_formula_and_keeps_closed_30m():
    rows = day_1m("20260907")
    bars = build_intraday_gs_bars(rows, "30m", start_date="20260907", end_date="20260907")
    assert len(bars) == 8
    assert bars[0]["trade_date"] == "202609071000"
    assert bars[-1]["trade_date"] == "202609071500"
    assert bars[-1]["is_partial"] is False
    assert bars[0]["gs_formula_id"] == "decision_gs_v1"
    assert bars[0]["mj20"] is not None
    assert all(row["gs_signal"] is None for row in bars)


@pytest.mark.unit
def test_get_minute_bars_1m_unchanged(monkeypatch, authed_client):
    coll = _MinuteCollection(day_1m("20260907"))
    monkeypatch.setattr(te, "get_data_db", lambda: {"minute_bars": coll})
    resp = authed_client.get(
        "/api/minute-bars/",
        params={"symbol": "000001.SZ", "start_date": "20260907", "end_date": "20260907", "limit": 1000},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["tf"] == "1m"
    assert body["count"] == 240
    assert "gs_formula_id" not in body["data"][0]
    assert body["data"][0]["trade_date"] == "202609070931"
    assert body["day"] == "20260907"


@pytest.mark.unit
def test_resolve_1m_query_day_pins_undated_requests_to_latest_session():
    assert resolve_1m_query_day("20260907", "20260907", "202609081031") is None
    assert resolve_1m_query_day(None, None, "202609081031") == "20260908"
    assert resolve_1m_query_day(None, None, None) is None
    assert compact_day(202609081031) == "20260908"


@pytest.mark.unit
def test_get_minute_bars_1m_without_dates_uses_latest_day(monkeypatch, authed_client):
    coll = _MinuteCollection(day_1m("20260906") + day_1m("20260908"))
    monkeypatch.setattr(te, "get_data_db", lambda: {"minute_bars": coll})
    resp = authed_client.get(
        "/api/minute-bars/",
        params={"symbol": "000001.SZ", "tf": "1m", "limit": 1000},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["tf"] == "1m"
    assert body["day"] == "20260908"
    assert body["count"] == 240
    assert {row["trade_date"][:8] for row in body["data"]} == {"20260908"}
    assert coll.last_query["trade_date"]["$gte"] == "202609080000"
    assert coll.last_query["trade_date"]["$lte"] == "202609082359"


@pytest.mark.unit
def test_get_minute_bars_30m_resamples_and_attaches_gs(monkeypatch, authed_client):
    coll = _MinuteCollection(day_1m("20260906") + day_1m("20260907"))
    monkeypatch.setattr(te, "get_data_db", lambda: {"minute_bars": coll})
    resp = authed_client.get(
        "/api/minute-bars/",
        params={
            "symbol": "000001.SZ",
            "start_date": "20260907",
            "end_date": "20260907",
            "tf": "30m",
            "limit": 200,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["tf"] == "30m"
    assert body["count"] == 8
    assert body["data"][0]["gs_formula_id"] == "decision_gs_v1"
    assert {row["trade_date"][:8] for row in body["data"]} == {"20260907"}
    gte = coll.last_query["trade_date"]["$gte"]
    assert gte < "202609070000"


@pytest.mark.unit
def test_get_minute_bars_rejects_unknown_tf(authed_client):
    resp = authed_client.get(
        "/api/minute-bars/",
        params={"symbol": "000001.SZ", "tf": "15m"},
    )
    assert resp.status_code == 400
