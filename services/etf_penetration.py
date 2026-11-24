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

# 阶段标签：渗透率的变化率 ≈ 份额申赎的变化率（分子 ETF 价格与分母指数市值同向，
# 价格部分基本抵消），因此判据可以直接用「20 日相对变化 × 历史分位」。
# 阈值 ±2%/20 日 与 35/65 分位沿用 national-team 那一套，保持两个页面口径一致。
PHASE_LABELS: Dict[str, str] = {
    "bottom_refill": "底部回补",
    "inflow": "持续流入",
    "top_exit": "高位撤离",
    "outflow": "持续流出",
    "flat": "平稳",
    "unknown": "未知",
}


def _pctile_rank(values: List[float], value: float) -> Optional[float]:
    """``value`` 在 ``values`` 中的百分位（0-100），并列取区间中点。"""
    if not values or value is None:
        return None
    below = sum(1 for v in values if v < value)
    equal = sum(1 for v in values if v == value)
    return round((below + 0.5 * equal) / len(values) * 100.0, 1)


def _penetration_phase(chg20: Optional[float], pctile: Optional[float]) -> str:
    """20 日相对变化 × 历史分位 → 阶段。"""
    if chg20 is None or pctile is None:
        return "unknown"
    if chg20 >= 2.0:
        return "bottom_refill" if pctile <= 35 else "inflow"
    if chg20 <= -2.0:
        return "top_exit" if pctile >= 65 else "outflow"
    return "flat"


def _relative_change(values: List[float], i: int, n: int) -> Optional[float]:
    if i - n < 0:
        return None
    base = values[i - n]
    if not base:
        return None
    return round((values[i] / base - 1.0) * 100.0, 2)


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
    # 数据永远全量加载：days 只控制「返回多少行」给图表，绝不能参与分位与阶段的计算。
    # 否则「5年」档位的 1 年分位只基于 5 年数据、「全部」档位基于 13 年数据，
    # 同一个时点切档位会看到两个不同的分位——这是用户已经会踩到的坑。
    start = ALL_START

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

    # 阶段与分位必须在按 days 截断前、基于完整序列计算，否则「1 年分位」
    # 会退化成「当前窗口内的分位」，换个回看区间结论就变。
    ratios = [row["ratio_pct"] for row in rows]
    for i, row in enumerate(rows):
        row["chg_1d"] = _relative_change(ratios, i, 1)
        row["chg_5d"] = _relative_change(ratios, i, 5)
        row["chg_20d"] = _relative_change(ratios, i, 20)

    summary: Optional[Dict[str, Any]] = None
    if rows:
        latest_ratio = ratios[-1]
        window_1y = ratios[-250:]
        pctile_1y = _pctile_rank(window_1y, latest_ratio)
        pctile_all = _pctile_rank(ratios, latest_ratio)
        phase = _penetration_phase(rows[-1].get("chg_20d"), pctile_1y)
        recent = [
            _penetration_phase(row.get("chg_20d"), _pctile_rank(window_1y, row["ratio_pct"]))
            for row in rows[-20:]
        ]
        summary = {
            "ratio_pct": latest_ratio,
            "chg_1d": rows[-1].get("chg_1d"),
            "chg_5d": rows[-1].get("chg_5d"),
            "chg_20d": rows[-1].get("chg_20d"),
            "pctile_1y": pctile_1y,
            # 全样本分位只作参考：13 年分布与最近一年分布讲的是不同的故事，
            # 阶段判定仍用 1 年分位，否则阈值要重新校准。
            "pctile_all": pctile_all,
            "phase": phase,
            "phase_label": PHASE_LABELS.get(phase, phase),
            "recent_phases": sorted(set(recent)),
            # 两个分位必须一起看：科创50 在 2026-11 的 1 年分位是 99.8%，
            # 但全样本只有 56%——因为 2024-10 曾到过 10.11%，现在 5.32% 只是当年一半。
            # 只看 1 年会误判成「历史新高」。
            "note": (
                "阶段用 1 年分位判定（衡量近期位置）；pctile_all 是全样本分位。"
                "两者差距大时，说明当前位置相对长期仍有空间或已透支。"
            ),
            "peaks": {
                "max_ratio_pct": round(max(ratios), 4),
                "max_date": rows[ratios.index(max(ratios))]["trade_date"],
                "min_ratio_pct": round(min(ratios), 4),
                "min_date": rows[ratios.index(min(ratios))]["trade_date"],
            },
        }

    # 真实起点（全量）与本次返回区间起点要分开：前端文案「头部池全部成立后起」
    # 讲的是前者，切到「5年」档位不该把这句话改成 5 年前。
    first_full_date = rows[0]["trade_date"] if rows else None
    last_date = rows[-1]["trade_date"] if rows else None

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
        "summary": summary,
        "coverage": {
            "fund_count": len(funds),
            "first_full_date": first_full_date,
            "last_date": last_date,
            "series_start": rows[0]["trade_date"] if rows else None,
            "total_points": len(ratios),
        },
    }


__all__ = ["ETF_PENETRATION_POOLS", "compute_penetration", "DEFAULT_INDEX", "DEFAULT_DAYS"]
