"""National-team ETF monitoring.

Computes an intraday "is the national team buying?" snapshot from
``quant_data.minute_bars`` (1m bars) and ``quant_data.etf_shares``.

The signal is deliberately evidence-based rather than a single score:

- same-time volume ratio: today's cumulative amount / 20-day median at the
  same minute (never full-day average, which makes every morning look quiet);
- breadth: how many broad-index ETFs are above 2.0x / 2.5x at the same time;
- tail concentration: 14:30-15:00 share of the day's amount, weighted across
  the pool, versus the 20-day median;
- share confirmation: latest ``fund_share`` change (T+1) to separate buying
  from redemptions like the 2026-01 episode.

Everything is read-only.  The service is self-contained (pymongo + stdlib) so
it can be exercised against today's data without booting the API.
"""
from __future__ import annotations

import bisect
import statistics
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

# 2025 annual report top-10 holders (see llm-wiki national-team note).
# This is the fallback pool; data_db.etf_national_team_pool overrides it.
POOL: List[Dict[str, Any]] = [
    {"ts_code": "510300.SH", "name": "华泰柏瑞沪深300ETF", "index": "沪深300", "tier": "confirmed", "holder": "汇金资管 42.62% / 汇金投资 40.14%", "holder_pct": 82.76},
    {"ts_code": "510310.SH", "name": "易方达沪深300ETF", "index": "沪深300", "tier": "confirmed", "holder": "汇金资管 43.34% / 汇金投资 41.54%", "holder_pct": 85.27},
    {"ts_code": "510330.SH", "name": "华夏沪深300ETF", "index": "沪深300", "tier": "confirmed", "holder": "汇金资管 44.47% / 汇金投资 44.15%", "holder_pct": 88.62},
    {"ts_code": "159919.SZ", "name": "嘉实沪深300ETF", "index": "沪深300", "tier": "confirmed", "holder": "汇金投资 47.88% / 汇金资管 39.09%", "holder_pct": 87.27},
    {"ts_code": "510050.SH", "name": "华夏上证50ETF", "index": "上证50", "tier": "confirmed", "holder": "汇金投资 50.81% / 汇金资管 35.24%", "holder_pct": 86.05},
    {"ts_code": "510500.SH", "name": "南方中证500ETF", "index": "中证500", "tier": "confirmed", "holder": "汇金投资 43.20% / 汇金资管 31.38%", "holder_pct": 75.58},
    {"ts_code": "512500.SH", "name": "华夏中证500ETF", "index": "中证500", "tier": "confirmed", "holder": "汇金投资 47.26% / 汇金资管单一计划 18.50%", "holder_pct": 65.76},
    {"ts_code": "159922.SZ", "name": "嘉实中证500ETF", "index": "中证500", "tier": "confirmed", "holder": "汇金投资 44.32%", "holder_pct": 44.32},
    {"ts_code": "512100.SH", "name": "南方中证1000ETF", "index": "中证1000", "tier": "confirmed", "holder": "汇金资管 51.51% / 汇金投资 34.92%", "holder_pct": 86.43},
    {"ts_code": "159845.SZ", "name": "华夏中证1000ETF", "index": "中证1000", "tier": "confirmed", "holder": "汇金资管 56.04% / 汇金投资 30.11%", "holder_pct": 86.15},
    {"ts_code": "560010.SH", "name": "广发中证1000ETF", "index": "中证1000", "tier": "confirmed", "holder": "汇金资管 58.77% / 汇金投资 33.09%", "holder_pct": 91.86},
    {"ts_code": "588080.SH", "name": "易方达科创50ETF", "index": "科创50", "tier": "confirmed", "holder": "汇金投资 22.92% / 汇金资管 19.03%", "holder_pct": 41.95},
    {"ts_code": "588050.SH", "name": "工银瑞信科创50ETF", "index": "科创50", "tier": "confirmed", "holder": "汇金资管 12.42% / 汇金投资 11.78%", "holder_pct": 24.20},
    {"ts_code": "159915.SZ", "name": "易方达创业板ETF", "index": "创业板", "tier": "confirmed", "holder": "汇金投资 36.07% / 汇金资管 17.96%", "holder_pct": 54.03},
    {"ts_code": "159952.SZ", "name": "广发创业板ETF", "index": "创业板", "tier": "confirmed", "holder": "汇金投资 15.43% / 诚通金控 0.85%", "holder_pct": 16.28},
    {"ts_code": "159977.SZ", "name": "天弘创业板ETF", "index": "创业板", "tier": "confirmed", "holder": "汇金投资 8.94%", "holder_pct": 8.94},
]

INDEX_NAMES = {
    "000300.SH": "沪深300",
    "000905.SH": "中证500",
    "000852.SH": "中证1000",
    "000688.SH": "科创50",
}

LEVEL_LABELS = {
    "calm": "平静",
    "watch": "观察",
    "suspect": "疑似",
    "high": "高置信",
}

# Net primary-market share flow, AUM-weighted.  +/-1% of prior pool AUM is the
# same threshold used by the per-ETF share confirmation, so a day can only be
# "high" (buy) when the direction is inflow.
SHARE_DIRECTION_LABELS = {
    "inflow": "净申购",
    "outflow": "净赎回",
    "neutral": "中性",
    "unknown": "未知",
}


def _share_direction(flow: Optional[float]) -> str:
    if flow is None:
        return "unknown"
    if flow >= 1.0:
        return "inflow"
    if flow <= -1.0:
        return "outflow"
    return "neutral"

_CTX_TTL_SECONDS = 30.0
_CTX_LOCK = threading.Lock()
_CTX_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}


def _num(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _median(values: Sequence[float]) -> Optional[float]:
    clean = [float(v) for v in values if v is not None]
    return statistics.median(clean) if clean else None


def _quantile(values: Sequence[float], q: float) -> Optional[float]:
    clean = sorted(float(v) for v in values if v is not None)
    if not clean:
        return None
    if len(clean) == 1:
        return clean[0]
    pos = (len(clean) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(clean) - 1)
    frac = pos - lo
    return clean[lo] * (1 - frac) + clean[hi] * frac


def _norm_day(value: Any) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits[:8]


def _norm_time(value: Any) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) >= 12:
        return digits[8:12]
    if len(digits) >= 4:
        return digits[-4:]
    return ""


def _hm(trade_date: str) -> str:
    return str(trade_date)[8:12]


def _label_level(level: str) -> str:
    return LEVEL_LABELS.get(level, level)


def load_pool(data_db: Any) -> List[Dict[str, Any]]:
    """Return the enabled pool from Mongo, falling back to the built-in list."""
    try:
        docs = list(
            data_db["etf_national_team_pool"].find(
                {"enabled": {"$ne": False}}, {"_id": 0}
            )
        )
    except Exception:
        docs = []
    if not docs:
        return [dict(item) for item in POOL]
    out: List[Dict[str, Any]] = []
    for doc in docs:
        code = str(doc.get("ts_code") or "").strip().upper()
        if not code:
            continue
        out.append(
            {
                "ts_code": code,
                "name": doc.get("name") or code,
                "index": doc.get("index") or doc.get("index_name") or "-",
                "tier": doc.get("tier") or "confirmed",
                "holder": doc.get("holder") or doc.get("holder_label") or "",
                "holder_pct": _num(doc.get("holder_pct")),
            }
        )
    return out


_DATES_TTL_SECONDS = 60.0
_DATES_LOCK = threading.Lock()
_DATES_CACHE: Dict[str, Tuple[float, List[str]]] = {}


def list_trade_dates(data_db: Any, limit: int = 60) -> List[str]:
    """Trading dates where *all* pool ETFs have 1m bars.

    Used by the dashboard date picker.  The aggregation is bounded to the last
    400 calendar days (``limit`` caps it further, max 240 sessions) and cached
    for a minute.  The QMT backfill now covers 2025-12-01 onward; dates before
    that are absent until backfilled.
    """
    pool = load_pool(data_db)
    codes = [item["ts_code"] for item in pool]
    if not codes:
        return []
    key = f"{id(data_db)}:{int(limit)}"
    now = time.monotonic()
    with _DATES_LOCK:
        cached = _DATES_CACHE.get(key)
        if cached and now - cached[0] < _DATES_TTL_SECONDS:
            return list(cached[1])
    cutoff = (datetime.now() - timedelta(days=400)).strftime("%Y%m%d") + "0000"
    pipeline = [
        {"$match": {"symbol": {"$in": codes}, "trade_date": {"$gte": cutoff}}},
        {
            "$group": {
                "_id": {"$substrBytes": ["$trade_date", 0, 8]},
                "symbols": {"$addToSet": "$symbol"},
            }
        },
        {"$match": {"symbols": {"$size": len(codes)}}},
        {"$sort": {"_id": -1}},
        {"$limit": int(limit)},
    ]
    dates = sorted(
        str(row["_id"]) for row in data_db["minute_bars"].aggregate(pipeline)
    )
    with _DATES_LOCK:
        _DATES_CACHE[key] = (now, dates)
    return dates


def _latest_trade_date(data_db: Any, codes: Sequence[str]) -> Optional[str]:
    doc = data_db["minute_bars"].find_one(
        {"symbol": {"$in": list(codes)}},
        {"trade_date": 1},
        sort=[("trade_date", -1)],
    )
    if not doc:
        return None
    return _norm_day(doc.get("trade_date"))


def _trading_days(data_db: Any, codes: Sequence[str], today: str) -> List[str]:
    raw = data_db["etf_daily"].distinct(
        "trade_date",
        {"ts_code": {"$in": list(codes)}, "trade_date": {"$lte": today}},
    )
    days = sorted({_norm_day(x) for x in raw if _norm_day(x)})
    if today not in days:
        days.append(today)
    days = sorted(set(days))
    if days:
        return days[-21:]
    # Fallback: derive from minute bars (only when etf_daily is unavailable).
    fallback = set()
    for doc in data_db["minute_bars"].find(
        {"symbol": {"$in": list(codes)}, "trade_date": {"$lte": today + "2359"}},
        {"trade_date": 1, "_id": 0},
    ):
        day = _norm_day(doc.get("trade_date"))
        if day:
            fallback.add(day)
    return sorted(fallback)[-21:]


def _load_bars(
    data_db: Any, codes: Sequence[str], days: Sequence[str]
) -> Dict[str, Dict[str, Dict[str, Tuple[float, float]]]]:
    out: Dict[str, Dict[str, Dict[str, Tuple[float, float]]]] = defaultdict(
        lambda: defaultdict(dict)
    )
    if not days:
        return out
    start = days[0] + "0000"
    end = days[-1] + "2359"
    cursor = data_db["minute_bars"].find(
        {
            "symbol": {"$in": list(codes)},
            "trade_date": {"$gte": start, "$lte": end},
        },
        {"symbol": 1, "trade_date": 1, "amount": 1, "close": 1, "_id": 0},
    )
    for doc in cursor:
        td = str(doc.get("trade_date") or "")
        day = td[:8]
        hm = td[8:12]
        if not day or not hm:
            continue
        amount = _num(doc.get("amount")) or 0.0
        close = _num(doc.get("close")) or 0.0
        out[doc["symbol"]][day][hm] = (amount, close)
    return out


def _load_daily_closes(
    data_db: Any, codes: Sequence[str], days: Sequence[str]
) -> Dict[str, Dict[str, float]]:
    out: Dict[str, Dict[str, float]] = defaultdict(dict)
    if not days:
        return out
    for doc in data_db["etf_daily"].find(
        {
            "ts_code": {"$in": list(codes)},
            "trade_date": {"$gte": days[0], "$lte": days[-1]},
        },
        {"ts_code": 1, "trade_date": 1, "close": 1, "_id": 0},
    ):
        close = _num(doc.get("close"))
        if close:
            out[doc["ts_code"]][_norm_day(doc.get("trade_date"))] = close
    return out


def _load_shares(data_db: Any, codes: Sequence[str], today: str) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for code in codes:
        rows = list(
            data_db["etf_shares"]
            .find(
                {"ts_code": code, "trade_date": {"$lte": today}},
                {"trade_date": 1, "fd_share": 1, "_id": 0},
            )
            .sort("trade_date", -1)
            .limit(6)
        )
        values = [
            (str(row.get("trade_date")), _num(row.get("fd_share")))
            for row in rows
            if _num(row.get("fd_share"))
        ]
        chg1 = (
            (values[0][1] / values[1][1] - 1) * 100
            if len(values) > 1 and values[1][1]
            else None
        )
        chg5 = (
            (values[0][1] / values[5][1] - 1) * 100
            if len(values) > 5 and values[5][1]
            else None
        )
        out[code] = {
            "date": values[0][0] if values else None,
            "chg1": chg1,
            "chg5": chg5,
            "latest": values[0][1] if values else None,
            "prev": values[1][1] if len(values) > 1 else None,
            "prev_date": values[1][0] if len(values) > 1 else None,
        }
    return out


_HISTORY_TTL_SECONDS = 60.0
_HISTORY_LOCK = threading.Lock()
_HISTORY_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}


def _load_share_series(
    data_db: Any, codes: Sequence[str], start_day: str
) -> Dict[str, Tuple[List[str], List[float]]]:
    """Ascending (dates, fd_share) per ETF, for point-in-time share changes."""
    out: Dict[str, Tuple[List[str], List[float]]] = {}
    for code in codes:
        rows = list(
            data_db["etf_shares"]
            .find(
                {"ts_code": code, "trade_date": {"$gte": start_day}},
                {"trade_date": 1, "fd_share": 1, "_id": 0},
            )
            .sort("trade_date", 1)
        )
        dates: List[str] = []
        values: List[float] = []
        for row in rows:
            value = _num(row.get("fd_share"))
            if value:
                dates.append(str(row.get("trade_date")))
                values.append(value)
        out[code] = (dates, values)
    return out


def _share_changes(
    series: Tuple[List[str], List[float]], day: str
) -> Tuple[
    Optional[float],
    Optional[float],
    Optional[float],
    Optional[float],
    Optional[str],
    Optional[str],
]:
    """(chg1, chg5, latest, prev, prev_date, row_date) for the latest row <= day.

    ``row_date`` is the trade_date of the share row actually used; it is older
    than ``day`` whenever that day's fund_share has not been published yet
    (T+1), which callers must not present as same-day data.
    """
    dates, values = series
    idx = bisect.bisect_right(dates, day) - 1
    if idx < 0 or not values[idx]:
        return None, None, None, None, None, None
    current = values[idx]
    chg1 = (
        (current / values[idx - 1] - 1) * 100
        if idx >= 1 and values[idx - 1]
        else None
    )
    chg5 = (
        (current / values[idx - 5] - 1) * 100
        if idx >= 5 and values[idx - 5]
        else None
    )
    prev = values[idx - 1] if idx >= 1 else None
    prev_date = dates[idx - 1] if idx >= 1 else None
    return chg1, chg5, current, prev, prev_date, dates[idx]


def _pool_day_stats(
    bars: Mapping[str, Mapping[str, Mapping[str, Tuple[float, float]]]],
    codes: Sequence[str],
    day: str,
    as_of: str,
) -> Tuple[Dict[str, float], Dict[str, float], str]:
    """Per-code cumulative / tail amount up to ``as_of`` plus the last bar time."""
    cum: Dict[str, float] = {}
    tail: Dict[str, float] = {}
    last_hm = ""
    for code in codes:
        day_map = bars.get(code, {}).get(day) or {}
        total = 0.0
        tail_total = 0.0
        for hm, (amount, _close) in day_map.items():
            if hm > as_of:
                continue
            total += amount
            if "1430" < hm <= as_of:
                tail_total += amount
            if hm > last_hm:
                last_hm = hm
        cum[code] = total
        tail[code] = tail_total
    return cum, tail, last_hm


def compute_history(
    data_db: Any,
    as_of: Optional[str] = None,
    limit: int = 60,
) -> Dict[str, Any]:
    """Daily national-team signal series at a fixed intraday time.

    Every row uses the same definition as :func:`compute_snapshot`, so the
    history reconciles with the cross-section shown in the dashboard.  The
    first 20 covered sessions only have a partial baseline; ``baseline_days``
    exposes how many sessions were actually available.
    """
    pool = load_pool(data_db)
    codes = [item["ts_code"] for item in pool]
    if not codes:
        raise ValueError("national-team pool is empty")
    as_of_hm = _norm_time(as_of) or "1500"
    if as_of_hm < "0930" or as_of_hm > "1500":
        raise ValueError(f"as_of {as_of_hm} is outside the trading session")
    limit = max(1, int(limit))
    cache_key = f"{id(data_db)}:{as_of_hm}:{limit}"
    now = time.monotonic()
    with _HISTORY_LOCK:
        cached = _HISTORY_CACHE.get(cache_key)
        if cached and now - cached[0] < _HISTORY_TTL_SECONDS:
            return cached[1]

    # The dashboard can only replay full-coverage sessions; the baseline may
    # still use the older single-ETF sessions, matching compute_snapshot().
    covered_dates = list_trade_dates(data_db, limit=min(240, limit + 20))
    if not covered_dates:
        raise ValueError("no minute_bars data for the national-team pool")
    out_dates = covered_dates[-limit:]
    latest = covered_dates[-1]
    raw_calendar = data_db["etf_daily"].distinct(
        "trade_date",
        {"ts_code": {"$in": codes}, "trade_date": {"$lte": latest}},
    )
    calendar = sorted({_norm_day(x) for x in raw_calendar if _norm_day(x)})
    if not calendar:
        calendar = list(covered_dates)
    calendar = sorted(set(calendar) | set(out_dates))
    earliest = out_dates[0]
    calendar = [d for d in calendar if d < earliest][-20:] + [
        d for d in calendar if d >= earliest
    ]
    bars = _load_bars(data_db, codes, calendar)
    present = {
        day: {code for code in codes if bars.get(code, {}).get(day)}
        for day in calendar
    }
    stats = {
        day: _pool_day_stats(bars, codes, day, as_of_hm)
        for day in calendar
        if present.get(day)
    }
    share_start = (
        datetime.strptime(calendar[0], "%Y%m%d") - timedelta(days=40)
    ).strftime("%Y%m%d")
    share_series = _load_share_series(data_db, codes, share_start)
    daily_close = _load_daily_closes(
        data_db, codes, sorted({share_start, *calendar})
    )

    rows: List[Dict[str, Any]] = []
    for day in out_dates:
        if day not in stats:
            continue
        hist_days = [d for d in calendar if d < day and d in stats][-20:]
        baseline_days = min(
            (
                sum(1 for h in hist_days if code in present.get(h, set()))
                for code in codes
            ),
            default=0,
        )
        cum, tail, last_hm = stats[day]
        pool_cur = 0.0
        pool_base = 0.0
        breadth2 = 0
        breadth25 = 0
        share1_sig_values: List[float] = []  # level check: latest known share
        share1_values: List[float] = []  # display: same-day share only
        share5_values: List[float] = []
        share_dates: List[str] = []
        share_covered = 0
        flow_delta = 0.0
        flow_base = 0.0
        flow_abs = 0.0
        for code in codes:
            cur = cum.get(code) or 0.0
            base = _median(
                [
                    stats[h][0].get(code) or 0.0
                    for h in hist_days
                    if code in present.get(h, set())
                ]
            )
            ratio = cur / base if base else 0.0
            pool_cur += cur
            pool_base += base or 0.0
            if ratio >= 2.0:
                breadth2 += 1
            if ratio >= 2.5:
                breadth25 += 1
            (
                share1,
                share5,
                latest_share,
                prev_share,
                prev_share_date,
                share_row_date,
            ) = _share_changes(share_series.get(code, ([], [])), day)
            if share1 is not None:
                share1_sig_values.append(share1)
            if share_row_date:
                share_dates.append(share_row_date)
            # Share confirmation is T+1: on the day itself (and until the next
            # session's sync) the latest row belongs to an older trade date.  It
            # still feeds the level check exactly as before, but it must not be
            # displayed as this day's share change.
            if share_row_date == day:
                share_covered += 1
                if share1 is not None:
                    share1_values.append(share1)
                if share5 is not None:
                    share5_values.append(share5)
                weight_close = (
                    daily_close.get(code, {}).get(prev_share_date)
                    if prev_share_date
                    else None
                )
                if latest_share and prev_share and weight_close:
                    base_aum = prev_share * weight_close
                    delta_aum = (latest_share - prev_share) * weight_close
                    flow_base += base_aum
                    flow_delta += delta_aum
                    flow_abs += abs(delta_aum)

        if as_of_hm > "1430" and pool_cur:
            day_tail_share = sum(tail.values()) / pool_cur
        else:
            day_tail_share = None
        tail_baselines: List[float] = []
        if as_of_hm > "1430":
            for hist_day in hist_days:
                h_cum, h_tail, _ = stats[hist_day]
                h_cur = sum(h_cum.values())
                if h_cur:
                    tail_baselines.append(sum(h_tail.values()) / h_cur)
        tail_baseline = _median(tail_baselines)
        tail_ratio = (
            day_tail_share / tail_baseline
            if day_tail_share is not None and tail_baseline
            else None
        )

        avg_share1_sig = (
            statistics.mean(share1_sig_values) if share1_sig_values else None
        )
        avg_share1 = statistics.mean(share1_values) if share1_values else None
        avg_share5 = statistics.mean(share5_values) if share5_values else None
        pool_flow = (flow_delta / flow_base * 100) if flow_base else None
        share_rotation = (flow_abs / abs(flow_delta)) if flow_delta else None
        known_share_date = max(share_dates) if share_dates else None
        share_stale = bool(known_share_date and known_share_date < day)
        share_direction = _share_direction(pool_flow)
        if (
            breadth25 >= 3
            and avg_share1_sig is not None
            and avg_share1_sig >= 1
            and tail_ratio is not None
            and tail_ratio >= 1.3
        ):
            level = "high"
        elif breadth25 >= 3:
            level = "suspect"
        elif breadth2 >= 2:
            level = "watch"
        else:
            level = "calm"

        rows.append(
            {
                "trade_date": day,
                "level": level,
                "level_label": _label_level(level),
                "pool_ratio": (pool_cur / pool_base) if pool_base else None,
                "amount_yuan": pool_cur,
                "baseline_yuan": pool_base,
                "breadth_2x": breadth2,
                "breadth_25x": breadth25,
                "tail_ratio": tail_ratio,
                "share_chg_1d": avg_share1,
                "share_chg_5d": avg_share5,
                "pool_share_flow": pool_flow,
                "share_rotation": share_rotation,
                "direction": share_direction,
                "direction_label": SHARE_DIRECTION_LABELS[share_direction],
                "share_date": known_share_date,
                "share_stale": share_stale,
                "share_covered": share_covered,
                "share_total": len(codes),
                "baseline_days": baseline_days,
                "partial": bool(last_hm and last_hm < as_of_hm),
            }
        )

    payload = {
        "success": True,
        "as_of": as_of_hm,
        "dates": out_dates,
        "rows": rows,
    }
    with _HISTORY_LOCK:
        _HISTORY_CACHE[cache_key] = (now, payload)
    return payload


def _context_key(data_db: Any, trade_date: Optional[str]) -> str:
    return f"{id(data_db)}:{trade_date or 'latest'}"


def _build_context(data_db: Any, trade_date: Optional[str]) -> Dict[str, Any]:
    pool = load_pool(data_db)
    codes = [item["ts_code"] for item in pool]
    latest = _latest_trade_date(data_db, codes)
    today = _norm_day(trade_date) or latest
    if not today:
        raise ValueError("no minute_bars data for the national-team pool")
    days = _trading_days(data_db, codes, today)
    if today not in days:
        days = sorted(set(days + [today]))[-21:]
    hist_days = [day for day in days if day < today][-20:]
    bars = _load_bars(data_db, codes, days)
    if not bars.get(codes[0], {}).get(today) and not any(
        bars.get(code, {}).get(today) for code in codes
    ):
        raise ValueError(f"no minute_bars data for {today}")
    times = sorted(
        {
            hm
            for code in codes
            for hm in (bars.get(code, {}).get(today) or {}).keys()
        }
    )
    if not times:
        raise ValueError(f"no intraday bars for {today}")
    return {
        "pool": pool,
        "codes": codes,
        "today": today,
        "days": days,
        "hist_days": hist_days,
        "bars": bars,
        "times": times,
        "daily_close": _load_daily_closes(data_db, codes, days),
        "shares": _load_shares(data_db, codes, today),
    }


def _get_context(data_db: Any, trade_date: Optional[str]) -> Dict[str, Any]:
    key = _context_key(data_db, trade_date)
    now = time.monotonic()
    with _CTX_LOCK:
        cached = _CTX_CACHE.get(key)
        if cached and now - cached[0] < _CTX_TTL_SECONDS:
            return cached[1]
    ctx = _build_context(data_db, trade_date)
    with _CTX_LOCK:
        _CTX_CACHE[key] = (now, ctx)
    return ctx


def _cumulative_series(
    day_map: Mapping[str, Tuple[float, float]], times: Sequence[str]
) -> List[float]:
    out: List[float] = []
    total = 0.0
    for hm in times:
        bar = day_map.get(hm)
        if bar:
            total += bar[0]
        out.append(total)
    return out


def _price_series(
    day_map: Mapping[str, Tuple[float, float]], times: Sequence[str]
) -> List[Optional[float]]:
    out: List[Optional[float]] = []
    last: Optional[float] = None
    for hm in times:
        bar = day_map.get(hm)
        if bar and bar[1]:
            last = bar[1]
        out.append(last)
    return out


def _series_for_code(ctx: Mapping[str, Any], code: str) -> Dict[str, Any]:
    times = ctx["times"]
    today = ctx["today"]
    bars = ctx["bars"]
    today_map = bars.get(code, {}).get(today) or {}
    today_cum = _cumulative_series(today_map, times)
    hist_series = [
        _cumulative_series(bars.get(code, {}).get(day) or {}, times)
        for day in ctx["hist_days"]
        if (bars.get(code, {}).get(day) or {})
    ]
    base_med: List[Optional[float]] = []
    base_p25: List[Optional[float]] = []
    base_p75: List[Optional[float]] = []
    for idx in range(len(times)):
        values = [series[idx] for series in hist_series]
        base_med.append(_median(values))
        base_p25.append(_quantile(values, 0.25))
        base_p75.append(_quantile(values, 0.75))
    return {
        "times": times,
        "today_cum": today_cum,
        "base_med": base_med,
        "base_p25": base_p25,
        "base_p75": base_p75,
        "price": _price_series(today_map, times),
    }


def _index_for_as_of(times: Sequence[str], as_of: str) -> int:
    if not times:
        return -1
    idx = -1
    for i, hm in enumerate(times):
        if hm <= as_of:
            idx = i
        else:
            break
    return idx


def _tail_amount(
    day_map: Mapping[str, Tuple[float, float]], as_of: str
) -> float:
    return sum(
        amount
        for hm, (amount, _close) in day_map.items()
        if "1430" < hm <= as_of
    )


def _share_pair(ctx: Mapping[str, Any], code: str) -> Tuple[Optional[float], Optional[float], Optional[str]]:
    item = ctx["shares"].get(code) or {}
    return item.get("chg1"), item.get("chg5"), item.get("date")


def _etf_signal(ratio: float, share1: Optional[float], tail_ratio: Optional[float]) -> str:
    if ratio >= 2.5 and tail_ratio is not None and tail_ratio >= 1.3:
        if share1 is None or share1 >= 1:
            return "high"
    if ratio >= 2.5:
        return "suspect"
    if ratio >= 2.0:
        return "watch"
    return "calm"


def _index_moves(data_db: Any, today: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for code, name in INDEX_NAMES.items():
        rows = list(
            data_db["index_prices"]
            .find(
                {"ts_code": code, "trade_date": {"$lte": today}},
                {"trade_date": 1, "close": 1, "_id": 0},
            )
            .sort("trade_date", -1)
            .limit(2)
        )
        pct = None
        if len(rows) >= 2:
            current = _num(rows[0].get("close"))
            previous = _num(rows[1].get("close"))
            if current and previous:
                pct = (current / previous - 1) * 100
        out.append({"ts_code": code, "name": name, "pct_chg": pct})
    return out


def compute_snapshot(
    data_db: Any,
    trade_date: Optional[str] = None,
    as_of: Optional[str] = None,
) -> Dict[str, Any]:
    ctx = _get_context(data_db, trade_date)
    times = ctx["times"]
    as_of_hm = _norm_time(as_of) or times[-1]
    idx = _index_for_as_of(times, as_of_hm)
    if idx < 0:
        raise ValueError(f"as_of {as_of_hm} is before the first bar {times[0]}")
    actual_as_of = times[idx]
    today = ctx["today"]

    etfs: List[Dict[str, Any]] = []
    pool_cur = 0.0
    pool_base = 0.0
    pool_tail = 0.0
    pool_prev_cur = 0.0
    pool_prev = 0.0
    pool_avg5_cur = 0.0
    pool_avg5 = 0.0
    tail_shares: List[float] = []
    breadth2 = 0
    breadth25 = 0
    share1_values: List[float] = []
    share5_values: List[float] = []
    share_covered = 0
    flow_delta = 0.0
    flow_base = 0.0
    flow_abs = 0.0
    prev_day = max((day for day in ctx["days"] if day < today), default=None)
    avg5_days = ctx["hist_days"][-5:]

    # Build per-code series once; reuse for breadth timeline.
    series_by_code = {code: _series_for_code(ctx, code) for code in ctx["codes"]}
    baseline_tail_by_day: List[float] = []

    for item in ctx["pool"]:
        code = item["ts_code"]
        series = series_by_code[code]
        cur = series["today_cum"][idx]
        base = series["base_med"][idx] or 0.0
        ratio = cur / base if base else 0.0
        prev_map = (ctx["bars"].get(code, {}).get(prev_day)) if prev_day else {}
        prev_cum = _cumulative_series(prev_map or {}, times)[idx] if prev_day else None
        avg5_values = [
            _cumulative_series((ctx["bars"].get(code, {}).get(day) or {}), times)[idx]
            for day in avg5_days
        ]
        avg5_cum = statistics.mean(avg5_values) if avg5_values else None
        prev_ratio = cur / prev_cum if prev_cum else None
        avg5_ratio = cur / avg5_cum if avg5_cum else None
        today_map = (ctx["bars"].get(code, {}).get(today)) or {}
        tail = _tail_amount(today_map, actual_as_of) if actual_as_of > "1430" else 0.0
        tail_share = tail / cur if cur and actual_as_of > "1430" else None
        per_day_tail_share: List[float] = []
        if actual_as_of > "1430":
            for day in ctx["hist_days"]:
                day_map = (ctx["bars"].get(code, {}).get(day)) or {}
                day_cur = _cumulative_series(day_map, times)[idx]
                if day_cur:
                    per_day_tail_share.append(_tail_amount(day_map, actual_as_of) / day_cur)
        tail_baseline = _median(per_day_tail_share)
        tail_ratio = (
            tail_share / tail_baseline
            if tail_share is not None and tail_baseline
            else None
        )
        share1, share5, share_date = _share_pair(ctx, code)
        if share_date == today:
            share_covered += 1
        if share1 is not None:
            share1_values.append(share1)
        if share5 is not None:
            share5_values.append(share5)
        if ratio >= 2.0:
            breadth2 += 1
        if ratio >= 2.5:
            breadth25 += 1
        pool_cur += cur
        pool_base += base
        pool_tail += tail
        if prev_cum:
            pool_prev_cur += cur
            pool_prev += prev_cum
        if avg5_cum:
            pool_avg5_cur += cur
            pool_avg5 += avg5_cum

        prev_close = None
        for day in sorted(ctx["daily_close"].get(code, {}), reverse=True):
            if day < today:
                prev_close = ctx["daily_close"][code][day]
                break

        # AUM-weighted net primary-market flow.  Weight with the close on the
        # previous share row's date, so price moves do not contaminate shares.
        share_info = ctx["shares"].get(code) or {}
        latest_share = share_info.get("latest")
        prev_share = share_info.get("prev")
        prev_share_date = share_info.get("prev_date")
        weight_close = None
        if prev_share_date:
            weight_close = ctx["daily_close"].get(code, {}).get(prev_share_date)
        weight_close = weight_close or prev_close
        if latest_share and prev_share and weight_close:
            base_aum = prev_share * weight_close
            delta_aum = (latest_share - prev_share) * weight_close
            flow_base += base_aum
            flow_delta += delta_aum
            flow_abs += abs(delta_aum)

        last_price = series["price"][idx]
        pct_chg = (
            (last_price / prev_close - 1) * 100
            if last_price and prev_close
            else None
        )
        level = _etf_signal(ratio, share1, tail_ratio)
        etfs.append(
            {
                "ts_code": code,
                "name": item.get("name") or code,
                "index": item.get("index") or "-",
                "tier": item.get("tier") or "confirmed",
                "holder": item.get("holder") or "",
                "holder_pct": item.get("holder_pct"),
                "price": last_price,
                "pct_chg": pct_chg,
                "amount_yuan": cur,
                "baseline_yuan": base,
                "ratio": ratio,
                "prev_day_amount_yuan": prev_cum,
                "prev_day_ratio": prev_ratio,
                "avg5_amount_yuan": avg5_cum,
                "avg5_ratio": avg5_ratio,
                "tail_share": tail_share,
                "tail_baseline": tail_baseline,
                "tail_ratio": tail_ratio,
                "share_chg_1d": share1,
                "share_chg_5d": share5,
                "share_date": share_date,
                "share_stale": bool(share_date and share_date < today),
                "signal": level,
                "signal_label": _label_level(level),
            }
        )

    # Pool-level weighted tail share: sum(tail) / sum(cur), not the mean of
    # per-ETF percentages (510050's tiny tail would otherwise hide the signal).
    pool_tail_share = pool_tail / pool_cur if pool_cur and actual_as_of > "1430" else None
    if actual_as_of > "1430":
        for day in ctx["hist_days"]:
            day_cur = 0.0
            day_tail = 0.0
            for item in ctx["pool"]:
                day_map = (ctx["bars"].get(item["ts_code"], {}).get(day)) or {}
                day_cur += _cumulative_series(day_map, times)[idx]
                day_tail += _tail_amount(day_map, actual_as_of)
            if day_cur:
                baseline_tail_by_day.append(day_tail / day_cur)
    pool_tail_baseline = _median(baseline_tail_by_day)
    pool_tail_ratio = (
        pool_tail_share / pool_tail_baseline
        if pool_tail_share is not None and pool_tail_baseline
        else None
    )
    avg_share1 = statistics.mean(share1_values) if share1_values else None
    avg_share5 = statistics.mean(share5_values) if share5_values else None
    pool_flow = (flow_delta / flow_base * 100) if flow_base else None
    share_rotation = (flow_abs / abs(flow_delta)) if flow_delta else None
    share_direction = _share_direction(pool_flow)

    # Breadth timeline / first crossings.  Ignore the opening auction window:
    # a tiny 09:30 baseline makes almost anything look like a signal.
    first_watch = None
    first_suspect = None
    for i, hm in enumerate(times[: idx + 1]):
        if hm < "0940":
            continue
        n2 = 0
        n25 = 0
        for code in ctx["codes"]:
            series = series_by_code[code]
            cur = series["today_cum"][i]
            base = series["base_med"][i] or 0
            if not base:
                continue
            ratio = cur / base
            if ratio >= 2.0:
                n2 += 1
            if ratio >= 2.5:
                n25 += 1
        if first_watch is None and n2 >= 2:
            first_watch = hm
        if first_suspect is None and n25 >= 3:
            first_suspect = hm

    if (
        breadth25 >= 3
        and avg_share1 is not None
        and avg_share1 >= 1
        and pool_tail_ratio is not None
        and pool_tail_ratio >= 1.3
    ):
        level = "high"
    elif breadth25 >= 3:
        level = "suspect"
    elif breadth2 >= 2:
        level = "watch"
    else:
        level = "calm"

    share_date = max(
        (item.get("share_date") for item in etfs if item.get("share_date")),
        default=None,
    )
    # fund_share is published T+1, so during (and right after) the session the
    # newest row belongs to an older trade date.  Values stay available, but the
    # UI must label them with their own date instead of the snapshot's date.
    share_stale = bool(share_date and share_date < today)
    return {
        "success": True,
        "trade_date": today,
        "as_of": actual_as_of,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "level": level,
        "level_label": _label_level(level),
        "breadth": {
            "ratio2": breadth2,
            "ratio25": breadth25,
            "pool_size": len(ctx["pool"]),
        },
        "pool": {
            "amount_yuan": pool_cur,
            "baseline_yuan": pool_base,
            "ratio": (pool_cur / pool_base) if pool_base else None,
            "prev_day_amount_yuan": pool_prev,
            "prev_day_ratio": (pool_prev_cur / pool_prev) if pool_prev else None,
            "avg5_amount_yuan": pool_avg5,
            "avg5_ratio": (pool_avg5_cur / pool_avg5) if pool_avg5 else None,
        },
        "tail": {
            "share": pool_tail_share,
            "baseline": pool_tail_baseline,
            "ratio": pool_tail_ratio,
        },
        "share": {
            "avg_chg_1d": avg_share1,
            "avg_chg_5d": avg_share5,
            "date": share_date,
            "stale": share_stale,
            "covered": share_covered,
            "total": len(ctx["pool"]),
            "pool_flow": pool_flow,
            "rotation": share_rotation,
            "direction": share_direction,
            "direction_label": SHARE_DIRECTION_LABELS[share_direction],
        },
        "first_signal": {
            "watch": first_watch,
            "suspect": first_suspect,
        },
        "index_moves": _index_moves(data_db, today),
        "etfs": etfs,
    }


def compute_curve(
    data_db: Any,
    code: str,
    trade_date: Optional[str] = None,
    as_of: Optional[str] = None,
) -> Dict[str, Any]:
    ctx = _get_context(data_db, trade_date)
    code = str(code or "").strip().upper()
    if code not in ctx["codes"]:
        raise ValueError(f"{code} is not in the national-team pool")
    series = _series_for_code(ctx, code)
    times = series["times"]
    as_of_hm = _norm_time(as_of) or times[-1]
    idx = _index_for_as_of(times, as_of_hm)
    if idx < 0:
        raise ValueError(f"as_of {as_of_hm} is before the first bar {times[0]}")
    item = next((row for row in ctx["pool"] if row["ts_code"] == code), {})
    ratio = []
    for i, hm in enumerate(times):
        base = series["base_med"][i]
        ratio.append(series["today_cum"][i] / base if base else None)
    return {
        "success": True,
        "trade_date": ctx["today"],
        "code": code,
        "name": item.get("name") or code,
        "index": item.get("index") or "-",
        "as_of": times[idx],
        "times": ["%s:%s" % (hm[:2], hm[2:]) for hm in times],
        "today_cum": series["today_cum"],
        "baseline_med": series["base_med"],
        "baseline_p25": series["base_p25"],
        "baseline_p75": series["base_p75"],
        "price": series["price"],
        "ratio": ratio,
        "amount_unit": "yuan",
    }


# ---------------------------------------------------------------------------
# Entry ladder
# ---------------------------------------------------------------------------
#
# Daily, study-backed sizing ladder for a discretionary buyer.  It is NOT a
# standalone alpha strategy: the 2015-2026 costed backtest showed no edge over
# a constant-exposure benchmark (Sharpe 0.22 for every constant exposure vs
# -0.04..0.19 for the timing rules), so the card is framed as position
# discipline.  Frequencies come from the six completed accumulation episodes
# (2015+), measured with a 120-session window from each episode start; see
# llm-wiki projects/trading/research/national-team-etf-entry-timing.md.

_LADDER_TTL_SECONDS = 300.0
_LADDER_LOCK = threading.Lock()
_LADDER_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}

_LADDER_DEFAULT_INDEX = "000300.SH"

# Design A (2026-11-21): the accumulation episodes stay pool-wide (the same six
# used by the study), and each index is anchored to its own close at the raw
# episode start. Everything below is the forward path *of that index* after the
# first tier trigger, recomputed from index_prices with the study's method
# (HS300 reproduces the study row for row). STAR50 has four episodes because
# 000688.SH only starts on 2019-12-31.
_LADDER_INDEX_SPECS = {
    "000300.SH": {
        "name": "沪深300",
        "episodes": 6, "regime_a": 3, "regime_b": 3,
        "median_low": -17.9, "median_days": 75,
        "median_f60_onset": -3.6, "median_f120_onset": -5.3,
        "tiers": {
            "T1": {"reach": "4/6", "f60": 7.2, "f120": -8.6, "post_dd": -13.0},
            "T2": {"reach": "3/6", "f60": 7.4, "f120": -10.9, "post_dd": -12.9},
            "T3": {"reach": "3/6", "f60": 11.1, "f120": -4.8, "post_dd": -6.2},
            "T4": {"reach": "6/6", "f60": 0.4, "f120": -3.1, "f250": 3.3},
        },
    },
    "000016.SH": {
        "name": "上证50",
        "episodes": 6, "regime_a": 2, "regime_b": 4,
        "median_low": -16.7, "median_days": 75,
        "median_f60_onset": -3.7, "median_f120_onset": -6.2,
        "tiers": {
            "T1": {"reach": "4/6", "f60": 4.4, "f120": -7.3, "post_dd": -10.6},
            "T2": {"reach": "4/6", "f60": 13.8, "f120": -2.9, "post_dd": -4.2},
            "T3": {"reach": "2/6", "f60": 10.5, "f120": -5.5, "post_dd": -5.9},
            "T4": {"reach": "6/6", "f60": 0.9, "f120": -1.8, "f250": 4.0},
        },
    },
    "000905.SH": {
        "name": "中证500",
        "episodes": 6, "regime_a": 3, "regime_b": 3,
        "median_low": -15.7, "median_days": 75,
        "median_f60_onset": -2.6, "median_f120_onset": -1.0,
        "tiers": {
            "T1": {"reach": "4/6", "f60": 1.5, "f120": 3.1, "post_dd": -13.3},
            "T2": {"reach": "3/6", "f60": 11.3, "f120": -14.2, "post_dd": -15.5},
            "T3": {"reach": "2/6", "f60": 24.2, "f120": 3.6, "post_dd": -5.1},
            "T4": {"reach": "6/6", "f60": 0.0, "f120": -2.2, "f250": 4.2},
        },
    },
    "000852.SH": {
        "name": "中证1000",
        "episodes": 6, "regime_a": 3, "regime_b": 3,
        "median_low": -17.3, "median_days": 75.5,
        "median_f60_onset": -2.2, "median_f120_onset": -0.4,
        "tiers": {
            "T1": {"reach": "5/6", "f60": 2.8, "f120": 7.6, "post_dd": -15.8},
            "T2": {"reach": "3/6", "f60": 12.4, "f120": -2.7, "post_dd": -15.5},
            "T3": {"reach": "3/6", "f60": 20.1, "f120": -0.3, "post_dd": -10.6},
            "T4": {"reach": "6/6", "f60": 0.5, "f120": 2.0, "f250": 10.8},
        },
    },
    "000688.SH": {
        "name": "科创50",
        "episodes": 4, "regime_a": 2, "regime_b": 2,
        "median_low": -15.1, "median_days": 77,
        "median_f60_onset": -5.6, "median_f120_onset": -7.4,
        "tiers": {
            "T1": {"reach": "3/4", "f60": -1.6, "f120": -12.2, "post_dd": -25.3},
            "T2": {"reach": "2/4", "f60": -11.1, "f120": -7.8, "post_dd": -23.4},
            "T3": {"reach": "1/4", "f60": -2.8, "f120": -4.0, "post_dd": -20.6},
            "T4": {"reach": "4/4", "f60": -2.1, "f120": -4.7, "f250": 11.8},
        },
    },
    "399006.SZ": {
        "name": "创业板",
        "episodes": 6, "regime_a": 3, "regime_b": 3,
        "median_low": -15.0, "median_days": 72.5,
        "median_f60_onset": -6.5, "median_f120_onset": -8.4,
        "tiers": {
            "T1": {"reach": "5/6", "f60": -8.1, "f120": -5.9, "post_dd": -22.9},
            "T2": {"reach": "3/6", "f60": -8.0, "f120": -5.4, "post_dd": -18.3},
            "T3": {"reach": "2/6", "f60": 19.7, "f120": 0.3, "post_dd": -14.0},
            "T4": {"reach": "6/6", "f60": -1.7, "f120": -2.3, "f250": 6.0},
        },
    },
}

_LADDER_TIER_SPECS = [
    {"id": "T1", "label": "-10%", "drawdown": 0.10},
    {"id": "T2", "label": "-15%", "drawdown": 0.15},
    {"id": "T3", "label": "-20%", "drawdown": 0.20},
]

_LADDER_STATUS_LABELS = {
    "triggered": "已触发",
    "pending": "待触发",
    "unknown": "无数据",
}


def _load_adj_factors(
    data_db: Any, codes: Sequence[str], days: Sequence[str]
) -> Dict[str, Dict[str, float]]:
    out: Dict[str, Dict[str, float]] = {code: {} for code in codes}
    if not days:
        return out
    for doc in data_db["etf_adj_factor"].find(
        {
            "ts_code": {"$in": list(codes)},
            "trade_date": {"$gte": days[0], "$lte": days[-1]},
        },
        {"ts_code": 1, "trade_date": 1, "adj_factor": 1, "_id": 0},
    ):
        factor = _num(doc.get("adj_factor"))
        if factor:
            out.setdefault(doc["ts_code"], {})[_norm_day(doc.get("trade_date"))] = factor
    return out


def _load_index_closes(
    data_db: Any, code: str, days: Sequence[str]
) -> Dict[str, float]:
    out: Dict[str, float] = {}
    if not days:
        return out
    for doc in data_db["index_prices"].find(
        {
            "ts_code": code,
            "trade_date": {"$gte": days[0], "$lte": days[-1]},
        },
        {"trade_date": 1, "close": 1, "_id": 0},
    ):
        close = _num(doc.get("close"))
        if close:
            out[_norm_day(doc.get("trade_date"))] = close
    return out


def _ladder_daily_flows(
    share_series: Mapping[str, Tuple[Sequence[str], Sequence[float]]],
    closes: Mapping[str, Mapping[str, float]],
    adj_factors: Mapping[str, Mapping[str, float]],
) -> Dict[str, Optional[float]]:
    """Daily pool net-subscription %, AUM-weighted, corporate actions excluded.

    ``flow = AUM_t - AUM_{t-1} * (1 + adjusted return)``; splits cancel out,
    and days around a split/merge (share jump within three sessions of an
    adj-factor change) are dropped instead of being read as a redemption.
    The first 20 share rows of each ETF are skipped to avoid listing flows.
    """
    daily: Dict[str, List[float]] = defaultdict(lambda: [0.0, 0.0])
    for code, (dates, values) in share_series.items():
        close_map = closes.get(code) or {}
        adj_map = adj_factors.get(code) or {}
        rows = [
            (str(day), float(share), close_map.get(str(day)), adj_map.get(str(day)))
            for day, share in zip(dates, values)
            if close_map.get(str(day)) and adj_map.get(str(day))
        ]
        if len(rows) < 21:
            continue
        ca_idx = {
            i
            for i in range(1, len(rows))
            if rows[i - 1][3] and abs(rows[i][3] / rows[i - 1][3] - 1) > 0.03
        }
        for i in range(1, len(rows)):
            if i < 20:
                continue
            day, share, close, adj = rows[i]
            prev_day, prev_share, prev_close, prev_adj = rows[i - 1]
            try:
                gap = (
                    datetime.strptime(day, "%Y%m%d")
                    - datetime.strptime(prev_day, "%Y%m%d")
                ).days
            except ValueError:
                continue
            if gap > 10:
                continue
            share_jump = abs(share / prev_share - 1) if prev_share else 0.0
            near_ca = any(abs(i - j) <= 3 for j in ca_idx)
            if i in ca_idx or (share_jump > 0.20 and near_ca):
                continue
            adj_return = (close * adj) / (prev_close * prev_adj) - 1
            prev_aum = prev_share * 1e4 * prev_close
            flow = share * 1e4 * close - prev_aum * (1 + adj_return)
            daily[day][0] += flow
            daily[day][1] += prev_aum
    return {
        day: (value[0] / value[1] * 100 if value[1] else None)
        for day, value in daily.items()
    }


def _rolling_sum(values: Sequence[Optional[float]], window: int, min_periods: int) -> List[Optional[float]]:
    out: List[Optional[float]] = []
    for i in range(len(values)):
        start = max(0, i - window + 1)
        chunk = [v for v in values[start : i + 1] if v is not None]
        if i + 1 >= min_periods and len(chunk) >= min_periods:
            out.append(sum(chunk))
        else:
            out.append(None)
    return out


def _ladder_episode(
    dates: Sequence[str],
    flow60: Sequence[Optional[float]],
    hs300: Mapping[str, Optional[float]],
    today: str,
) -> Dict[str, Any]:
    """Causal accumulation episode: flow60>=5 starts, five sessions confirm.

    ``p0`` is the HS300 close on the *raw* start day (the first day of the
    run), so the displayed drawdown matches the study's episode table.  The
    episode only becomes ``active`` after the fifth consecutive session;
    ten consecutive sessions below 5% end it.  After the end we keep the last
    completed episode so T4 (buy after accumulation ends) can still be shown.
    """
    idx_today = -1
    for i, day in enumerate(dates):
        if day <= today:
            idx_today = i
        else:
            break
    if idx_today < 0:
        return {
            "active": False,
            "start": None,
            "start_idx": None,
            "p0": None,
            "days": None,
            "ended": False,
            "end": None,
        }
    raw = [v is not None and v >= 5 for v in flow60]
    active = False
    true_count = 0
    false_count = 0
    run_start: Optional[int] = None
    cur_start: Optional[int] = None
    cur_p0: Optional[float] = None
    last_start: Optional[int] = None
    last_p0: Optional[float] = None
    last_end: Optional[int] = None
    for i in range(idx_today + 1):
        if raw[i]:
            if run_start is None:
                run_start = i
            true_count += 1
            false_count = 0
        else:
            true_count = 0
            false_count += 1
            if not active:
                run_start = None
        if not active and run_start is not None and true_count >= 5:
            active = True
            cur_start = run_start
            cur_p0 = hs300.get(dates[run_start])
        if active and false_count >= 10:
            active = False
            last_start, last_p0, last_end = cur_start, cur_p0, i
            run_start = None
            cur_start = None
            cur_p0 = None
    if active:
        start_idx, p0 = cur_start, cur_p0
        ended = False
        end_idx = None
    else:
        start_idx, p0 = last_start, last_p0
        ended = last_end is not None
        end_idx = last_end
    return {
        "active": active,
        "start": dates[start_idx] if start_idx is not None else None,
        "start_idx": start_idx,
        "p0": p0,
        # Inclusive session count: the start day itself is day 1, matching the
        # "第 N 日" wording on the dashboard card.
        "days": (idx_today - start_idx + 1) if start_idx is not None else None,
        "ended": bool(ended and not active),
        "end": dates[end_idx] if end_idx is not None else None,
        "end_idx": end_idx,
        "end_days_ago": (idx_today - end_idx) if end_idx is not None else None,
    }


# Study filter for a *real* accumulation episode: cumulative net subscription
# >= 10% and >= 15 sessions. Weaker runs are window artefacts — the 2025-04
# run peaked at flow60 +12.6% but its 69 sessions netted -0.1%, and the study's
# episode table filters it out.
_T4_MIN_CUM_FLOW = 10.0
_T4_MIN_SESSIONS = 15


def _t4_qualified(
    cum_flow: Optional[float], sessions: Optional[int]
) -> bool:
    """True when the episode meets the study's size filter for T4."""
    return (
        cum_flow is not None
        and sessions is not None
        and cum_flow >= _T4_MIN_CUM_FLOW
        and sessions >= _T4_MIN_SESSIONS
    )


def _ladder_tiers(
    p0: Optional[float],
    price: Optional[float],
    active: bool,
    ended: bool,
    t4_recent: bool = False,
    index_spec: Optional[Mapping[str, Any]] = None,
    t4_qualified: bool = True,
) -> List[Dict[str, Any]]:
    stats = (index_spec or _LADDER_INDEX_SPECS[_LADDER_DEFAULT_INDEX])["tiers"]
    tiers: List[Dict[str, Any]] = []
    for spec in _LADDER_TIER_SPECS:
        target = round(p0 * (1 - spec["drawdown"]), 1) if p0 else None
        if price is None or target is None:
            status = "unknown"
            distance = None
        elif price <= target:
            status = "triggered"
            distance = None
        else:
            status = "pending"
            distance = (target / price - 1) * 100
        hist = stats[spec["id"]]
        tiers.append(
            {
                "id": spec["id"],
                "label": spec["label"],
                "price": target,
                "weight": 0.25,
                "status": status,
                "status_label": _LADDER_STATUS_LABELS[status],
                "distance_pct": distance,
                "hist_reach": hist["reach"],
                "hist_f60": hist["f60"],
                "hist_f120": hist["f120"],
                "hist_post_dd": hist["post_dd"],
                "condition": None,
            }
        )
    if active:
        t4_status = "pending"
    elif ended and t4_recent and t4_qualified:
        # The "buy after accumulation ends" window is 60 sessions; after that
        # it is a stale event, not a live trigger. The study filter keeps
        # window artefacts (flow60 elevated, cumulative ~0) from triggering.
        t4_status = "triggered"
    elif ended:
        t4_status = "pending"
    else:
        t4_status = "unknown"
    t4_hist = stats["T4"]
    tiers.append(
        {
            "id": "T4",
            "label": "增仓结束",
            "price": None,
            "weight": 0.25,
            "status": t4_status,
            "status_label": _LADDER_STATUS_LABELS[t4_status],
            "distance_pct": None,
            "hist_reach": t4_hist["reach"],
            "hist_f60": t4_hist["f60"],
            "hist_f120": t4_hist["f120"],
            "hist_f250": t4_hist["f250"],
            "hist_post_dd": None,
            "condition": "flow60 < 5% 且段内累计 ≥10%",
        }
    )
    return tiers


def _annotate_tier_triggers(
    tiers: Sequence[Dict[str, Any]],
    sessions: Sequence[str],
    closes: Mapping[str, Optional[float]],
    start_date: Optional[str],
    episode: Optional[Mapping[str, Any]] = None,
) -> None:
    """Attach the first session each price tier was reached after the start.

    ``sessions`` is the index's own trade-date series (not the share dates):
    the latest index close is usually one session ahead of the T+1 share flow,
    and a tier can cross on that last session. Pending tiers keep ``None``;
    T4 (buy after the accumulation ends) uses the episode end date.
    """
    session_idx = {day: i for i, day in enumerate(sessions)}
    start_idx = session_idx.get(start_date) if start_date else None
    for tier in tiers:
        tier["trigger_date"] = None
        tier["trigger_days"] = None
        if tier["status"] != "triggered":
            continue
        if tier["id"] == "T4":
            end = (episode or {}).get("end")
            end_idx = session_idx.get(end) if end else None
            if end_idx is not None:
                tier["trigger_date"] = end
                if start_idx is not None:
                    tier["trigger_days"] = end_idx - start_idx + 1
            continue
        target = tier.get("price")
        if target is None or start_idx is None:
            continue
        for k in range(start_idx, len(sessions)):
            px = closes.get(sessions[k])
            if px is not None and px <= target:
                tier["trigger_date"] = sessions[k]
                tier["trigger_days"] = k - start_idx + 1
                break


def compute_entry_ladder(
    data_db: Any,
    trade_date: Optional[str] = None,
    index: Optional[str] = None,
) -> Dict[str, Any]:
    """Probability-based buy ladder for the national-team ETF pool.

    Read-only and cached for five minutes: the underlying share data is daily
    (T+1), so a shorter TTL would only add Mongo scans.

    ``index`` selects the price anchor (000300.SH / 000016.SH / 000905.SH /
    000852.SH / 000688.SH / 399006.SZ). The accumulation episode itself is
    pool-wide; see ``_LADDER_INDEX_SPECS``.
    """
    pool = load_pool(data_db)
    codes = [item["ts_code"] for item in pool]
    if not codes:
        raise ValueError("national-team pool is empty")
    index_code = str(index or _LADDER_DEFAULT_INDEX).strip().upper()
    index_spec = _LADDER_INDEX_SPECS.get(index_code)
    if index_spec is None:
        raise ValueError(
            f"unsupported index {index_code}; expected one of "
            + ", ".join(_LADDER_INDEX_SPECS)
        )
    today = _norm_day(trade_date)
    if not today:
        doc = data_db["etf_daily"].find_one(
            {"ts_code": {"$in": codes}},
            {"trade_date": 1},
            sort=[("trade_date", -1)],
        )
        today = _norm_day(doc.get("trade_date")) if doc else ""
    if not today:
        raise ValueError("no etf_daily data for the national-team pool")

    key = f"{id(data_db)}:{today}:{index_code}"
    now = time.monotonic()
    with _LADDER_LOCK:
        cached = _LADDER_CACHE.get(key)
        if cached and now - cached[0] < _LADDER_TTL_SECONDS:
            return cached[1]

    start_day = (datetime.strptime(today, "%Y%m%d") - timedelta(days=420)).strftime(
        "%Y%m%d"
    )
    share_series = _load_share_series(data_db, codes, start_day)
    closes = _load_daily_closes(data_db, codes, [start_day, today])
    adj_factors = _load_adj_factors(data_db, codes, [start_day, today])
    flows = _ladder_daily_flows(share_series, closes, adj_factors)
    dates = sorted(day for day, value in flows.items() if day <= today and value is not None)
    if not dates:
        raise ValueError("no share-flow data for the national-team pool")
    values = [flows[day] for day in dates]
    flow20 = _rolling_sum(values, 20, 15)
    flow60 = _rolling_sum(values, 60, 40)

    index_closes = _load_index_closes(data_db, index_code, [start_day, today])
    hs_dates = sorted(index_closes)
    aligned: Dict[str, Optional[float]] = {}
    last_hs: Optional[float] = None
    j = 0
    for day in dates:
        while j < len(hs_dates) and hs_dates[j] <= day:
            last_hs = index_closes[hs_dates[j]]
            j += 1
        aligned[day] = last_hs
    episode = _ladder_episode(dates, flow60, aligned, today)

    index_date = max((d for d in hs_dates if d <= today), default=None)
    current_price = index_closes.get(index_date) if index_date else None
    p0 = episode.get("p0")
    drawdown = (
        (current_price / p0 - 1) * 100
        if current_price is not None and p0
        else None
    )
    active = bool(episode.get("active"))
    ended = bool(episode.get("ended"))
    if not active and not ended:
        regime = "none"
    elif drawdown is not None and drawdown <= -15:
        regime = "deep"
    else:
        regime = "shallow"

    # Study filter for T4: cumulative net subscription over the episode (sum
    # of the daily AUM-weighted flows) must clear 10% and the run must last at
    # least 15 sessions. This keeps window artefacts like the 2025-04 run
    # (flow60 peaked +12.6%, cumulative -0.1%) from firing a buy tier.
    start_idx = episode.get("start_idx")
    end_idx = episode.get("end_idx")
    cum_flow: Optional[float] = None
    session_count: Optional[int] = None
    if start_idx is not None:
        last_i = end_idx
        if last_i is None and episode.get("days"):
            last_i = start_idx + episode["days"] - 1
        if last_i is not None and last_i >= start_idx:
            cum_flow = sum(values[start_idx : last_i + 1])
            session_count = last_i - start_idx + 1
    t4_qualified = _t4_qualified(cum_flow, session_count)

    end_days_ago = episode.get("end_days_ago")
    t4_recent = ended and end_days_ago is not None and end_days_ago <= 60
    tiers = _ladder_tiers(
        p0,
        current_price,
        active,
        ended,
        t4_recent=t4_recent,
        index_spec=index_spec,
        t4_qualified=t4_qualified,
    )
    _annotate_tier_triggers(
        tiers, hs_dates, index_closes, episode.get("start"), episode
    )
    suggested = sum(t["weight"] for t in tiers if t["status"] == "triggered")
    next_tier = next(
        (t for t in tiers if t["status"] == "pending" and t["price"] is not None),
        None,
    )

    negative_days = 0
    for value in reversed(flow60):
        if value is not None and value < 0:
            negative_days += 1
        else:
            break

    latest_flow = flow60[-1] if flow60 else None
    latest_flow20 = flow20[-1] if flow20 else None
    payload = {
        "success": True,
        "trade_date": today,
        "flow_date": dates[-1],
        "as_of": "close",
        "index": {
            "ts_code": index_code,
            "name": index_spec["name"],
            "close": current_price,
            "date": index_date,
        },
        "episode": {
            "active": active,
            "start": episode.get("start"),
            "start_close": p0,
            "days": episode.get("days"),
            "ended": ended,
            "end": episode.get("end"),
            "cum_flow": cum_flow,
            "sessions": session_count,
            "t4_qualified": t4_qualified,
            "flow20": latest_flow20,
            "flow60": latest_flow,
            "flow": values[-1] if values else None,
            "drawdown": drawdown,
            "regime": regime,
        },
        "tiers": tiers,
        "suggested_position": suggested,
        "next_tier": next_tier,
        "invalidation": {
            "active": negative_days >= 20,
            "flow60": latest_flow,
            "negative_days": negative_days,
            "rule": "flow60 < 0 连续 20 个交易日",
        },
        "history": {
            "episodes": index_spec["episodes"],
            "regime_a": index_spec["regime_a"],
            "regime_b": index_spec["regime_b"],
            "median_low": index_spec["median_low"],
            "median_days": index_spec["median_days"],
            "median_f60_onset": index_spec["median_f60_onset"],
            "median_f120_onset": index_spec["median_f120_onset"],
            "note": f"{index_spec['episodes']} 个已完成 episode；频数不是精确概率",
        },
    }
    with _LADDER_LOCK:
        _LADDER_CACHE[key] = (now, payload)
    return payload


# ---------------------------------------------------------------------------
# 国家队敞口方向（2026-11-24 新增）
#
# 背景：2026 年国家队在 A 股的敞口出现了完整来回——2025Q4 高位、2026Q1/Q2
# 连续两季腰斩（核心宽基份额 -78%）、Q3 起回补。原有的盘中量价信号只看当天，
# 看不到这个「方向切换」。本模块给出慢变量：池内合计份额的时间轴、历史分位、
# 以及由 20 日变化 + 分位共同判定的阶段。
#
# 口径：
#   share_yi = 池内 ETF 的 fd_share 之和（Mongo 存万分，除 1e4 得亿份）
#   est_yi   = share_yi × 池加权国家队占比（holder_pct 按规模加权）
#   ★ 只用「池内全部 ETF 都有份额数据」的交易日，避免池子分母漂移
#     （20220728 起连续 1043 天全覆盖；更早的日子覆盖不全，不能用）
# ---------------------------------------------------------------------------

_EXPOSURE_TTL_SECONDS = 300.0
_EXPOSURE_LOCK = threading.Lock()
_EXPOSURE_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}

EXPOSURE_PHASES = {
    "bottom_refill": "底部回补",
    "inflow": "持续流入",
    "top_exit": "高位撤离",
    "outflow": "持续流出",
    "flat": "平稳",
    "unknown": "未知",
}


def _exposure_phase(chg20: Optional[float], pctile: Optional[float]) -> str:
    """20 日变化 × 历史分位 → 阶段。

    分界用 ±2%/20 日（约年化 ±25%，对应一次真实的建仓/撤仓节奏）与 35/65 分位。
    """
    if chg20 is None or pctile is None:
        return "unknown"
    if chg20 >= 2.0:
        return "bottom_refill" if pctile <= 35 else "inflow"
    if chg20 <= -2.0:
        return "top_exit" if pctile >= 65 else "outflow"
    return "flat"


def _pctile_rank(values: Sequence[float], value: float) -> Optional[float]:
    """value 在 values 中的百分位（0-100）。"""
    if not values or value is None:
        return None
    below = sum(1 for v in values if v < value)
    equal = sum(1 for v in values if v == value)
    return round((below + 0.5 * equal) / len(values) * 100.0, 1)


def compute_exposure(data_db: Any, days: int = 750) -> Dict[str, Any]:
    """国家队 ETF 敞口方向：合计份额时间轴 + 历史分位 + 阶段判定。"""
    pool = load_pool(data_db)
    codes = [p["ts_code"] for p in pool]
    if not codes:
        return {"error": "empty_pool"}

    key = f"{','.join(sorted(codes))}|{days}"
    now = time.time()
    with _EXPOSURE_LOCK:
        cached = _EXPOSURE_CACHE.get(key)
        if cached and now - cached[0] < _EXPOSURE_TTL_SECONDS:
            return cached[1]

    # 逐日份额（万份 -> 亿份）
    by_date: Dict[str, Dict[str, float]] = defaultdict(dict)
    try:
        cur = data_db["etf_shares"].find(
            {"ts_code": {"$in": codes}},
            {"ts_code": 1, "trade_date": 1, "fd_share": 1},
        )
        for doc in cur:
            d = _norm_day(doc.get("trade_date"))
            v = _num(doc.get("fd_share"))
            if not d or v is None:
                continue
            by_date[d][str(doc.get("ts_code"))] = v / 1e4
    except Exception as exc:  # pragma: no cover
        return {"error": f"etf_shares_read_failed: {exc}"}

    full_dates = sorted(d for d, m in by_date.items() if len(m) == len(codes))
    if not full_dates:
        return {"error": "no_full_coverage_dates", "pool_size": len(codes)}
    dates = full_dates[-days:] if days and days > 0 else full_dates

    # 池加权国家队占比（规模加权；规模缺失时等权）
    wsum = 0.0
    tot = 0.0
    for p in pool:
        pct = _num(p.get("holder_pct"))
        if pct is None:
            continue
        w = 1.0
        tot += w
        wsum += w * pct
    pool_pct = (wsum / tot) if tot > 0 else None

    shares = [sum(by_date[d].values()) for d in dates]

    def _chg(vals: Sequence[float], i: int, n: int) -> Optional[float]:
        if i - n < 0:
            return None
        base = vals[i - n]
        if not base:
            return None
        return round((vals[i] / base - 1.0) * 100.0, 2)

    series: List[Dict[str, Any]] = []
    for i, d in enumerate(dates):
        v = shares[i]
        window = shares[max(0, i - 250): i] or shares[: i + 1]
        series.append(
            {
                "trade_date": d,
                "shares_yi": round(v, 1),
                "est_national_yi": round(v * pool_pct / 100.0, 1) if pool_pct else None,
                "chg_1d": _chg(shares, i, 1),
                "chg_5d": _chg(shares, i, 5),
                "chg_20d": _chg(shares, i, 20),
                "pctile_1y": _pctile_rank(window, v),
            }
        )

    latest = series[-1]
    # 阶段用「最新值 + 20 日变化」
    phase = _exposure_phase(latest.get("chg_20d"), latest.get("pctile_1y"))
    # 近 20 日曾达到的极端阶段（避免只看一天导致信号抖动）
    recent_phases = [
        _exposure_phase(row.get("chg_20d"), row.get("pctile_1y")) for row in series[-20:]
    ]

    payload = {
        "as_of": latest["trade_date"],
        "pool_size": len(codes),
        "pool_national_pct": round(pool_pct, 2) if pool_pct else None,
        "coverage_days": len(full_dates),
        "start": dates[0],
        "summary": {
            "shares_yi": latest["shares_yi"],
            "est_national_yi": latest["est_national_yi"],
            "chg_1d": latest["chg_1d"],
            "chg_5d": latest["chg_5d"],
            "chg_20d": latest["chg_20d"],
            "pctile_1y": latest["pctile_1y"],
            "phase": phase,
            "phase_label": EXPOSURE_PHASES.get(phase, phase),
            "recent_phases": sorted(set(recent_phases)),
        },
        "peaks": {
            "max_shares_yi": round(max(shares), 1),
            "max_date": dates[shares.index(max(shares))],
            "min_shares_yi": round(min(shares), 1),
            "min_date": dates[shares.index(min(shares))],
        },
        "series": series,
        "note": (
            "份额是 T+1 披露的一级市场申赎，不区分申购方；"
            "est_national_yi 按 2025 年报国家队占比静态推算，是下界估计。"
        ),
    }
    with _EXPOSURE_LOCK:
        _EXPOSURE_CACHE[key] = (now, payload)
    return payload
