"""Head broad-index ETF penetration: ETF AUM as a share of index float cap.

For a curated pool of the largest broad-index ETFs (the vehicles the national
team uses), the series is::

    ratio(t) = sum(shares_i(t) * close_i(t)) / index_float_mv(t)

Both the numerator and the denominator scale with the index level, so price
moves largely cancel out.  The curve therefore tracks creation/redemption
(national-team accumulation and unwinding), not market beta.

Only dates where every fund in the pool has both shares and a close are
emitted, so a fund launch or a data gap cannot create a fake level jump.
Share data is T+1: the latest emitted point is the latest day whose shares are
already stored.

Everything is read-only and uses only the data-access collections handed in by
the caller, so it can be exercised against production data without booting the
API.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from services.market_stats import lookback_start

# Curated "head" pools: for each index, the ETFs the national team actually
# uses (largest AUM, ~83-90% of that index's broad ETF AUM).  Kept separate
# from services.etf_national_team.POOL on purpose: that pool drives the
# intraday breadth denominator and must not change when this feature is tuned.
ETF_PENETRATION_POOLS: Dict[str, Dict[str, Any]] = {
    "hs300": {
        "name": "沪深300",
        "index_code": "000300.SH",
        "funds": [
            {"ts_code": "510300.SH", "name": "华泰柏瑞沪深300ETF"},
            {"ts_code": "510310.SH", "name": "易方达沪深300ETF"},
            {"ts_code": "510330.SH", "name": "华夏沪深300ETF"},
            {"ts_code": "159919.SZ", "name": "嘉实沪深300ETF"},
        ],
    },
    "sse50": {
        "name": "上证50",
        "index_code": "000016.SH",
        "funds": [
            {"ts_code": "510050.SH", "name": "华夏上证50ETF"},
        ],
    },
    "csi500": {
        "name": "中证500",
        "index_code": "000905.SH",
        "funds": [
            {"ts_code": "510500.SH", "name": "南方中证500ETF"},
            {"ts_code": "512500.SH", "name": "华夏中证500ETF"},
            {"ts_code": "159922.SZ", "name": "嘉实中证500ETF"},
        ],
    },
    "csi1000": {
        "name": "中证1000",
        "index_code": "000852.SH",
        "funds": [
            {"ts_code": "512100.SH", "name": "南方中证1000ETF"},
            {"ts_code": "159845.SZ", "name": "华夏中证1000ETF"},
            {"ts_code": "560010.SH", "name": "广发中证1000ETF"},
        ],
    },
    "gem": {
        "name": "创业板",
        "index_code": "399006.SZ",
        "funds": [
            {"ts_code": "159915.SZ", "name": "易方达创业板ETF"},
            {"ts_code": "159952.SZ", "name": "广发创业板ETF"},
            {"ts_code": "159977.SZ", "name": "天弘创业板ETF"},
        ],
    },
    "star50": {
        "name": "科创50",
        "index_code": "000688.SH",
        "funds": [
            {"ts_code": "588000.SH", "name": "华夏上证科创板50成份ETF"},
            {"ts_code": "588080.SH", "name": "易方达上证科创板50ETF"},
            {"ts_code": "588050.SH", "name": "工银瑞信上证科创板50ETF"},
        ],
    },
}

DEFAULT_INDEX = "hs300"
DEFAULT_DAYS = 2500
ALL_START = "20100101"


def _norm_day(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if "." in text:
        text = text.split(".", 1)[0]
    return text or None


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _load_series(coll: Any, codes: Iterable[str], start: str, field: str) -> Dict[str, Dict[str, float]]:
    rows = coll.find(
        {"ts_code": {"$in": list(codes)}, "trade_date": {"$gte": start}},
        {"_id": 0, "ts_code": 1, "trade_date": 1, field: 1},
    )
    out: Dict[str, Dict[str, float]] = {}
    for row in rows:
        day = _norm_day(row.get("trade_date"))
        value = _as_float(row.get(field))
        if not day or value is None:
            continue
        out.setdefault(str(row.get("ts_code")), {})[day] = value
    return out


def _load_index_float_mv(coll: Any, index_code: str, start: str) -> Dict[str, float]:
    rows = coll.find(
        {"ts_code": index_code, "trade_date": {"$gte": start}},
        {"_id": 0, "trade_date": 1, "float_mv": 1},
    )
    out: Dict[str, float] = {}
    for row in rows:
        day = _norm_day(row.get("trade_date"))
        value = _as_float(row.get("float_mv"))
        if day and value and value > 0:
            out[day] = value
    return out


def _load_index_close(coll: Any, index_code: str, start: str) -> Dict[str, float]:
    """Index close for the price-context panel.

    ``index_dailybasic`` does not return ``close`` (Tushare omits it even when
    requested); the index level lives in ``index_prices`` (``index_daily``).
    Missing prices simply yield ``index_close=None`` rows in the series.
    """
    rows = coll.find(
        {"ts_code": index_code, "trade_date": {"$gte": start}},
        {"_id": 0, "trade_date": 1, "close": 1},
    )
    out: Dict[str, float] = {}
    for row in rows:
        day = _norm_day(row.get("trade_date"))
        value = _as_float(row.get("close"))
        if day and value and value > 0:
            out[day] = value
    return out


def _available_indices() -> List[Dict[str, Any]]:
    return [
        {
            "key": key,
            "name": profile["name"],
            "index_code": profile["index_code"],
            "fund_count": len(profile["funds"]),
        }
        for key, profile in ETF_PENETRATION_POOLS.items()
    ]


def compute_penetration(data_db: Dict[str, Any], index_key: str = DEFAULT_INDEX, days: int = DEFAULT_DAYS) -> Dict[str, Any]:
    """Compute the head-ETF penetration series for one broad index.

    ``data_db`` is the mapping returned by ``db.get_data_db()`` (in tests it can
    be a shim).  ``days`` <= 0 means all available history.
    """
    key = (index_key or DEFAULT_INDEX).strip().lower()
    profile = ETF_PENETRATION_POOLS.get(key)
    if profile is None:
        raise ValueError(f"unknown index key: {index_key}")

    funds = profile["funds"]
    codes = [fund["ts_code"] for fund in funds]
    today = datetime.today().strftime("%Y%m%d")
    start = lookback_start(today, days) if days and days > 0 else ALL_START

    empty = {
        "success": True,
        "index": {"key": key, "name": profile["name"], "index_code": profile["index_code"], "funds": funds},
        "available_indices": _available_indices(),
        "days": days,
        "series": [],
        "latest": None,
        "coverage": {"fund_count": len(funds), "first_full_date": None, "last_date": None},
    }

    shares_coll = data_db.get("etf_shares")
    daily_coll = data_db.get("etf_daily")
    basic_coll = data_db.get("index_daily_basic")
    if shares_coll is None or daily_coll is None or basic_coll is None:
        return empty

    shares = _load_series(shares_coll, codes, start, "fd_share")
    closes = _load_series(daily_coll, codes, start, "close")
    float_mv = _load_index_float_mv(basic_coll, profile["index_code"], start)
    price_coll = data_db.get("index_prices")
    index_close = (
        _load_index_close(price_coll, profile["index_code"], start)
        if price_coll is not None
        else {}
    )

    rows: List[Dict[str, Any]] = []
    for day in sorted(float_mv):
        aum_wan = 0.0
        covered = 0
        for code in codes:
            share = shares.get(code, {}).get(day)
            close = closes.get(code, {}).get(day)
            if share is None or close is None:
                continue
            aum_wan += share * close
            covered += 1
        if covered != len(codes):
            continue
        aum_yi = aum_wan / 1e4
        cap_yi = float_mv[day] / 1e8
        if cap_yi <= 0:
            continue
        rows.append(
            {
                "trade_date": day,
                # NaN/inf already filtered; aum is 万份 -> 万元 -> 亿元
                "aum_yi": round(aum_yi, 2),
                "float_mv_yi": round(cap_yi, 2),
                "ratio_pct": round(aum_yi / cap_yi * 100.0, 4),
                "index_close": round(index_close[day], 2) if day in index_close else None,
                "n_funds": covered,
            }
        )

    if days and days > 0:
        rows = rows[-days:]

    latest = rows[-1] if rows else None
    return {
        "success": True,
        "index": {"key": key, "name": profile["name"], "index_code": profile["index_code"], "funds": funds},
        "available_indices": _available_indices(),
        "days": days,
        "series": rows,
        "latest": latest,
        "coverage": {
            "fund_count": len(funds),
            "first_full_date": rows[0]["trade_date"] if rows else None,
            "last_date": rows[-1]["trade_date"] if rows else None,
        },
    }


__all__ = ["ETF_PENETRATION_POOLS", "compute_penetration", "DEFAULT_INDEX", "DEFAULT_DAYS"]
