"""Daily -> weekly/monthly K-line aggregation shared by stock and ETF.

One rule for every K-line surface (ETF 淘金 / 自选 / 工作台):

- bucket by ISO week (``1w``) or calendar month (``1m``)
- the bar's ``trade_date`` is the **last trading day inside the bucket**, never
  a pandas period label (``W-FRI`` / ``ME`` would emit future or non-trading
  dates such as ``20261130``);
- open = first day's open, high = max, low = min, close = last day's close;
- ``vol`` / ``volume`` / ``amount`` summed; identity fields (name, fd_share,
  adjust flags) come from the last day in the bucket;
- ``pre_close`` = previous bucket's close, ``pct_chg`` = close vs that close
  (percent). The newest bucket is the in-progress week/month.

Prices must already be in the caller's adjust口径 (qfq/hfq/none) before
aggregation; this module never reads adj_factor itself.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

TF_WEEK = "1w"
TF_MONTH = "1m"
VALID_TF = ("1d", TF_WEEK, TF_MONTH)

_SUM_COLS = ("vol", "volume", "amount")


def normalize_tf(tf: Any) -> str:
    tfc = str(tf or "1d").strip().lower()
    if tfc in ("d", "day"):
        return "1d"
    if tfc in ("w", "week"):
        return TF_WEEK
    if tfc in ("m", "month"):
        return TF_MONTH
    return tfc


def ymd(value: Any) -> str:
    text = str(value or "").replace("-", "").strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits[:8] if len(digits) >= 8 else ""


def period_key(trade_date: Any, tf: str) -> str:
    day = ymd(trade_date)
    if not day:
        return ""
    if tf == TF_MONTH:
        return day[:6]
    try:
        return datetime.strptime(day, "%Y%m%d").strftime("%G-W%V")
    except ValueError:
        return day[:6]


def _num(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        from bson.decimal128 import Decimal128

        if isinstance(value, Decimal128):
            value = float(str(value))
    except Exception:
        pass
    if hasattr(value, "item"):
        try:
            value = value.item()
        except Exception:
            pass
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


def aggregate_rows(rows: Sequence[Dict[str, Any]], tf: str) -> List[Dict[str, Any]]:
    """Aggregate price-adjusted daily rows (any order) into bars, newest first."""
    tfc = normalize_tf(tf)
    if tfc not in (TF_WEEK, TF_MONTH) or not rows:
        return [dict(row) for row in rows]

    ordered = sorted(
        (dict(row) for row in rows),
        key=lambda row: ymd(row.get("trade_date")),
    )
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for row in ordered:
        key = period_key(row.get("trade_date"), tfc)
        if key:
            buckets.setdefault(key, []).append(row)

    bars: List[Dict[str, Any]] = []
    for key in sorted(buckets):
        group = buckets[key]
        first, last = group[0], group[-1]
        bar = dict(last)
        bar["trade_date"] = ymd(last.get("trade_date"))
        bar["open"] = _num(first.get("open"))
        highs = [v for v in (_num(r.get("high")) for r in group) if v is not None]
        lows = [v for v in (_num(r.get("low")) for r in group) if v is not None]
        bar["high"] = max(highs) if highs else None
        bar["low"] = min(lows) if lows else None
        bar["close"] = _num(last.get("close"))
        for col in _SUM_COLS:
            if col not in group[0] and col not in last:
                continue
            values = [v for v in (_num(r.get(col)) for r in group) if v is not None]
            bar[col] = sum(values) if values else None
        prev_close = _num(bars[-1].get("close")) if bars else None
        bar["pre_close"] = prev_close
        close = bar["close"]
        bar["pct_chg"] = (
            (close - prev_close) / prev_close * 100.0
            if close is not None and prev_close not in (None, 0)
            else None
        )
        bars.append(bar)
    return list(reversed(bars))


def fill_pre_close(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Stamp ``pre_close`` / ``pct_chg`` on daily rows (newest-first input ok)."""
    out = [dict(row) for row in rows]
    ordered = sorted(out, key=lambda row: ymd(row.get("trade_date")))
    prev_close: Optional[float] = None
    for row in ordered:
        close = _num(row.get("close"))
        row["pre_close"] = prev_close
        row["pct_chg"] = (
            (close - prev_close) / prev_close * 100.0
            if close is not None and prev_close not in (None, 0)
            else None
        )
        if close is not None:
            prev_close = close
    return out
