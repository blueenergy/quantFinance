"""Build 30m/60m K-line rows from ``minute_bars`` 1m and stamp decision G/S."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, NamedTuple, Optional

from stock_data_access.indicators.decision_gs import attach_decision_gs
from stock_data_access.indicators.intraday_resample import (
    filter_bars_by_ymd,
    normalize_intraday_tf,
    resample_ashare_intraday,
)

SOURCE_1M_CAP = 20000
DEFAULT_LOOKBACK_DAYS = 50
WARMUP_CALENDAR_DAYS = {"30m": 25, "60m": 45}


class IntradayWindows(NamedTuple):
    source_start: str
    source_end: str
    visible_start: str
    visible_end: str


def compact_day(value: Optional[str]) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits[:8] if len(digits) >= 8 else ""


def _shift_ymd(ymd: str, days: int) -> str:
    return (datetime.strptime(ymd, "%Y%m%d") + timedelta(days=days)).strftime("%Y%m%d")


def intraday_query_windows(
    tf: str,
    start_date: Optional[str],
    end_date: Optional[str],
    *,
    today: Optional[datetime] = None,
) -> IntradayWindows:
    """1m fetch window (includes G/S warmup) and the user-visible day range."""
    now = today or datetime.now()
    visible_end = compact_day(end_date) or now.strftime("%Y%m%d")
    visible_start = compact_day(start_date)
    if not visible_start:
        visible_start = _shift_ymd(visible_end, -(DEFAULT_LOOKBACK_DAYS - 1))
    extra = WARMUP_CALENDAR_DAYS.get(tf, 0)
    return IntradayWindows(
        source_start=_shift_ymd(visible_start, -extra),
        source_end=visible_end,
        visible_start=visible_start,
        visible_end=visible_end,
    )


def minute_trade_date_bound(value: Optional[str], *, end: bool) -> Optional[str]:
    day = compact_day(value)
    if not day:
        return None
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) >= 12:
        return digits[:12]
    return day + ("2359" if end else "0000")


def build_intraday_gs_bars(
    minute_rows: List[Dict[str, Any]],
    tf: str,
    *,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Resample 1m → 30m/60m, attach decision_gs_v1, then apply the visible day window."""
    normalized = normalize_intraday_tf(tf)
    if normalized == "1m":
        rows = list(minute_rows)
        if limit is not None:
            return rows[: max(1, int(limit))]
        return rows
    resampled = resample_ashare_intraday(minute_rows, normalized)
    stamped = attach_decision_gs(resampled)
    visible = filter_bars_by_ymd(
        stamped,
        compact_day(start_date) or None,
        compact_day(end_date) or None,
    )
    if limit is not None:
        return visible[: max(1, int(limit))]
    return visible
