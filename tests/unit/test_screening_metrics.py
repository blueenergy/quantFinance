"""Unit tests for price window metric pure functions."""

import pandas as pd
import pytest

from services.screening.metrics import (
    PriceWindowMetrics,
    compare_value,
    compute_price_window_metrics,
)


def _make_df(closes, highs=None, lows=None):
    n = len(closes)
    if highs is None:
        highs = [c * 1.05 for c in closes]
    if lows is None:
        lows = [c * 0.95 for c in closes]
    dates = pd.date_range("2026-04-01", periods=n, freq="B")
    df = pd.DataFrame(
        {
            "close": closes,
            "high": highs,
            "low": lows,
            "open": closes,
        },
        index=dates,
    )
    df.attrs["adj_degraded"] = False
    return df


def test_range_amplitude_and_peak_gain():
    df = _make_df([10, 12, 11, 9], highs=[11, 15, 12, 10], lows=[9, 11, 10, 8])
    m = compute_price_window_metrics("600000.SH", df)
    assert m is not None
    assert m.peak_gain == pytest.approx(15 / 10 - 1)
    assert m.range_amplitude == pytest.approx((15 - 8) / 8)
    assert m.drawdown_from_peak == pytest.approx(9 / 15 - 1)
    assert m.return_pct == pytest.approx(9 / 10 - 1)


def test_empty_df_returns_none():
    assert compute_price_window_metrics("X", pd.DataFrame()) is None


def test_compare_ops():
    assert compare_value(0.5, "gt", 0.3)
    assert compare_value(0.5, "gte", 0.5)
    assert compare_value(0.2, "lt", 0.3)
    assert compare_value(0.2, "lte", 0.2)
    assert not compare_value(None, "gt", 0.1)
