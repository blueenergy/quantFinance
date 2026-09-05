import pandas as pd
import pytest
from fastapi.testclient import TestClient

from auth import get_current_active_user
from main import app
from routers import kline_series


class _EmptyMongoCursor(list):
    """Minimal Mongo cursor stub supporting sort().limit()."""

    def sort(self, *args, **kwargs):
        return self

    def limit(self, n):
        return self


@pytest.fixture
def authed_client():
    app.dependency_overrides[get_current_active_user] = lambda: {"id": "u1", "is_active": True}
    yield TestClient(app)
    app.dependency_overrides.pop(get_current_active_user, None)


@pytest.mark.unit
def test_kline_series_stock_uses_adjusted_prices(monkeypatch, authed_client):
    df = pd.DataFrame(
        {
            "trade_date": ["20240101", "20240102"],
            "open": [9.0, 10.0],
            "high": [10.0, 11.0],
            "low": [8.5, 9.5],
            "close": [9.5, 10.5],
            "volume": [100, 200],
            "adj_factor": [1.0, 1.1],
        }
    )

    class _FakeDao:
        def load_adjusted_ohlc(self, symbol, start, end, adjust="qfq"):
            out = df.copy()
            if adjust == "qfq":
                out["close"] = out["close"] * out["adj_factor"] / 1.1
            return out.set_index(pd.to_datetime(out["trade_date"]))

    monkeypatch.setattr(
        kline_series,
        "_index_prices",
        type("C", (), {"find": lambda *a, **k: _EmptyMongoCursor()})(),
    )

    class _EmptyEtfBasic:
        def find_one(self, *args, **kwargs):
            return None

    monkeypatch.setattr(kline_series, "_etf_basic", _EmptyEtfBasic())
    monkeypatch.setattr(kline_series, "AdjustedPriceDataAccess", lambda db: _FakeDao())

    resp = authed_client.get(
        "/api/kline-series",
        params={"symbol": "600519.SH", "start_date": "20240101", "end_date": "20240102"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "volume_price"
    assert body["adjust"] == "qfq"
    assert body["count"] == 2
    assert body["data"][-1]["close"] == pytest.approx(10.5)


@pytest.mark.unit
def test_kline_series_index_branch_ignores_adjust(monkeypatch, authed_client):
    class _IdxColl:
        def find(self, flt, proj):
            return _EmptyMongoCursor(
                [
                    {
                        "trade_date": "20240101",
                        "open": 100,
                        "high": 101,
                        "low": 99,
                        "close": 100.5,
                        "volume": 1,
                    }
                ]
            )

    monkeypatch.setattr(kline_series, "_index_prices", _IdxColl())

    resp = authed_client.get("/api/kline-series", params={"symbol": "000300.SH", "adjust": "qfq"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "index_prices"
    assert body["adjust"] == "none"
    assert body["data"][0]["close"] == 100.5


class _EtfColl:
    def __init__(self, docs):
        self.docs = list(docs)

    def find_one(self, query=None, projection=None, sort=None):
        rows = [dict(doc) for doc in self.docs if self._matches(doc, query or {})]
        if sort:
            field, direction = sort[0]
            rows.sort(key=lambda row: str(row.get(field) or ""), reverse=direction < 0)
        return rows[0] if rows else None

    def find(self, query=None, projection=None, sort=None, limit=0):
        rows = [dict(doc) for doc in self.docs if self._matches(doc, query or {})]
        if sort:
            field, direction = sort[0]
            rows.sort(key=lambda row: str(row.get(field) or ""), reverse=direction < 0)
        if limit:
            rows = rows[:limit]
        return rows

    def _matches(self, doc, query):
        for key, expected in query.items():
            value = doc.get(key)
            if isinstance(expected, dict):
                if "$in" in expected and value not in expected["$in"]:
                    return False
                if "$gte" in expected and not (str(value) >= str(expected["$gte"])):
                    return False
                if "$lte" in expected and not (str(value) <= str(expected["$lte"])):
                    return False
                continue
            if value != expected:
                return False
        return True


@pytest.mark.unit
def test_kline_series_etf_uses_etf_daily_not_volume_price(monkeypatch, authed_client):
    monkeypatch.setattr(
        kline_series,
        "_index_prices",
        type("C", (), {"find": lambda *a, **k: _EmptyMongoCursor()})(),
    )
    monkeypatch.setattr(
        kline_series,
        "_etf_basic",
        _EtfColl([{"ts_code": "510300.SH", "name": "沪深300ETF"}]),
    )
    monkeypatch.setattr(
        kline_series,
        "_etf_daily",
        _EtfColl(
            [
                {
                    "ts_code": "510300.SH",
                    "trade_date": "20240102",
                    "open": 4.5,
                    "high": 4.7,
                    "low": 4.4,
                    "close": 4.6,
                    "vol": 100,
                },
                {
                    "ts_code": "510300.SH",
                    "trade_date": "20240101",
                    "open": 4.4,
                    "high": 4.5,
                    "low": 4.3,
                    "close": 4.5,
                    "vol": 80,
                },
            ]
        ),
    )
    monkeypatch.setattr(
        kline_series,
        "_etf_adj_factor",
        _EtfColl(
            [
                {"ts_code": "510300.SH", "trade_date": "20240101", "adj_factor": 1.0},
                {"ts_code": "510300.SH", "trade_date": "20240102", "adj_factor": 1.0},
            ]
        ),
    )

    resp = authed_client.get(
        "/api/kline-series",
        params={"symbol": "510300.SH", "start_date": "20240101", "end_date": "20240102"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "etf_daily"
    assert body["count"] == 2
    assert body["data"][0]["trade_date"] == "20240101"
    assert body["data"][-1]["close"] == pytest.approx(4.6)
    assert body["data"][-1]["volume"] == pytest.approx(100)


@pytest.mark.unit
def test_kline_series_futures_uses_fut_daily(monkeypatch, authed_client):
    class _FutColl:
        def find(self, flt, proj):
            return _EmptyMongoCursor(
                [
                    {
                        "ts_code": "IF.CFX",
                        "trade_date": "20240102",
                        "open": 3500.0,
                        "high": 3520.0,
                        "low": 3490.0,
                        "close": 3515.0,
                        "vol": 1000.0,
                        "amount": 2000.0,
                        "oi": 50000.0,
                        "contract": "IF2401.CFX",
                    }
                ]
            )

    monkeypatch.setattr(
        kline_series,
        "_index_prices",
        type("C", (), {"find": lambda *a, **k: _EmptyMongoCursor()})(),
    )
    monkeypatch.setattr(kline_series, "_etf_basic", _EtfColl([]))
    monkeypatch.setattr(kline_series, "_fut_daily", _FutColl())

    resp = authed_client.get(
        "/api/kline-series",
        params={"symbol": "IF.CFX", "start_date": "20240101", "end_date": "20240102"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "fut_daily"
    assert body["adjust"] == "none"
    assert body["count"] == 1
    assert body["data"][0]["contract"] == "IF2401.CFX"
    assert body["data"][0]["oi"] == pytest.approx(50000.0)


@pytest.mark.unit
def test_futures_varieties_endpoint(authed_client):
    resp = authed_client.get("/api/futures/varieties")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["count"] == 16
    codes = [row["ts_code"] for row in body["data"]["all"]]
    assert "IF.CFX" in codes
    assert "LC.GFE" in codes
