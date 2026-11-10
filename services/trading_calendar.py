"""SSE trading-day helpers tolerant to ``trade_calendar.is_open`` type drift."""

from __future__ import annotations

from typing import Any, Optional

# 2026-11-10 凌晨日历被重写成全日历：is_open 由 int 1 变成字符串 "1"/"0"，
# 且不再只存开市日。所有读日历的判断都必须同时兼容两种表示与休市日。
_OPEN_FLAG_VALUES = (1, "1")
_TRUE_TEXT = {"1", "true", "yes", "y", "open"}


def is_sse_open_day(calendar: Any, trade_date: str) -> bool:
    """Return True when ``trade_date`` (YYYYMMDD) is an SSE open day.

    ``calendar`` is the ``trade_calendar`` collection handle. Missing
    ``is_open`` counts as open so the old open-days-only contract keeps working.
    """
    doc = calendar.find_one({"exchange": "SSE", "trade_date": str(trade_date)})
    if not doc:
        return False
    flag = doc.get("is_open", 1)
    if flag is True:
        return True
    return str(flag).strip().lower() in _TRUE_TEXT


def latest_sse_open_day(calendar: Any, end_date: str) -> Optional[str]:
    """Return the latest SSE open day on or before ``end_date`` (YYYYMMDD)."""
    doc = calendar.find_one(
        {
            "exchange": "SSE",
            "is_open": {"$in": list(_OPEN_FLAG_VALUES)},
            "trade_date": {"$lte": str(end_date)},
        },
        projection={"trade_date": 1},
        sort=[("trade_date", -1)],
    )
    value = str((doc or {}).get("trade_date") or "")
    return value or None
