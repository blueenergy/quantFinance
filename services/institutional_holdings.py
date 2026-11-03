"""Aggregate top10 float-holder disclosures by institutional category."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from services.holder_taxonomy import (
    ALL_CATEGORIES,
    category_label,
    classify_holder,
)

_YI = 1e8


def _number(row: Mapping[str, Any], key: str) -> Optional[float]:
    value = row.get(key)
    if value is None and isinstance(row.get("raw"), Mapping):
        value = row["raw"].get(key)
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _empty_stock() -> Dict[str, Any]:
    return {
        "shares": 0.0,
        "value": 0.0,
        "has_value": False,
        "ratio": 0.0,
        "float_ratio": 0.0,
        "holders": set(),
        "price": None,
    }


def _aggregate(
    rows: Iterable[Mapping[str, Any]],
    categories: Sequence[str],
    prices: Optional[Mapping[str, float]] = None,
) -> tuple[Dict[str, Dict[str, Dict[str, Any]]], Dict[str, Dict[str, Dict[str, Any]]]]:
    """Aggregate shares, market value and ratios by stock and holder.

    ``hold_amount`` in Tushare ``top10_floatholders`` is a share count, not a
    yuan amount. Market value is estimated as ``shares * report_date_close``.
    """
    prices = prices or {}
    by_stock: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(dict)
    by_holder: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        category = classify_holder(row.get("holder_name"))
        if category not in categories:
            continue
        ts_code = str(row.get("ts_code") or "").strip()
        holder_name = str(row.get("holder_name") or "").strip()
        if not ts_code or not holder_name:
            continue
        shares = _number(row, "hold_amount") or 0.0
        ratio = _number(row, "hold_ratio") or 0.0
        float_ratio = _number(row, "hold_float_ratio") or 0.0
        close = prices.get(ts_code)
        value = shares * close if close is not None and close > 0 else None

        stock_bucket = by_stock[category].setdefault(ts_code, _empty_stock())
        stock_bucket["shares"] += shares
        stock_bucket["ratio"] += ratio
        stock_bucket["float_ratio"] += float_ratio
        stock_bucket["holders"].add(holder_name)
        if value is not None:
            stock_bucket["value"] += value
            stock_bucket["has_value"] = True
            stock_bucket["price"] = close

        holder_bucket = by_holder[category].setdefault(
            holder_name,
            {"shares": 0.0, "value": 0.0, "has_value": False, "stocks": set()},
        )
        holder_bucket["shares"] += shares
        if value is not None:
            holder_bucket["value"] += value
            holder_bucket["has_value"] = True
        holder_bucket["stocks"].add(ts_code)
    return by_stock, by_holder


def _stock_meta(stock_info: Mapping[str, Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {
        str(code): {
            "name": (meta or {}).get("name") or "",
            "industry": (meta or {}).get("industry") or "",
        }
        for code, meta in stock_info.items()
    }


def _filter_industry(
    by_stock: Dict[str, Dict[str, Dict[str, Any]]],
    meta: Mapping[str, Mapping[str, Any]],
    industry: Optional[str],
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    if not industry:
        return by_stock
    out: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for category, stocks in by_stock.items():
        filtered = {
            code: bucket
            for code, bucket in stocks.items()
            if str((meta.get(code) or {}).get("industry") or "") == industry
        }
        if filtered:
            out[category] = filtered
    return out


def _change_type(
    current_ratio: float,
    previous_ratio: float,
    current_shares: float,
    previous_shares: float,
    has_current: bool,
    has_previous: bool,
) -> str:
    """Classify by disclosed share ratio, falling back to share count.

    Ratio change is the primary signal: market value changes include price
    moves and Tushare's strategic stakes can carry non-market amounts.
    """
    if has_current and not has_previous:
        return "new"
    if has_previous and not has_current:
        return "exited"
    if current_ratio != previous_ratio:
        return "increased" if current_ratio > previous_ratio else "decreased"
    if current_shares != previous_shares:
        return "increased" if current_shares > previous_shares else "decreased"
    return "unchanged"


def _market_value_yi(bucket: Optional[Mapping[str, Any]]) -> Optional[float]:
    if not bucket or not bucket.get("has_value"):
        return None
    return round(float(bucket.get("value") or 0.0) / _YI, 2)


def build_institutional_holdings_summary(
    current_rows: Sequence[Mapping[str, Any]],
    previous_rows: Sequence[Mapping[str, Any]],
    *,
    period: str,
    previous_period: Optional[str],
    categories: Sequence[str] = ALL_CATEGORIES,
    stock_info: Optional[Mapping[str, Mapping[str, Any]]] = None,
    current_prices: Optional[Mapping[str, float]] = None,
    previous_prices: Optional[Mapping[str, float]] = None,
    industry: Optional[str] = None,
    new_only: bool = False,
    top_n: int = 20,
) -> Dict[str, Any]:
    """Build a category-level summary from two holder periods.

    ``current_rows`` / ``previous_rows`` are ``top10_floatholders`` documents.
    ``hold_amount`` is a share count; ``hold_shares_yi`` is 亿股 and
    ``hold_amount_yi`` is an estimated 亿元 market value using the report-date
    close. Ratios are percentage points. This is a top-10 disclosure view, not
    a complete holdings dataset.
    """
    top_n = max(1, min(int(top_n or 20), 100))
    meta = _stock_meta(stock_info or {})
    current_by_stock, current_by_holder = _aggregate(
        current_rows, categories, current_prices
    )
    previous_by_stock, _ = _aggregate(
        previous_rows, categories, previous_prices
    )
    current_by_stock = _filter_industry(current_by_stock, meta, industry)
    previous_by_stock = _filter_industry(previous_by_stock, meta, industry)

    by_category: Dict[str, Dict[str, Any]] = {}
    top_stocks: Dict[str, List[Dict[str, Any]]] = {}
    top_exited: Dict[str, List[Dict[str, Any]]] = {}
    top_holders: Dict[str, List[Dict[str, Any]]] = {}
    all_current_codes: set[str] = set()
    all_holders: set[str] = set()
    total_value = 0.0
    total_shares = 0.0
    total_missing_price = 0

    for category in categories:
        current = current_by_stock.get(category, {})
        previous = previous_by_stock.get(category, {})
        codes = sorted(set(current) | set(previous))
        all_current_codes.update(current)
        holders = current_by_holder.get(category, {})
        all_holders.update(holders)

        stock_rows: List[Dict[str, Any]] = []
        exited_rows: List[Dict[str, Any]] = []
        counts = {"new": 0, "exited": 0, "increased": 0, "decreased": 0, "unchanged": 0}
        net_shares = 0.0
        net_value = 0.0
        priced_value_stocks = 0
        for code in codes:
            cur = current.get(code)
            prev = previous.get(code)
            cur_shares = float((cur or {}).get("shares") or 0.0)
            prev_shares = float((prev or {}).get("shares") or 0.0)
            cur_ratio = float((cur or {}).get("ratio") or 0.0)
            prev_ratio = float((prev or {}).get("ratio") or 0.0)
            ratio_change = cur_ratio - prev_ratio
            kind = _change_type(
                cur_ratio,
                prev_ratio,
                cur_shares,
                prev_shares,
                cur is not None,
                prev is not None,
            )
            counts[kind] = counts.get(kind, 0) + 1
            net_shares += cur_shares - prev_shares

            cur_value = _market_value_yi(cur)
            prev_value = _market_value_yi(prev)
            if cur_value is not None and prev_value is not None:
                change_value = cur_value - prev_value
                priced_value_stocks += 1
            elif cur_value is not None:
                change_value = cur_value
                priced_value_stocks += 1
            elif prev_value is not None:
                change_value = -prev_value
                priced_value_stocks += 1
            else:
                change_value = None
            if change_value is not None:
                net_value += change_value

            info = meta.get(code) or {}
            row = {
                "ts_code": code,
                "name": info.get("name") or "",
                "industry": info.get("industry") or "",
                "holder_count": len((cur or {}).get("holders") or []),
                "hold_shares_yi": round(cur_shares / _YI, 2),
                "hold_amount_yi": cur_value,
                "previous_hold_shares_yi": round(prev_shares / _YI, 2),
                "previous_hold_amount_yi": prev_value,
                "change_shares_yi": round((cur_shares - prev_shares) / _YI, 2),
                "change_amount_yi": change_value,
                "hold_ratio_sum": round(cur_ratio, 2),
                "previous_hold_ratio_sum": round(prev_ratio, 2),
                "change_ratio_pp": round(ratio_change, 2),
                "hold_float_ratio_sum": round(
                    float((cur or {}).get("float_ratio") or 0.0), 2
                ),
                "price": (cur or {}).get("price") or (prev or {}).get("price"),
                "price_missing": bool(
                    (cur is not None and cur_value is None)
                    or (prev is not None and prev_value is None)
                ),
                "change_type": kind,
            }
            if cur is not None:
                stock_rows.append(row)
            if kind == "exited":
                exited_rows.append(row)

        sorted_stocks = sorted(
            stock_rows,
            key=lambda row: (
                row["hold_amount_yi"] if row["hold_amount_yi"] is not None else -1.0,
                row["hold_shares_yi"] or 0.0,
            ),
            reverse=True,
        )
        if new_only:
            sorted_stocks = [row for row in sorted_stocks if row["change_type"] == "new"]
        top_stocks[category] = sorted_stocks[:top_n]
        top_exited[category] = sorted(
            exited_rows,
            key=lambda row: (
                row["change_amount_yi"]
                if row["change_amount_yi"] is not None
                else -1.0
            ),
        )[:top_n]

        holder_rows = [
            {
                "holder_name": holder_name,
                "category": category,
                "stocks": len(bucket["stocks"]),
                "hold_shares_yi": round(float(bucket["shares"]) / _YI, 2),
                "hold_amount_yi": (
                    round(float(bucket["value"]) / _YI, 2)
                    if bucket.get("has_value")
                    else None
                ),
                "price_missing": not bucket.get("has_value"),
            }
            for holder_name, bucket in holders.items()
        ]
        top_holders[category] = sorted(
            holder_rows,
            key=lambda row: (
                row["hold_amount_yi"] if row["hold_amount_yi"] is not None else -1.0,
                row["hold_shares_yi"] or 0.0,
            ),
            reverse=True,
        )[:top_n]

        category_value = sum(
            float(bucket["value"]) for bucket in current.values() if bucket.get("has_value")
        )
        category_shares = sum(float(bucket["shares"]) for bucket in current.values())
        priced_stocks = sum(1 for bucket in current.values() if bucket.get("has_value"))
        missing_price = len(current) - priced_stocks
        total_value += category_value
        total_shares += category_shares
        total_missing_price += missing_price
        by_category[category] = {
            "label": category_label(category),
            "stocks": len(current),
            "holders": len(holders),
            "hold_shares_yi": round(category_shares / _YI, 2),
            "hold_amount_yi": round(category_value / _YI, 2) if priced_stocks else None,
            "net_change_shares_yi": round(net_shares / _YI, 2),
            "net_change_amount_yi": round(net_value, 2) if priced_value_stocks else None,
            "priced_stocks": priced_stocks,
            "missing_price_stocks": missing_price,
            "new_stocks": counts["new"],
            "exited_stocks": counts["exited"],
            "increased_stocks": counts["increased"],
            "decreased_stocks": counts["decreased"],
            "unchanged_stocks": counts["unchanged"],
        }

    current_codes = set().union(*[set(stocks) for stocks in current_by_stock.values()]) if current_by_stock else set()
    previous_codes = set().union(*[set(stocks) for stocks in previous_by_stock.values()]) if previous_by_stock else set()
    coverage_ratio = (
        len(previous_codes) / len(current_codes) if current_codes else 0.0
    )
    comparison_reliable = bool(
        current_codes and previous_codes and coverage_ratio >= 0.8
    )

    caveats = [
        "仅覆盖前十大流通股东；股数、市值和比例都是披露口径的下界，不是完整持仓。",
        "hold_shares_yi 为亿股；hold_amount_yi 为股数 × 报告期收盘价估算的亿元市值。",
        "金额变化包含股价影响；增减持以 hold_ratio 变化（pp）为准。",
        "报告期数据有披露滞后，不代表今日持仓。",
        "分类基于股东名称规则，可能存在个别误判；合资公募产品已排除。",
    ]
    if total_missing_price:
        caveats.append(
            f"{total_missing_price} 只股票缺少报告期收盘价，市值和金额变化只覆盖有价格的股票。"
        )
    if not comparison_reliable:
        caveats.append(
            "上一期覆盖不足"
            f"（{len(previous_codes)}/{len(current_codes)}，{coverage_ratio:.0%}），"
            "新进/退出/增减持统计不可靠；请先回补上一期 top10_floatholders。"
        )
    if "hksc" in categories:
        caveats.append("香港中央结算为北向通道，不是单一机构。")
    if new_only:
        caveats.append("top_stocks 仅展示较上一期新进的股票；汇总统计仍为全量。")
    if industry:
        caveats.append(f"已按行业「{industry}」过滤; 未匹配行业的股票不参与统计。")

    return {
        "period": period,
        "previous_period": previous_period,
        "categories": [
            {"key": category, "label": category_label(category)}
            for category in categories
        ],
        "summary": {
            "total_stocks": len(all_current_codes),
            "total_holders": len(all_holders),
            "total_hold_shares_yi": round(total_shares / _YI, 2),
            "total_hold_amount_yi": round(total_value / _YI, 2),
            "total_missing_price_stocks": total_missing_price,
            "by_category": by_category,
        },
        "comparison": {
            "current_stocks": len(current_codes),
            "previous_stocks": len(previous_codes),
            "previous_coverage_ratio": round(coverage_ratio, 3),
            "reliable": comparison_reliable,
        },
        "top_stocks": top_stocks,
        "top_exited_stocks": top_exited,
        "top_holders": top_holders,
        "caveats": caveats,
    }
