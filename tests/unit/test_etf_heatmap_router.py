"""Router tests for GET /api/etf/heatmap endpoints."""
import pytest
from fastapi.testclient import TestClient

from main import app
from routers import etf as etf_mod
from tests.unit.test_etf_router import (
    BasicCollection,
    ChainCursor,
    EmptyCollection,
    ShareSizeCollection,
    _make_data_db,
)


class HeatmapShareSizeCollection(ShareSizeCollection):
    """ShareSizeCollection with $in and date range filters."""

    def find(self, query, projection=None):
        filtered = list(self.docs)
        if query:
            if "ts_code" in query and isinstance(query["ts_code"], dict) and "$in" in query["ts_code"]:
                inc = set(query["ts_code"]["$in"])
                filtered = [d for d in filtered if d.get("ts_code") in inc]
            td_filter = query.get("trade_date")
            if isinstance(td_filter, dict):
                lo = td_filter.get("$gte")
                hi = td_filter.get("$lte")
                if lo is not None:
                    filtered = [d for d in filtered if str(d.get("trade_date", "")) >= str(lo)]
                if hi is not None:
                    filtered = [d for d in filtered if str(d.get("trade_date", "")) <= str(hi)]
        return ChainCursor([dict(d) for d in filtered])


@pytest.fixture
def heatmap_share_docs():
    """Two semiconductors over 3 days for window=1d/5d tests."""
    return [
        {
            "ts_code": "512760.SH",
            "trade_date": "20250410",
            "total_share": 1000.0,
            "total_size": 2000.0,
            "nav": 2.0,
            "close": 2.0,
        },
        {
            "ts_code": "512760.SH",
            "trade_date": "20250409",
            "total_share": 990.0,
            "total_size": 1980.0,
            "nav": 2.0,
            "close": 2.0,
        },
        {
            "ts_code": "159995.SZ",
            "trade_date": "20250410",
            "total_share": 500.0,
            "total_size": 1500.0,
            "nav": 3.0,
            "close": 3.0,
        },
        {
            "ts_code": "159995.SZ",
            "trade_date": "20250409",
            "total_share": 490.0,
            "total_size": 1470.0,
            "nav": 3.0,
            "close": 3.0,
        },
    ]


@pytest.mark.unit
def test_heatmap_route_not_captured_by_symbol_kline(monkeypatch, heatmap_share_docs):
    basic = [
        {"ts_code": "512760.SH", "name": "半导体ETF"},
        {"ts_code": "159995.SZ", "name": "芯片ETF"},
    ]
    monkeypatch.setattr(
        etf_mod,
        "data_db",
        _make_data_db(
            {
                "etf_share_size": HeatmapShareSizeCollection(heatmap_share_docs),
                "etf_basic": BasicCollection(basic),
            }
        ),
    )
    client = TestClient(app)
    r = client.get("/api/etf/heatmap", params={"window": "1d", "trade_date": "20250410"})
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["trade_date"] == "20250410"
    assert isinstance(body["cells"], list)
    semi = next(c for c in body["cells"] if c["bucket_id"] == "semiconductor")
    assert semi["kind"] == "theme"
    # top2 by size: 512760 (2000) + 159995 (1500); day net: 10*2 + 10*3 = 50
    assert semi["net_inflow"] == pytest.approx(50.0)


@pytest.mark.unit
def test_heatmap_invalid_window(monkeypatch):
    monkeypatch.setattr(
        etf_mod,
        "data_db",
        _make_data_db(
            {
                "etf_share_size": EmptyCollection(),
                "etf_basic": BasicCollection([]),
            }
        ),
    )
    client = TestClient(app)
    r = client.get("/api/etf/heatmap", params={"window": "7d"})
    assert r.status_code == 400


@pytest.mark.unit
def test_heatmap_series_for_bucket(monkeypatch, heatmap_share_docs):
    basic = [
        {"ts_code": "512760.SH", "name": "半导体ETF"},
        {"ts_code": "159995.SZ", "name": "芯片ETF"},
    ]
    monkeypatch.setattr(
        etf_mod,
        "data_db",
        _make_data_db(
            {
                "etf_share_size": HeatmapShareSizeCollection(heatmap_share_docs),
                "etf_basic": BasicCollection(basic),
            }
        ),
    )
    client = TestClient(app)
    r = client.get(
        "/api/etf/heatmap/semiconductor/series",
        params={"limit": 10, "trade_date": "20250410"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["bucket_id"] == "semiconductor"
    assert body["flagship_ts_code"] == "512760.SH"
    assert len(body["series"]) >= 1
    assert body["series"][-1]["trade_date"] == "20250410"


@pytest.mark.unit
def test_heatmap_series_unknown_bucket(monkeypatch):
    monkeypatch.setattr(
        etf_mod,
        "data_db",
        _make_data_db(
            {
                "etf_share_size": EmptyCollection(),
                "etf_basic": BasicCollection([]),
            }
        ),
    )
    client = TestClient(app)
    r = client.get("/api/etf/heatmap/not-a-bucket/series")
    assert r.status_code == 404
