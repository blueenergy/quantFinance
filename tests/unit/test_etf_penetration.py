from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from auth import get_current_active_user
from routers import market_stats
from services import etf_penetration


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
                    if "$in" in cond and value not in cond["$in"]:
                        ok = False
                        break
                    if "$gte" in cond and str(value) < str(cond["$gte"]):
                        ok = False
                        break
                elif value != cond:
                    ok = False
                    break
            if ok:
                filtered.append(dict(doc))
        return filtered


TEST_POOLS = {
    "test": {
        "name": "测试指数",
        "index_code": "TEST.IDX",
        "funds": [
            {"ts_code": "AAA.SH", "name": "A ETF"},
            {"ts_code": "BBB.SH", "name": "B ETF"},
        ],
    }
}


def _collections():
    shares = [
        {"ts_code": "AAA.SH", "trade_date": "20260105", "fd_share": 10000.0},
        {"ts_code": "AAA.SH", "trade_date": "20260106", "fd_share": 11000.0},
        {"ts_code": "AAA.SH", "trade_date": "20260107", "fd_share": 12000.0},
        {"ts_code": "BBB.SH", "trade_date": "20260105", "fd_share": 5000.0},
        # 20260106 missing BBB on purpose: the point must be dropped, not summed
        # with partial coverage.
        {"ts_code": "BBB.SH", "trade_date": "20260107", "fd_share": 6000.0},
    ]
    closes = [
        {"ts_code": "AAA.SH", "trade_date": "20260105", "close": 2.0},
        {"ts_code": "AAA.SH", "trade_date": "20260106", "close": 2.0},
        {"ts_code": "AAA.SH", "trade_date": "20260107", "close": 2.0},
        {"ts_code": "BBB.SH", "trade_date": "20260105", "close": 2.0},
        {"ts_code": "BBB.SH", "trade_date": "20260106", "close": 2.0},
        {"ts_code": "BBB.SH", "trade_date": "20260107", "close": 2.0},
    ]
    basic = [
        {"ts_code": "TEST.IDX", "trade_date": "20260105", "float_mv": 1.0e11},
        {"ts_code": "TEST.IDX", "trade_date": "20260106", "float_mv": 1.1e11},
        {"ts_code": "TEST.IDX", "trade_date": "20260107", "float_mv": 1.2e11},
    ]
    prices = [
        {"ts_code": "TEST.IDX", "trade_date": "20260105", "close": 4000.0},
        {"ts_code": "TEST.IDX", "trade_date": "20260106", "close": 4100.0},
        {"ts_code": "TEST.IDX", "trade_date": "20260107", "close": 4200.0},
    ]
    return {
        "etf_shares": _Coll(shares),
        "etf_daily": _Coll(closes),
        "index_daily_basic": _Coll(basic),
        "index_prices": _Coll(prices),
    }


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(market_stats.router, prefix="/api")
    app.dependency_overrides[get_current_active_user] = lambda: {"id": "u1", "is_active": True}
    return TestClient(app)


@pytest.mark.unit
def test_compute_penetration_requires_full_pool_coverage(monkeypatch):
    monkeypatch.setattr(etf_penetration, "ETF_PENETRATION_POOLS", TEST_POOLS)
    payload = etf_penetration.compute_penetration(_collections(), index_key="test", days=0)

    assert payload["success"] is True
    assert payload["index"]["name"] == "测试指数"
    assert payload["coverage"]["fund_count"] == 2
    # 20260106 dropped: BBB has no share row that day.
    assert [row["trade_date"] for row in payload["series"]] == ["20260105", "20260107"]
    first = payload["series"][0]
    # (10000 + 5000) 万份 * 2 元 = 30000 万元 = 3 亿元 / 1000 亿元 = 0.3%
    assert first["aum_yi"] == pytest.approx(3.0)
    assert first["float_mv_yi"] == pytest.approx(1000.0)
    assert first["ratio_pct"] == pytest.approx(0.3)
    assert first["index_close"] == pytest.approx(4000.0)
    assert payload["latest"]["trade_date"] == "20260107"
    # (12000 + 6000) * 2 = 3.6 亿元 / 1200 亿元 = 0.3%
    assert payload["latest"]["ratio_pct"] == pytest.approx(0.3)
    assert payload["latest"]["index_close"] == pytest.approx(4200.0)


@pytest.mark.unit
def test_compute_penetration_rejects_unknown_index(monkeypatch):
    monkeypatch.setattr(etf_penetration, "ETF_PENETRATION_POOLS", TEST_POOLS)
    with pytest.raises(ValueError):
        etf_penetration.compute_penetration(_collections(), index_key="nope", days=0)


@pytest.mark.unit
def test_penetration_router_returns_series_and_indices(monkeypatch):
    monkeypatch.setattr(etf_penetration, "ETF_PENETRATION_POOLS", TEST_POOLS)
    monkeypatch.setattr(market_stats, "get_data_db", _collections)

    res = _client().get("/api/market-stats/etf-penetration", params={"index": "test", "days": 0})
    assert res.status_code == 200
    body = res.json()
    assert body["index"]["key"] == "test"
    assert body["available_indices"][0]["key"] == "test"
    assert len(body["series"]) == 2
    assert body["latest"]["ratio_pct"] == pytest.approx(0.3)


@pytest.mark.unit
def test_penetration_router_rejects_unknown_index(monkeypatch):
    monkeypatch.setattr(etf_penetration, "ETF_PENETRATION_POOLS", TEST_POOLS)
    monkeypatch.setattr(market_stats, "get_data_db", _collections)

    res = _client().get("/api/market-stats/etf-penetration", params={"index": "nope"})
    assert res.status_code == 400
