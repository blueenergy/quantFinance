"""Reject backtest windows that cannot hold enough bars, at submit time.

Strategies need an indicator warm-up window, and the worker already fails a
task whose feed is too short. Catching it here instead means the user is told
immediately rather than after a whole batch has run and come back
partial_failed.

The bar count is deliberately an *upper* bound: trading days are at most five
in seven calendar days, and holidays only reduce that further. So a window
rejected here could not have worked under any market calendar, and a window
that is merely borderline is still allowed through to the worker.
"""
import logging
import math
from datetime import datetime
from typing import Any, Dict, Iterable, Optional

from fastapi import HTTPException

log = logging.getLogger(__name__)

_DATE_FORMAT = "%Y%m%d"
_TRADING_DAYS_PER_WEEK = 5
_CALENDAR_DAYS_PER_WEEK = 7


def _parse_date(value: Any) -> Optional[datetime]:
    text = str(value or "").strip().replace("-", "")
    if not text:
        return None
    try:
        return datetime.strptime(text, _DATE_FORMAT)
    except ValueError:
        return None


def estimate_max_trading_bars(start_date: Any, end_date: Any) -> Optional[int]:
    """Largest number of trading bars a date range could contain.

    Returns ``None`` when either endpoint is unparsable, leaving the range to
    whatever validation already handles malformed dates.
    """
    start = _parse_date(start_date)
    end = _parse_date(end_date)
    if start is None or end is None:
        return None

    calendar_days = (end - start).days + 1
    if calendar_days <= 0:
        return 0
    return math.ceil(calendar_days * _TRADING_DAYS_PER_WEEK / _CALENDAR_DAYS_PER_WEEK)


def minimum_calendar_days(min_bars: int) -> int:
    """Shortest window that could supply ``min_bars`` trading days."""
    return math.ceil(min_bars * _CALENDAR_DAYS_PER_WEEK / _TRADING_DAYS_PER_WEEK)


def load_min_bars_map(collections: Dict[str, Any]) -> Dict[str, int]:
    """Read per-strategy bar requirements published by quant-strategies.

    Returns an empty map when the metadata is unavailable, which disables the
    pre-check rather than blocking submissions on a bookkeeping collection.
    """
    collection = (collections or {}).get("strategy_metadata")
    if collection is None:
        return {}

    try:
        docs = collection.find({}, {"_id": 0, "key": 1, "min_bars": 1})
        return {
            doc["key"]: int(doc["min_bars"])
            for doc in docs
            if doc.get("key") and isinstance(doc.get("min_bars"), int)
        }
    except Exception as exc:
        log.warning("Skipping backtest bar pre-check, strategy_metadata unreadable: %s", exc)
        return {}


def validate_backtest_window(
    collections: Dict[str, Any],
    strategy_keys: Iterable[str],
    start_date: Any,
    end_date: Any,
) -> None:
    """Raise 422 if the window is too short for any of the given strategies."""
    available = estimate_max_trading_bars(start_date, end_date)
    if available is None:
        return

    min_bars_map = load_min_bars_map(collections)
    if not min_bars_map:
        return

    for strategy_key in dict.fromkeys(strategy_keys):
        min_bars = min_bars_map.get(strategy_key)
        if not min_bars or available >= min_bars:
            continue
        raise HTTPException(
            status_code=422,
            detail=(
                f"Date range {start_date}-{end_date} is too short for strategy "
                f"'{strategy_key}': it needs at least {min_bars} trading bars to "
                f"warm up its indicators, but the range holds at most {available}. "
                f"Use a span of at least {minimum_calendar_days(min_bars)} calendar days."
            ),
        )
