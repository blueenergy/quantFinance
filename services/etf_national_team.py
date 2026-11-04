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
    180 calendar days and cached for a minute; the QMT backfill currently
    covers 2026-09-01 onward, so older dates are intentionally absent.
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
    cutoff = (datetime.now() - timedelta(days=180)).strftime("%Y%m%d") + "0000"
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
) -> Tuple[Optional[float], Optional[float]]:
    dates, values = series
    idx = bisect.bisect_right(dates, day) - 1
    if idx < 0 or not values[idx]:
        return None, None
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
    return chg1, chg5


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
        share1_values: List[float] = []
        share5_values: List[float] = []
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
            share1, share5 = _share_changes(share_series.get(code, ([], [])), day)
            if share1 is not None:
                share1_values.append(share1)
            if share5 is not None:
                share5_values.append(share5)

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

        avg_share1 = statistics.mean(share1_values) if share1_values else None
        if (
            breadth25 >= 3
            and avg_share1 is not None
            and avg_share1 >= 1
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
                "share_chg_5d": (
                    statistics.mean(share5_values) if share5_values else None
                ),
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

    share_date = next(
        (item.get("share_date") for item in etfs if item.get("share_date")),
        None,
    )
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
