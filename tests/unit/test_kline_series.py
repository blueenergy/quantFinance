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
