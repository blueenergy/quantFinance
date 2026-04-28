"""Unit tests for routers.market.fetch_market_data (no real Mongo/network)."""
import os

# Before importing routers.market: market_analysis_cache singleton uses this to pick in-memory store.
os.environ.setdefault("PYTEST_CURRENT_TEST", "1")

import datetime
import pytest
from routers import market


class FakeCursor(list):
    def limit(self, n):
        return list(self)[:n]


class FakeCollection:
    def __init__(self, latest_record=None, historical=None):
        self._latest = latest_record
        self._historical = historical or []

    def find_one(self, query, sort=None):
        sym = query.get("symbol") if query else None
        if sym is None:
            return self._latest
        if self._latest and self._latest.get("symbol") == sym:
            return self._latest
        for doc in self._historical:
            if doc.get("symbol") == sym:
                return doc
        return None

    def find(self, query=None, sort=None):
        sym = query.get("symbol") if query else None
        rows = self._historical
        if sym is not None:
            rows = [d for d in rows if d.get("symbol") == sym]
        return FakeCursor(rows)


@pytest.mark.unit
def test_fetch_market_data_external_success(monkeypatch):
    external = {"analysis_date": "2025-09-19", "market_data": {"IDX": {"symbol": "IDX"}}}

    monkeypatch.setattr(market.external_market_data, "get_real_market_data", lambda: external)

    result = market.fetch_market_data()

    assert result is external
    assert result is not None
    assert "market_data" in result


@pytest.mark.unit
def test_fetch_market_data_local_db_fallback(monkeypatch):
    def raise_err():
        raise RuntimeError("external down")

    monkeypatch.setattr(market.external_market_data, "get_real_market_data", raise_err)

    trade_date = datetime.date(2025, 9, 19)
    latest = {"symbol": "1A0001", "trade_date": trade_date, "price": 100}
    historical = [
        {"symbol": "1A0001", "trade_date": trade_date - datetime.timedelta(days=i), "price": 100 - i}
        for i in range(5)
    ]

    fake_col = FakeCollection(latest_record=latest, historical=historical)

    def fake_get_data_db():
        return {"volume_price": fake_col}

    monkeypatch.setattr(market, "get_data_db", fake_get_data_db)

    result = market.fetch_market_data()

    assert result is not None
    assert result.get("data_source") == "local_database"
    assert "market_data" in result
    assert len(result["market_data"]) >= 1


@pytest.mark.unit
def test_fetch_market_data_no_data(monkeypatch):
    monkeypatch.setattr(
        market.external_market_data,
        "get_real_market_data",
        lambda: (_ for _ in ()).throw(RuntimeError("fail")),
    )

    fake_col = FakeCollection(latest_record=None, historical=[])

    def fake_get_data_db():
        return {"volume_price": fake_col}

    monkeypatch.setattr(market, "get_data_db", fake_get_data_db)

    result = market.fetch_market_data()
    assert result is None


@pytest.mark.unit
def test_prune_market_data_for_dashboard_categories_shape():
    md = {
        "categories": {
            "indices": {"^IXIC": {"price": 1}, "EXTRA": {"price": 99}},
            "commodities": {"GC=F": {"price": 2000}, "IGNORE": {}},
        },
        "meta": "keep",
    }
    out = market._prune_market_data_for_dashboard(md)
    assert out["meta"] == "keep"
    assert "^IXIC" in out["categories"]["indices"]
    assert "EXTRA" not in out["categories"]["indices"]
    assert "GC=F" in out["categories"]["commodities"]
    assert "IGNORE" not in out["categories"]["commodities"]


@pytest.mark.unit
def test_prune_market_data_for_dashboard_flat_shape():
    md = {
        "indices": {"^IXIC": {"p": 1}, "ZZ": {}},
        "forex": {"CNH=X": {"p": 7}, "OTHER": {}},
    }
    out = market._prune_market_data_for_dashboard(md)
    assert "^IXIC" in out["indices"] and "ZZ" not in out["indices"]
    assert "CNH=X" in out["forex"] and "OTHER" not in out["forex"]
