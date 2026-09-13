"""Batch-load current scoring context for fund-inventory classifications."""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from stock_data_access.score_query import merge_score_scope


def _as_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _a_share_symbols(rows: Sequence[Mapping[str, Any]]) -> List[str]:
    return sorted(
        {
            str(row.get("ts_code") or "").strip().upper()
            for row in rows
            if str(row.get("market") or "A") == "A"
            and str(row.get("ts_code") or "").strip()
        }
    )


def _latest_scores(collection: Any, symbols: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    if collection is None or not symbols:
        return {}
    pipeline = [
        {"$match": merge_score_scope({"symbol": {"$in": list(symbols)}}, None)},
        {"$sort": {"symbol": 1, "score_date": -1}},
        {"$group": {"_id": "$symbol", "doc": {"$first": "$$ROOT"}}},
        {
            "$project": {
                "_id": 0,
                "symbol": "$_id",
                "score_date": "$doc.score_date",
                "growth_score": "$doc.growth_score",
                "fundamental_score": "$doc.fundamental_score",
                "value_score": "$doc.value_score",
            }
        },
    ]
    return {
        str(row.get("symbol") or ""): dict(row)
        for row in collection.aggregate(pipeline)
        if row.get("symbol")
    }


def _latest_financials(
    collection: Any,
    symbols: Sequence[str],
) -> Dict[str, List[Dict[str, Any]]]:
    if collection is None or not symbols:
        return {}
    projection = {
        "_id": 0,
        "ts_code": 1,
        "ann_date": 1,
        "end_date": 1,
        "q_netprofit_yoy": 1,
        "netprofit_yoy": 1,
    }
    cursor = collection.find(
        {"ts_code": {"$in": list(symbols)}},
        projection,
    ).sort([("ts_code", 1), ("end_date", -1), ("ann_date", -1)])
    out: Dict[str, List[Dict[str, Any]]] = {}
    seen_periods: Dict[str, set] = {}
    for raw in cursor:
        symbol = str(raw.get("ts_code") or "")
        period = str(raw.get("end_date") or "")
        if not symbol or not period:
            continue
        periods = seen_periods.setdefault(symbol, set())
        if period in periods or len(periods) >= 2:
            continue
        periods.add(period)
        out.setdefault(symbol, []).append(dict(raw))
    return out


def _ytd_price_points(
    collection: Any,
    symbols: Sequence[str],
    year_start: str,
) -> Dict[str, Dict[str, Any]]:
    if collection is None or not symbols:
        return {}
    pipeline = [
        {
            "$match": {
                "symbol": {"$in": list(symbols)},
                "trade_date": {"$gte": year_start},
            }
        },
        {"$sort": {"symbol": 1, "trade_date": 1}},
        {
            "$group": {
                "_id": "$symbol",
                "first_date": {"$first": "$trade_date"},
                "first_close": {"$first": "$close"},
                "last_date": {"$last": "$trade_date"},
                "last_close": {"$last": "$close"},
            }
        },
    ]
    return {
        str(row.get("_id") or ""): dict(row)
        for row in collection.aggregate(pipeline)
        if row.get("_id")
    }


def _adjustment_factors(
    collection: Any,
    points: Mapping[str, Mapping[str, Any]],
) -> Dict[Tuple[str, str], float]:
    if collection is None or not points:
        return {}
    dates = {
        str(point.get(field) or "")
        for point in points.values()
        for field in ("first_date", "last_date")
        if point.get(field)
    }
    if not dates:
        return {}
    cursor = collection.find(
        {
            "symbol": {"$in": list(points)},
            "trade_date": {"$in": list(dates)},
        },
        {"_id": 0, "symbol": 1, "trade_date": 1, "adj_factor": 1},
    )
    out: Dict[Tuple[str, str], float] = {}
    for row in cursor:
        factor = _as_float(row.get("adj_factor"))
        if factor is None or factor <= 0:
            continue
        out[(str(row.get("symbol") or ""), str(row.get("trade_date") or ""))] = factor
    return out


def _ytd_returns(
    points: Mapping[str, Mapping[str, Any]],
    factors: Mapping[Tuple[str, str], float],
) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for symbol, point in points.items():
        first_close = _as_float(point.get("first_close"))
        last_close = _as_float(point.get("last_close"))
        first_date = str(point.get("first_date") or "")
        last_date = str(point.get("last_date") or "")
        if (
            first_close is None
            or last_close is None
            or first_close <= 0
            or last_close <= 0
        ):
            continue
        first_factor = factors.get((symbol, first_date))
        last_factor = factors.get((symbol, last_date))
        degraded = first_factor is None or last_factor is None
        if degraded:
            ratio = last_close / first_close
        else:
            ratio = (last_close * last_factor) / (first_close * first_factor)
        out[symbol] = {
            "ytd_return_pct": round((ratio - 1.0) * 100.0, 2),
            "ytd_price_date": last_date,
            "ytd_adjustment_degraded": degraded,
        }
    return out


def build_candidate_contexts(
    rows: Sequence[Mapping[str, Any]],
    *,
    finance_collections: Mapping[str, Any],
    data_collections: Mapping[str, Any],
    as_of: Optional[date] = None,
) -> Dict[str, Dict[str, Any]]:
    """Return per-symbol score, earnings and adjusted YTD context in batches."""
    symbols = _a_share_symbols(rows)
    if not symbols:
        return {}
    today = as_of or date.today()
    scores = _latest_scores(finance_collections.get("stock_scores"), symbols)
    financials = _latest_financials(
        data_collections.get("financial_indicator"),
        symbols,
    )
    points = _ytd_price_points(
        data_collections.get("volume_price"),
        symbols,
        f"{today.year}0101",
    )
    factors = _adjustment_factors(
        data_collections.get("stock_adj_factor"),
        points,
    )
    returns = _ytd_returns(points, factors)

    return {
        symbol: {
            "score": scores.get(symbol) or {},
            "financials": financials.get(symbol) or [],
            **(returns.get(symbol) or {}),
        }
        for symbol in symbols
    }
