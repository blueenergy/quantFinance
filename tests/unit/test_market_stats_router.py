from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from auth import get_current_active_user
from routers import market_stats
from services.market_stats import attach_window_delta, pick_pe_index


class _Cursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, *_args, **_kwargs):
        self._docs.sort(key=lambda d: d.get("trade_date", ""))
        return self

    def __iter__(self):
        return iter(self._docs)


class _Coll:
    def __init__(self, docs):
        self.docs = list(docs)

    def find(self, query, _projection=None):
        filtered = []
        for doc in self.docs:
            ok = True
            for key, cond in (query or {}).items():
                value = doc.get(key)
                if isinstance(cond, dict):
                    lo = cond.get("$gte")
                    if lo is not None and str(value) < str(lo):
                        ok = False
                        break
                elif value != cond:
                    ok = False
                    break
            if ok:
                filtered.append(dict(doc))
        return _Cursor(filtered)


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(market_stats.router, prefix="/api")
    app.dependency_overrides[get_current_active_user] = lambda: {"id": "u1", "is_active": True}
    return TestClient(app)


@pytest.mark.unit
def test_pick_pe_index_prefers_alla_then_sse():
    code, name = pick_pe_index(["000300.SH", "ALLA.WI", "000001.SH"])
    assert code == "ALLA.WI"
    assert name == "全A（自算）"
    code, name = pick_pe_index(["000300.SH", "000001.SH", "399006.SZ"])
    assert code == "000001.SH"
    assert name == "上证指数"
    code, name = pick_pe_index(["000300.SH", "399006.SZ"])
    assert code == "000300.SH"
    code, name = pick_pe_index(["000300.SH"], requested="000300.SH")
    assert code == "000300.SH"
    assert pick_pe_index([]) == (None, None)


@pytest.mark.unit
def test_attach_window_delta_uses_trading_day_offset():
    rows = [{"trade_date": f"20260{i:03d}", "rzrqye": float(i)} for i in range(1, 36)]
    out = attach_window_delta(rows, field="rzrqye", window=30)
    assert out[0]["delta_30d"] is None
    assert out[29]["delta_30d"] is None
    assert out[30]["delta_30d"] == pytest.approx(30.0)


@pytest.mark.unit
def test_overview_returns_pe_and_margin_series(monkeypatch):
    pe_docs = [
        {"ts_code": "000001.SH", "trade_date": "20260810", "pe_ttm": 14.0, "pe": 13.0, "pb": 1.2},
        {"ts_code": "000300.SH", "trade_date": "20260810", "pe_ttm": 12.5, "pe": 12.0, "pb": 1.3},
        {"ts_code": "000001.SH", "trade_date": "20260811", "pe_ttm": 14.2, "pe": 13.1, "pb": 1.21},
    ]
    margin_docs = [
        {"exchange_id": "ALL", "trade_date": "20260810", "rzye": 1.0e12, "rqye": 1.0e10, "rzrqye": 1.01e12},
        {"exchange_id": "ALL", "trade_date": "20260811", "rzye": 1.02e12, "rqye": 1.1e10, "rzrqye": 1.031e12},
        {"exchange_id": "SSE", "trade_date": "20260811", "rzye": 6.0e11, "rqye": 5.0e9, "rzrqye": 6.05e11},
    ]
    monkeypatch.setattr(
        market_stats,
        "get_data_db",
        lambda: {
            "index_daily_basic": _Coll(pe_docs),
            "market_pe_daily": _Coll([]),
            "margin_market_daily": _Coll(margin_docs),
        },
    )
    res = _client().get("/api/market-stats/overview", params={"days": 60})
    assert res.status_code == 200
    body = res.json()
    assert body["pe"]["index_code"] == "000001.SH"
    assert body["pe"]["index_name"] == "上证指数"
    assert body["pe"]["latest"]["pe_ttm"] == pytest.approx(14.2)
    assert len(body["pe"]["series"]) == 2
    assert body["margin"]["latest"]["rzrqye"] == pytest.approx(1.031e12)
    assert body["margin"]["latest"]["delta_30d"] is None
    assert len(body["margin"]["series"]) == 2


@pytest.mark.unit
def test_overview_empty_collections(monkeypatch):
    monkeypatch.setattr(
        market_stats,
        "get_data_db",
        lambda: {
            "index_daily_basic": _Coll([]),
            "market_pe_daily": _Coll([]),
            "margin_market_daily": _Coll([]),
        },
    )
    res = _client().get("/api/market-stats/overview")
    assert res.status_code == 200
    body = res.json()
    assert body["pe"]["series"] == []
    assert body["margin"]["latest"] is None


@pytest.mark.unit
def test_overview_defaults_to_alla_and_lists_it_first(monkeypatch):
    pe_docs = [
        {"ts_code": "000001.SH", "trade_date": "20260811", "pe_ttm": 14.2},
    ]
    alla_docs = [
        {
            "ts_code": "ALLA.WI",
            "trade_date": "20260811",
            "pe_ttm": 18.5,
            "n_used": 3200,
            "n_loss": 800,
            "n_missing": 100,
            "n_universe": 4100,
            "method": "cap_harmonic_ttm",
        }
    ]
    monkeypatch.setattr(
        market_stats,
        "get_data_db",
        lambda: {
            "index_daily_basic": _Coll(pe_docs),
            "market_pe_daily": _Coll(alla_docs),
            "margin_market_daily": _Coll([]),
        },
    )
    res = _client().get("/api/market-stats/overview", params={"days": 60})
    assert res.status_code == 200
    body = res.json()
    assert body["pe"]["index_code"] == "ALLA.WI"
    assert body["pe"]["index_name"] == "全A（自算）"
    assert body["pe"]["available_indices"][0]["ts_code"] == "ALLA.WI"
    assert body["pe"]["latest"]["pe_ttm"] == pytest.approx(18.5)
    assert body["pe"]["latest"]["n_used"] == 3200
