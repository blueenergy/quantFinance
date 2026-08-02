"""Regression tests for tab performance query shapes."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from routers import market as market_router


@pytest.mark.unit
def test_previous_trade_dates_uses_distinct():
    coll = MagicMock()
    coll.distinct.return_value = ["20260108", "20260107", "20260106", "20260103"]

    out = market_router._previous_trade_dates(coll, "20260109", 3)

    coll.distinct.assert_called_once()
    assert out == ["20260106", "20260107", "20260108"]


@pytest.mark.unit
def test_sector_spectrum_query_limits_symbols(monkeypatch):
    captured = {}

    class _FakeCursor:
        def sort(self, *_args, **_kwargs):
            return self

        def __iter__(self):
            return iter([])

    volume_price = MagicMock()
    volume_price.find.return_value = _FakeCursor()

    monkeypatch.setattr(
        market_router,
        "get_data_db",
        lambda: {"volume_price": volume_price, "sw_index_member": MagicMock(find=lambda *a, **k: iter([]))},
    )
    monkeypatch.setattr(market_router, "_get_sector_spectrum_cache", lambda _key: None)
    monkeypatch.setattr(market_router, "_set_sector_spectrum_cache", lambda *_a, **_k: None)
    monkeypatch.setattr(
        market_router,
        "_resolve_sector_members",
        lambda _level: {
            "600519.SH": {"sector_code": "801010", "sector_name": "农林牧渔"},
            "000001.SZ": {"sector_code": "801010", "sector_name": "农林牧渔"},
        },
    )
    monkeypatch.setattr(market_router, "_previous_trade_dates", lambda *_a, **_k: [])

    def _capture_find(query, projection):
        captured["query"] = query
        return _FakeCursor()

    volume_price.find.side_effect = _capture_find

    market_router.get_market_spectrum_sectors(
        start_date="20260101",
        end_date="20260110",
        level="L1",
        ma_period=5,
        limit=80,
        sector_code=None,
    )

    assert "$in" in captured["query"]["symbol"]
    assert set(captured["query"]["symbol"]["$in"]) == {"600519.SH", "000001.SZ"}


@pytest.mark.unit
def test_market_spectrum_query_pins_ma_period_and_granularity(monkeypatch):
    """market_spectrum allows several rows per day, keyed on
    (trade_date, ma_period, granularity). Querying by date alone would return one
    row per MA variant and silently duplicate every date for every consumer.
    """
    captured = {}

    class _FakeCursor:
        def sort(self, *_args, **_kwargs):
            return iter([])

    collection = MagicMock()

    def _capture_find(query, projection):
        captured["query"] = query
        return _FakeCursor()

    collection.find.side_effect = _capture_find
    monkeypatch.setattr(market_router, "get_data_db", lambda: {"market_spectrum": collection})

    market_router.get_market_spectrum(start_date="20050101", end_date="20260731", ma_period=5)

    assert captured["query"]["ma_period"] == 5
    assert captured["query"]["granularity"] == "daily"
    assert captured["query"]["trade_date"] == {"$gte": "20050101", "$lte": "20260731"}
