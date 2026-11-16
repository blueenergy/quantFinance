"""True 分时线 payload from ``minute_bars`` 1m rows.

A 分时图 is not a 1-minute candlestick chart: it is one price point per minute
plus an average-price line. Rules used here:

- canonical A-share slots: 09:30–11:30 and 13:01–15:00 (QMT labels the first
  afternoon bar 13:01; there is no 13:00 bar) — 241 slots per session;
- price = each minute's close;
- 均价 = Σamount / Σ(volume × 100) — volume is in 手, amount in 元, so this is
  the exact VWAP, not an approximation;
- pct = (price − pre_close) / pre_close × 100;
- slots without a bar stay ``None`` so an in-progress session stops at "now"
  and a halted stock shows a gap instead of a fake flat line.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

MORNING_START = 9 * 60 + 30
MORNING_END = 11 * 60 + 30
AFTERNOON_START = 13 * 60 + 1
AFTERNOON_END = 15 * 60

SLOT_COUNT = (MORNING_END - MORNING_START + 1) + (AFTERNOON_END - AFTERNOON_START + 1)


def _slot_label(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def time_slots() -> List[str]:
    morning = [_slot_label(m) for m in range(MORNING_START, MORNING_END + 1)]
    afternoon = [_slot_label(m) for m in range(AFTERNOON_START, AFTERNOON_END + 1)]
    return morning + afternoon


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


def _hhmm(trade_date: Any) -> str:
    digits = "".join(ch for ch in str(trade_date or "") if ch.isdigit())
    if len(digits) < 12:
        return ""
    return f"{digits[8:10]}:{digits[10:12]}"


def build_intraday_line(
    minute_rows: Iterable[Dict[str, Any]],
    pre_close: Optional[float],
    *,
    digits: int = 3,
) -> Dict[str, Any]:
    """Return ``{times, bars, summary}`` for one session."""
    rows_by_time: Dict[str, Dict[str, Any]] = {}
    for row in minute_rows:
        key = _hhmm(row.get("trade_date"))
        if key:
            rows_by_time[key] = row

    slots = time_slots()
    cum_amount = 0.0
    cum_shares = 0.0
    bars: List[Dict[str, Any]] = []
    for slot in slots:
        row = rows_by_time.get(slot)
        if not row:
            bars.append(
                {"time": slot, "price": None, "avg": None, "volume": 0.0, "amount": 0.0, "pct": None}
            )
            continue
        price = _num(row.get("close"))
        volume = _num(row.get("volume")) or 0.0
        amount = _num(row.get("amount")) or 0.0
        cum_amount += amount
        cum_shares += volume * 100.0
        avg = (cum_amount / cum_shares) if cum_shares > 0 else price
        pct = (
            (price - pre_close) / pre_close * 100.0
            if price is not None and pre_close not in (None, 0)
            else None
        )
        bars.append(
            {
                "time": slot,
                "price": None if price is None else round(price, digits),
                "avg": None if avg is None else round(avg, digits),
                "volume": volume,
                "amount": amount,
                "pct": None if pct is None else round(pct, 2),
            }
        )

    traded = [bar for bar in bars if bar["price"] is not None]
    prices = [bar["price"] for bar in traded]
    last = traded[-1] if traded else None
    summary = {
        "last": last["price"] if last else None,
        "pct": last["pct"] if last else None,
        "avg": last["avg"] if last else None,
        "high": max(prices) if prices else None,
        "low": min(prices) if prices else None,
        "volume": round(sum(bar["volume"] for bar in traded), 2),
        "amount": round(sum(bar["amount"] for bar in traded), 2),
        "count": len(traded),
    }
    return {"times": slots, "bars": bars, "summary": summary}
