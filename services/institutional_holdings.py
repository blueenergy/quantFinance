"""Aggregate top10 float-holder disclosures by institutional category."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from services.fund_inventory import yuan_to_yi
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
    return {"amount": 0.0, "ratio": 0.0, "float_ratio": 0.0, "holders": set()}


def _aggregate(
    rows: Iterable[Mapping[str, Any]],
    categories: Sequence[str],
) -> tuple[Dict[str, Dict[str, Dict[str, Any]]], Dict[str, Dict[str, Dict[str, Any]]]]:
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
        amount = _number(row, "hold_amount") or 0.0
        ratio = _number(row, "hold_ratio") or 0.0
        float_ratio = _number(row, "hold_float_ratio") or 0.0

        stock_bucket = by_stock[category].setdefault(ts_code, _empty_stock())
        stock_bucket["amount"] += amount
        stock_bucket["ratio"] += ratio
        stock_bucket["float_ratio"] += float_ratio
        stock_bucket["holders"].add(holder_name)

        holder_bucket = by_holder[category].setdefault(
            holder_name,
            {"amount": 0.0, "stocks": set()},
        )
        holder_bucket["amount"] += amount
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
    current_amount: float,
    previous_amount: float,
    has_current: bool,
    has_previous: bool,
) -> str:
    """Classify by disclosed share ratio, falling back to amount.

    Tushare ``hold_amount`` is not always the report-date market value (and for
    strategic stakes it can be a fixed carrying value), so ratio change is the
    primary signal for share-count changes.
    """
    if has_current and not has_previous:
        return "new"
    if has_previous and not has_current:
        return "exited"
    if current_ratio != previous_ratio:
        return "increased" if current_ratio > previous_ratio else "decreased"
    if current_amount != previous_amount:
        return "increased" if current_amount > previous_amount else "decreased"
    return "unchanged"


def build_institutional_holdings_summary(
    current_rows: Sequence[Mapping[str, Any]],
    previous_rows: Sequence[Mapping[str, Any]],
    *,
    period: str,
    previous_period: Optional[str],
    categories: Sequence[str] = ALL_CATEGORIES,
    stock_info: Optional[Mapping[str, Mapping[str, Any]]] = None,
    industry: Optional[str] = None,
    new_only: bool = False,
    top_n: int = 20,
) -> Dict[str, Any]:
    """Build a category-level summary from two holder periods.

    ``current_rows`` / ``previous_rows`` are ``top10_floatholders`` documents.
    Amounts are converted to 亿元; ratios are percentage points. This is a
    top-10 disclosure view, not a complete holdings dataset.
    """
    top_n = max(1, min(int(top_n or 20), 100))
    meta = _stock_meta(stock_info or {})
    current_by_stock, current_by_holder = _aggregate(current_rows, categories)
    previous_by_stock, _ = _aggregate(previous_rows, categories)
    current_by_stock = _filter_industry(current_by_stock, meta, industry)
    previous_by_stock = _filter_industry(previous_by_stock, meta, industry)

    by_category: Dict[str, Dict[str, Any]] = {}
    top_stocks: Dict[str, List[Dict[str, Any]]] = {}
    top_exited: Dict[str, List[Dict[str, Any]]] = {}
    top_holders: Dict[str, List[Dict[str, Any]]] = {}
    all_current_codes: set[str] = set()
    all_holders: set[str] = set()
    total_amount = 0.0

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
        net_change = 0.0
        for code in codes:
            cur = current.get(code)
            prev = previous.get(code)
            cur_amount = float((cur or {}).get("amount") or 0.0)
            prev_amount = float((prev or {}).get("amount") or 0.0)
            change = cur_amount - prev_amount
            cur_ratio = float((cur or {}).get("ratio") or 0.0)
            prev_ratio = float((prev or {}).get("ratio") or 0.0)
            ratio_change = cur_ratio - prev_ratio
            kind = _change_type(
                cur_ratio,
                prev_ratio,
                cur_amount,
                prev_amount,
                cur is not None,
                prev is not None,
            )
            counts[kind] = counts.get(kind, 0) + 1
            net_change += change
            info = meta.get(code) or {}
            row = {
                "ts_code": code,
                "name": info.get("name") or "",
                "industry": info.get("industry") or "",
                "holder_count": len((cur or {}).get("holders") or []),
                "hold_amount_yi": round(cur_amount / _YI, 2),
                "hold_ratio_sum": round(cur_ratio, 2),
                "previous_hold_ratio_sum": round(prev_ratio, 2),
                "change_ratio_pp": round(ratio_change, 2),
                "hold_float_ratio_sum": round(
                    float((cur or {}).get("float_ratio") or 0.0), 2
                ),
                "change_amount_yi": round(change / _YI, 2),
                "change_type": kind,
            }
            if cur is not None:
                stock_rows.append(row)
            if kind == "exited":
                exited_rows.append(
                    {
                        **row,
                        "hold_amount_yi": round(prev_amount / _YI, 2),
                        "change_amount_yi": round(change / _YI, 2),
                    }
                )

        sorted_stocks = sorted(
            stock_rows,
            key=lambda row: row["hold_amount_yi"] or 0.0,
            reverse=True,
        )
        if new_only:
            sorted_stocks = [row for row in sorted_stocks if row["change_type"] == "new"]
        top_stocks[category] = sorted_stocks[:top_n]
        top_exited[category] = sorted(
            exited_rows,
            key=lambda row: row["change_amount_yi"] or 0.0,
        )[:top_n]

        holder_rows = [
            {
                "holder_name": holder_name,
                "category": category,
                "stocks": len(bucket["stocks"]),
                "hold_amount_yi": round(float(bucket["amount"]) / _YI, 2),
            }
            for holder_name, bucket in holders.items()
        ]
        top_holders[category] = sorted(
            holder_rows,
            key=lambda row: row["hold_amount_yi"] or 0.0,
            reverse=True,
        )[:top_n]

        amount = sum(float(bucket["amount"]) for bucket in current.values())
        total_amount += amount
        by_category[category] = {
            "label": category_label(category),
            "stocks": len(current),
            "holders": len(holders),
            "hold_amount_yi": round(amount / _YI, 2),
            "net_change_amount_yi": round(net_change / _YI, 2),
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
        "仅覆盖前十大流通股东；金额和比例是披露口径的下界，不是完整持仓。",
        "报告期数据有披露滞后，不代表今日持仓。",
        "市值变化包含股价影响，不能直接当作增减持金额。",
        "分类基于股东名称规则，可能存在个别误判；合资公募产品已排除。",
    ]
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
            "total_hold_amount_yi": round(total_amount / _YI, 2),
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
