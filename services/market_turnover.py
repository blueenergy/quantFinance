"""Volume waterline: turnover relative to its own 20-day mean.

Two series feed the panel:

* daily  — ``market_turnover_daily``: one row per trading day with the five
  mutually exclusive boards plus the ETF split by fund type.
* intraday — ``market_turnover_intraday``: 2-minute snapshots of today's
  cumulative stock turnover per board. Rows backfilled from the Shenwan sector
  aggregate carry ``coverage="total_only"`` and a market-wide ``total`` only.

The intraday comparison has to normalise for the time of day: by 10:00 only
about a third of the session's turnover has happened, so comparing a 10:00
cumulative against a full-day mean would call every morning a shrink. The
observed cumulative fraction of the day at each clock time is what
``build_intraday_profile`` returns.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence

MA_WINDOW = 20
#: Trading minutes grid used for the time-of-day profile: 09:30-11:30, 13:00-15:00.
PROFILE_GRID: List[str] = [
    f"{hour:02d}{minute:02d}"
    for hour, start, end in ((9, 30, 59), (10, 0, 59), (11, 0, 30), (13, 0, 59), (14, 0, 59))
    for minute in range(start, end + 1, 2)
] + ["1500"]

BOARDS: List[Dict[str, str]] = [
    {"key": "sh", "name": "沪市主板"},
    {"key": "sz", "name": "深市主板"},
    {"key": "cx", "name": "创业板"},
    {"key": "star", "name": "科创板"},
    {"key": "bse", "name": "北交所"},
]

ETF_TYPES: List[Dict[str, str]] = [
    {"key": "equity", "name": "股票型"},
    {"key": "bond", "name": "债券型"},
    {"key": "money", "name": "货币型"},
]


def to_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return number


def _percentile(sorted_values: Sequence[float], q: float) -> Optional[float]:
    if not sorted_values:
        return None
    idx = (len(sorted_values) - 1) * q
    lower = int(idx)
    upper = min(lower + 1, len(sorted_values) - 1)
    if lower == upper:
        return sorted_values[lower]
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * (idx - lower)


def attach_ma_and_ratio(rows: List[Dict[str, Any]], keys: Iterable[str], window: int = MA_WINDOW) -> List[Dict[str, Any]]:
    """Add ``{key}_ma{window}`` and ``{key}_ratio`` to each row, oldest first.

    ``ratio`` is the row's value divided by the mean of the *previous* ``window``
    rows, so a row is never compared against a mean that contains itself.
    """
    keys = list(keys)
    for key in keys:
        values = [to_float(row.get(key)) for row in rows]
        for index, row in enumerate(rows):
            if index < window:
                row[f"{key}_ma{window}"] = None
                row[f"{key}_ratio"] = None
                continue
            window_values = [value for value in values[index - window : index] if value is not None]
            if len(window_values) < window:
                row[f"{key}_ma{window}"] = None
                row[f"{key}_ratio"] = None
                continue
            mean = sum(window_values) / len(window_values)
            row[f"{key}_ma{window}"] = round(mean, 2)
            current = values[index]
            row[f"{key}_ratio"] = round(current / mean, 4) if mean and current is not None else None
    return rows


def serialize_daily_row(doc: Dict[str, Any]) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "trade_date": doc.get("trade_date"),
        "stock_total": to_float(doc.get("stock_total")),
        "stock_count": doc.get("stock_count"),
        "etf_total": to_float(doc.get("etf_total")),
        "etf_count": doc.get("etf_count"),
        "etf_available": bool(doc.get("etf_available", False)),
    }
    for board in BOARDS:
        row[board["key"]] = to_float(doc.get(board["key"]))
    for etf in ETF_TYPES:
        row[f"etf_{etf['key']}"] = to_float(doc.get(f"etf_{etf['key']}"))
    return row


def cumulative_at_or_before(times: Sequence[str], values: Sequence[Optional[float]], bucket: str) -> Optional[float]:
    """Last cumulative value at or before *bucket* (times are ``HHMM`` strings)."""
    found: Optional[float] = None
    for moment, value in zip(times, values):
        if moment <= bucket:
            if value is not None:
                found = value
        else:
            break
    return found


def build_intraday_profile(
    day_series: Dict[str, List[Dict[str, Any]]],
    grid: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """Distribution of the cumulative fraction of the day at each clock time.

    ``day_series`` maps ``trade_date`` -> rows sorted by ``snapshot_time``, each
    row carrying ``t`` (HHMM) and ``total``. Days whose last snapshot is far
    short of the day's final total would distort the fractions; the caller
    passes only complete days.
    """
    grid = list(grid or PROFILE_GRID)
    profile: List[Dict[str, Any]] = []
    for bucket in grid:
        fractions: List[float] = []
        for rows in day_series.values():
            if not rows:
                continue
            end_of_day = to_float(rows[-1].get("total"))
            if not end_of_day or end_of_day <= 0:
                continue
            cumulative = cumulative_at_or_before([r["t"] for r in rows], [r.get("total") for r in rows], bucket)
            if cumulative is None:
                continue
            fractions.append(cumulative / end_of_day)
        if len(fractions) < 3:
            continue
        fractions.sort()
        profile.append(
            {
                "t": bucket,
                "p10": round(_percentile(fractions, 0.10), 4),
                "p50": round(_percentile(fractions, 0.50), 4),
                "p90": round(_percentile(fractions, 0.90), 4),
                "n": len(fractions),
            }
        )
    return profile


def profile_fraction(profile: Sequence[Dict[str, Any]], bucket: str) -> Optional[Dict[str, Any]]:
    """Profile entry whose clock time is at or before *bucket*."""
    found = None
    for entry in profile:
        if entry["t"] <= bucket:
            found = entry
        else:
            break
    return found


def project_full_day(cumulative: Optional[float], fraction: Optional[float]) -> Optional[float]:
    """Scale today's cumulative to a full-day estimate using the observed fraction."""
    if cumulative is None or not fraction or fraction <= 0:
        return None
    return round(cumulative / fraction, 2)
