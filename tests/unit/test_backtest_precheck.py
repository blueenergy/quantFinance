"""Unit tests for the submit-time backtest window pre-check."""

from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from services.backtest_precheck import (
    estimate_max_trading_bars,
    load_min_bars_map,
    minimum_calendar_days,
    validate_backtest_window,
)


class FakeMetadataCollection:
    def __init__(self, docs):
        self.docs = docs

    def find(self, query=None, projection=None):
        return [doc.copy() for doc in self.docs]


class BrokenMetadataCollection:
    def find(self, query=None, projection=None):
        raise RuntimeError("mongo is down")


def _collections(docs):
    return {"strategy_metadata": FakeMetadataCollection(docs)}


@pytest.mark.unit
def test_estimate_max_trading_bars_is_an_upper_bound():
    # January 2024 has 31 calendar days and 22 actual A-share trading days.
    assert estimate_max_trading_bars("20240101", "20240131") == 23


@pytest.mark.unit
def test_estimate_max_trading_bars_counts_both_endpoints():
    assert estimate_max_trading_bars("20240101", "20240107") == 5


@pytest.mark.unit
def test_estimate_max_trading_bars_accepts_dashed_dates():
    assert estimate_max_trading_bars("2024-01-01", "2024-01-31") == 23


@pytest.mark.unit
def test_estimate_max_trading_bars_returns_none_for_unparsable_dates():
    assert estimate_max_trading_bars("not-a-date", "20240131") is None
    assert estimate_max_trading_bars("20240101", "") is None


@pytest.mark.unit
def test_estimate_max_trading_bars_is_zero_for_reversed_range():
    assert estimate_max_trading_bars("20240131", "20240101") == 0


@pytest.mark.unit
def test_rejects_window_shorter_than_strategy_requirement():
    """Regression: a real turtle task failed after running, not at submit."""
    collections = _collections([{"key": "turtle", "min_bars": 50}])

    with pytest.raises(HTTPException) as exc:
        validate_backtest_window(collections, ["turtle"], "20240101", "20240131")

    assert exc.value.status_code == 422
    assert "turtle" in exc.value.detail
    assert "50" in exc.value.detail


@pytest.mark.unit
def test_accepts_window_long_enough_for_strategy():
    collections = _collections([{"key": "turtle", "min_bars": 50}])

    validate_backtest_window(collections, ["turtle"], "20240101", "20241231")


@pytest.mark.unit
def test_rejects_when_any_combo_strategy_needs_more_bars():
    collections = _collections([
        {"key": "turtle", "min_bars": 50},
        {"key": "single_yang", "min_bars": 120},
    ])

    # At most 87 bars: enough for turtle, impossible for single_yang's MA120.
    with pytest.raises(HTTPException) as exc:
        validate_backtest_window(collections, ["turtle", "single_yang"], "20240101", "20240430")

    assert "single_yang" in exc.value.detail


@pytest.mark.unit
def test_suggested_span_actually_satisfies_the_requirement():
    """The span named in the error message must clear the pre-check."""
    start = datetime(2024, 1, 1)
    for min_bars in (50, 55, 60, 120):
        span = minimum_calendar_days(min_bars)
        end = start + timedelta(days=span - 1)
        assert estimate_max_trading_bars(
            start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
        ) >= min_bars


@pytest.mark.unit
def test_pre_check_is_skipped_when_metadata_is_missing():
    """A bookkeeping collection must never block submissions."""
    validate_backtest_window({}, ["turtle"], "20240101", "20240131")
    validate_backtest_window({"strategy_metadata": None}, ["turtle"], "20240101", "20240131")


@pytest.mark.unit
def test_pre_check_is_skipped_when_metadata_read_fails():
    collections = {"strategy_metadata": BrokenMetadataCollection()}

    validate_backtest_window(collections, ["turtle"], "20240101", "20240131")


@pytest.mark.unit
def test_pre_check_is_skipped_for_strategy_without_min_bars():
    collections = _collections([{"key": "turtle"}, {"key": "grid", "min_bars": None}])

    validate_backtest_window(collections, ["turtle", "grid"], "20240101", "20240131")


@pytest.mark.unit
def test_pre_check_is_skipped_for_unparsable_dates():
    collections = _collections([{"key": "turtle", "min_bars": 50}])

    validate_backtest_window(collections, ["turtle"], "garbage", "20240131")


@pytest.mark.unit
def test_load_min_bars_map_ignores_malformed_entries():
    collections = _collections([
        {"key": "turtle", "min_bars": 55},
        {"key": "grid", "min_bars": "fifty"},
        {"min_bars": 30},
    ])

    assert load_min_bars_map(collections) == {"turtle": 55}
