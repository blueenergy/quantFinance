"""Pure price-metric functions (hfq OHLC window)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

import pandas as pd


@dataclass
class PriceWindowMetrics:
    ts_code: str
    start_trade_date: str
    end_trade_date: str
    range_amplitude: Optional[float]
    peak_gain: Optional[float]
    drawdown_from_peak: Optional[float]
    return_pct: Optional[float]
    peak_date: Optional[str]
    trough_date: Optional[str]
    peak_high: Optional[float]
    latest_close: Optional[float]
    adj_degraded: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "start_trade_date": self.start_trade_date,
            "end_trade_date": self.end_trade_date,
            "range_amplitude": self.range_amplitude,
            "peak_gain": self.peak_gain,
            "drawdown_from_peak": self.drawdown_from_peak,
            "return_pct": self.return_pct,
            "peak_date": self.peak_date,
            "trough_date": self.trough_date,
            "peak_high": self.peak_high,
            "latest_close": self.latest_close,
            "adj_degraded": self.adj_degraded,
        }


def _trade_date_str(idx) -> str:
    if hasattr(idx, "strftime"):
        return idx.strftime("%Y%m%d")
    return str(idx).replace("-", "")[:8]


def compute_price_window_metrics(ts_code: str, df: pd.DataFrame) -> Optional[PriceWindowMetrics]:
    """Compute interval metrics from an adjusted OHLC DataFrame (DatetimeIndex)."""
    if df is None or df.empty:
        return None

    work = df.sort_index()
    highs = pd.to_numeric(work["high"], errors="coerce")
    lows = pd.to_numeric(work["low"], errors="coerce")
    closes = pd.to_numeric(work["close"], errors="coerce")

    if highs.isna().all() or lows.isna().all() or closes.isna().all():
        return None

    min_low = float(lows.min())
    max_high = float(highs.max())
    if min_low <= 0:
        return None

    first_close = float(closes.iloc[0])
    latest_close = float(closes.iloc[-1])
    if first_close <= 0 or max_high <= 0:
        return None

    peak_idx = highs.idxmax()
    trough_idx = lows.idxmin()

    range_amplitude = (max_high - min_low) / min_low
    peak_gain = max_high / first_close - 1.0
    drawdown_from_peak = latest_close / max_high - 1.0
    return_pct = latest_close / first_close - 1.0

    adj_degraded = bool(work.attrs.get("adj_degraded", False))

    return PriceWindowMetrics(
        ts_code=ts_code,
        start_trade_date=_trade_date_str(work.index[0]),
        end_trade_date=_trade_date_str(work.index[-1]),
        range_amplitude=range_amplitude,
        peak_gain=peak_gain,
        drawdown_from_peak=drawdown_from_peak,
        return_pct=return_pct,
        peak_date=_trade_date_str(peak_idx),
        trough_date=_trade_date_str(trough_idx),
        peak_high=max_high,
        latest_close=latest_close,
        adj_degraded=adj_degraded,
    )


def compare_value(left: Optional[float], op: str, right: float) -> bool:
    if left is None:
        return False
    if op == "gt":
        return left > right
    if op == "gte":
        return left >= right
    if op == "lt":
        return left < right
    if op == "lte":
        return left <= right
    return False
