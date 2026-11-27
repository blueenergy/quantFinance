from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from auth import get_current_active_user
from routers import market_stats
from services.market_turnover import (
    attach_ma_and_ratio,
    build_intraday_profile,
    project_full_day,
)


class _Cursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, key, direction=1):
        self._docs.sort(key=lambda d: d.get(key, ""), reverse=(direction == -1))
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    def __iter__(self):
        return iter(self._docs)


class _Coll:
    def __init__(self, docs):
        self.docs = list(docs)

    @staticmethod
    def _match(doc, query):
        for key, cond in query.items():
            value = doc.get(key)
            if isinstance(cond, dict):
                if "$lt" in cond and not (str(value) < str(cond["$lt"])):
                    return False
                if "$gte" in cond and not (str(value) >= str(cond["$gte"])):
                    return False
                if "$in" in cond and value not in cond["$in"]:
                    return False
                if "$nin" in cond and value in cond["$nin"]:
                    return False
            elif value != cond:
                return False
        return True

    def find(self, query=None, _projection=None):
        return _Cursor([dict(doc) for doc in self.docs if self._match(doc, query or {})])

    def distinct(self, field):
        return sorted({doc.get(field) for doc in self.docs if doc.get(field) is not None})


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(market_stats.router, prefix="/api")
    app.dependency_overrides[get_current_active_user] = lambda: {"id": "u1", "is_active": True}
    return TestClient(app)


def _daily(trade_date, total, **extra):
    doc = {
        "trade_date": trade_date,
        "sh": total * 0.3,
        "sz": total * 0.25,
        "cx": total * 0.2,
        "star": total * 0.15,
        "bse": total * 0.01,
        "stock_total": total,
        "stock_count": 5500,
        "etf_equity": total * 0.16,
        "etf_bond": total * 0.1,
        "etf_money": total * 0.02,
        "etf_total": total * 0.28,
        "etf_count": 2200,
        "etf_available": True,
    }
    doc.update(extra)
    return doc


def test_attach_ma_uses_only_previous_rows():
    rows = [{"stock_total": 100.0} for _ in range(25)]
    rows[20]["stock_total"] = 200.0
    attach_ma_and_ratio(rows, ["stock_total"], window=20)
    assert rows[19]["stock_total_ma20"] is None
    # rows[20] is compared against rows[0:20], none of which include itself
    assert rows[20]["stock_total_ma20"] == pytest.approx(100.0)
    assert rows[20]["stock_total_ratio"] == pytest.approx(2.0)


def test_attach_ma_skips_gaps():
    rows = [{"stock_total": 100.0} for _ in range(20)] + [{"stock_total": None}, {"stock_total": 100.0}]
    attach_ma_and_ratio(rows, ["stock_total"], window=20)
    assert rows[20]["stock_total_ratio"] is None
    assert rows[21]["stock_total_ratio"] is None  # window still contains a gap


def test_build_intraday_profile_and_projection():
    def day(value):
        return [
            {"t": "0930", "total": value * 0.25},
            {"t": "1130", "total": value * 0.60},
            {"t": "1430", "total": value * 0.90},
            {"t": "1500", "total": value},
        ]

    series = {f"2026-11-2{i}": day(1000.0) for i in range(1, 6)}
    profile = build_intraday_profile(series)
    by_time = {entry["t"]: entry for entry in profile}
    assert by_time["1130"]["p50"] == pytest.approx(0.60)
    assert by_time["1500"]["p50"] == pytest.approx(1.0)
    assert by_time["1130"]["n"] == 5
    assert project_full_day(600.0, 0.60) == pytest.approx(1000.0)
    assert project_full_day(None, 0.6) is None
    assert project_full_day(600.0, 0.0) is None


def test_turnover_endpoint_adds_ratio(monkeypatch):
    docs = [_daily(f"2026-10-{day:02d}", 100.0) for day in range(1, 26)]
    docs.append(_daily("2026-10-26", 150.0))
    monkeypatch.setattr(
        market_stats, "get_data_db", lambda: {"market_turnover_daily": _Coll(docs)}
    )
    response = _client().get("/api/market-stats/turnover", params={"days": 10, "window": 20})
    assert response.status_code == 200
    payload = response.json()
    assert payload["latest"]["trade_date"] == "2026-10-26"
    assert payload["latest"]["stock_total_ratio"] == pytest.approx(1.5)
    assert len(payload["rows"]) == 10
    assert [b["key"] for b in payload["boards"]] == ["sh", "sz", "cx", "star", "bse"]


def test_turnover_intraday_projects_from_clock_profile(monkeypatch):
    def day(value):
        return [
            {"trade_date": f"2026-11-2{i}", "snapshot_time": f"2026-11-2{i}0930", "total": value * 0.25},
            {"trade_date": f"2026-11-2{i}", "snapshot_time": f"2026-11-2{i}1130", "total": value * 0.60},
            {"trade_date": f"2026-11-2{i}", "snapshot_time": f"2026-11-2{i}1430", "total": value * 0.90},
            {"trade_date": f"2026-11-2{i}", "snapshot_time": f"2026-11-2{i}1500", "total": value},
        ]

    intraday = []
    for i in range(1, 6):
        intraday.extend(day(1000.0))
    intraday.append(
        {
            "trade_date": "2026-11-27",
            "snapshot_time": "2026-11-271130",
            "total": 60.0,
            "coverage": "boards",
            "sh": 20.0,
            "sz": 15.0,
            "cx": 12.0,
            "star": 10.0,
            "bse": 1.0,
        }
    )
    daily = [_daily(f"2026-11-{day:02d}", 100.0) for day in range(1, 27)]
    monkeypatch.setattr(
        market_stats,
        "get_data_db",
        lambda: {"market_turnover_intraday": _Coll(intraday), "market_turnover_daily": _Coll(daily)},
    )
    response = _client().get("/api/market-stats/turnover-intraday", params={"days": 30, "window": 20})
    assert response.status_code == 200
    payload = response.json()
    assert payload["session_date"] == "2026-11-27"
    assert payload["is_final"] is False
    assert payload["projected"]["total"] == pytest.approx(100.0)
    assert payload["projected"]["ratio"] == pytest.approx(1.0)
    assert payload["baseline"]["profile_days"] == 5
    assert payload["boards"][0]["key"] == "sh"
    assert payload["boards"][0]["ratio"] == pytest.approx(20.0 / (30.0 * 0.6), abs=1e-3)


def test_turnover_intraday_empty_is_safe(monkeypatch):
    monkeypatch.setattr(
        market_stats,
        "get_data_db",
        lambda: {"market_turnover_intraday": _Coll([]), "market_turnover_daily": _Coll([])},
    )
    response = _client().get("/api/market-stats/turnover-intraday")
    assert response.status_code == 200
    payload = response.json()
    assert payload["session_date"] is None
    assert payload["projected"] is None

def test_turnover_intraday_accepts_a_historical_session(monkeypatch):
    def day(value):
        return [
            {"trade_date": f"2026-11-2{i}", "snapshot_time": f"2026-11-2{i}0930", "total": value * 0.25},
            {"trade_date": f"2026-11-2{i}", "snapshot_time": f"2026-11-2{i}1130", "total": value * 0.60},
            {"trade_date": f"2026-11-2{i}", "snapshot_time": f"2026-11-2{i}1500", "total": value},
        ]

    intraday = []
    for i in range(1, 6):
        intraday.extend(day(1000.0))
    daily = [_daily(f"2026-11-{d:02d}", 100.0) for d in range(1, 27)]
    monkeypatch.setattr(
        market_stats,
        "get_data_db",
        lambda: {"market_turnover_intraday": _Coll(intraday), "market_turnover_daily": _Coll(daily)},
    )
    response = _client().get(
        "/api/market-stats/turnover-intraday", params={"days": 30, "date": "2026-11-23"}
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["session_date"] == "2026-11-23"
    assert payload["is_live"] is False
    assert payload["available_dates"][0] == "2026-11-25"
    # the baseline must come from earlier sessions only
    assert payload["baseline"]["profile_days"] == 2
