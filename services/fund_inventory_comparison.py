"""Aggregate one fund-inventory period pair into a quarter-over-quarter view."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from services.fund_inventory import (
    PREVIEW_STATE_HINTS,
    PREVIEW_STATE_LABELS,
    STATE_HINTS,
    STATE_LABELS,
    pct_points,
    yuan_to_yi,
)

STATE_ORDER = (
    "incumbent_strengthen",
    "new_inventory",
    "inventory_turnover",
    "inventory_exit",
)
_FLOW_FIELDS = (
    "mv_prev",
    "mv_curr",
    "surface_mv_chg",
    "mv_price_effect",
    "net_ex_price",
    "new_inflow",
    "incumbent_delta",
    "exited_prev_mv",
)


def _num(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sum(rows: Iterable[Mapping[str, Any]], field: str) -> Optional[float]:
    values = [_num(row.get(field)) for row in rows]
    present = [value for value in values if value is not None]
    if not present:
        return None
    return sum(present)


def _yi(value: Optional[float]) -> Optional[float]:
    return yuan_to_yi(value)


def _stock_view(row: Mapping[str, Any], *, preview: bool) -> Dict[str, Any]:
    state = row.get("state")
    labels = PREVIEW_STATE_LABELS if preview else STATE_LABELS
    hints = PREVIEW_STATE_HINTS if preview else STATE_HINTS
    return {
        "symbol": row.get("symbol"),
        "ts_code": row.get("ts_code"),
        "name": row.get("name"),
        "industry": row.get("industry") or "未分类",
        "market": row.get("market")
        or ("HK" if str(row.get("ts_code") or "").endswith(".HK") else "A"),
        "state": state,
        "state_label": labels.get(state) or row.get("state_label") or state,
        "state_hint": hints.get(state, ""),
        "price_ret_pct": pct_points(row.get("price_ret")),
        "mv_prev_yi": _yi(_num(row.get("mv_prev"))),
        "mv_curr_yi": _yi(_num(row.get("mv_curr"))),
        "surface_mv_chg_yi": _yi(_num(row.get("surface_mv_chg"))),
        "mv_price_effect_yi": _yi(_num(row.get("mv_price_effect"))),
        "net_ex_price_yi": _yi(_num(row.get("net_ex_price"))),
        "new_inflow_yi": _yi(_num(row.get("new_inflow"))),
        "incumbent_delta_yi": _yi(_num(row.get("incumbent_delta"))),
        "exited_prev_mv_yi": _yi(_num(row.get("exited_prev_mv"))),
        "n_funds_prev": row.get("n_funds_prev"),
        "n_funds_curr": row.get("n_funds_curr"),
        "n_new": row.get("n_new"),
        "n_exited": row.get("n_exited"),
        "n_incumbent": row.get("n_incumbent"),
    }


def _flow_summary(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    return {
        "stocks": len(rows),
        "mv_prev_yi": _yi(_sum(rows, "mv_prev")),
        "mv_curr_yi": _yi(_sum(rows, "mv_curr")),
        "surface_mv_chg_yi": _yi(_sum(rows, "surface_mv_chg")),
        "mv_price_effect_yi": _yi(_sum(rows, "mv_price_effect")),
        "net_ex_price_yi": _yi(_sum(rows, "net_ex_price")),
        "new_inflow_yi": _yi(_sum(rows, "new_inflow")),
        "incumbent_delta_yi": _yi(_sum(rows, "incumbent_delta")),
        "exited_prev_mv_yi": _yi(_sum(rows, "exited_prev_mv")),
    }


def _abs_net(row: Mapping[str, Any]) -> float:
    value = _num(row.get("net_ex_price"))
    return abs(value) if value is not None else 0.0


def _top(
    rows: Sequence[Mapping[str, Any]],
    *,
    key,
    reverse: bool,
    limit: int,
    preview: bool,
) -> List[Dict[str, Any]]:
    ordered = sorted(rows, key=key, reverse=reverse)
    return [_stock_view(row, preview=preview) for row in ordered[:limit]]


def build_period_comparison(
    rows: Sequence[Mapping[str, Any]],
    *,
    period_curr: str,
    period_prev: str,
    quality: Optional[Mapping[str, Any]] = None,
    quality_status: str = "official",
    comparison_scope: str = "full",
    compare_mode: Optional[str] = None,
    top_n: int = 10,
    market: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a deterministic comparison summary from snapshot rows.

    ``rows`` are raw ``fund_inventory_snapshots`` documents (amounts in yuan).
    The result is presentation-ready: all amounts are in 亿元 and all ratios in
    percentage points. No candidate/LLM classification is performed here.
    """
    quality = quality or {}
    preview = comparison_scope == "top10" or quality_status == "provisional"
    top_n = max(1, min(int(top_n or 10), 50))

    usable = [row for row in rows if bool(row.get("price_adjusted", True))]
    if market in {"A", "HK"}:
        usable = [
            row
            for row in usable
            if str(row.get("market") or ("HK" if str(row.get("ts_code") or "").endswith(".HK") else "A"))
            == market
        ]

    by_state: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    by_industry: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for row in usable:
        state = str(row.get("state") or "unavailable")
        by_state[state].append(row)
        by_industry[str(row.get("industry") or "未分类")].append(row)

    state_counts = {state: len(by_state.get(state, [])) for state in STATE_ORDER}
    unavailable = len(by_state.get("unavailable", []))
    if unavailable:
        state_counts["unavailable"] = unavailable

    state_flow: Dict[str, Dict[str, Any]] = {}
    for state, state_rows in by_state.items():
        state_flow[state] = _flow_summary(state_rows)

    industry_rows: List[Dict[str, Any]] = []
    for industry, industry_rows_raw in by_industry.items():
        summary = _flow_summary(industry_rows_raw)
        summary["industry"] = industry
        summary["state_counts"] = {
            state: sum(1 for row in industry_rows_raw if str(row.get("state") or "unavailable") == state)
            for state in STATE_ORDER
        }
        industry_rows.append(summary)
    industry_rows.sort(
        key=lambda row: (
            row.get("net_ex_price_yi") is None,
            -(row.get("net_ex_price_yi") or 0.0),
            row.get("industry") or "",
        )
    )

    missing_flow = [row for row in usable if _num(row.get("net_ex_price")) is None]
    totals = _flow_summary(usable)
    totals["stocks_with_flow"] = len(usable) - len(missing_flow)
    totals["stocks_missing_flow"] = len(missing_flow)
    totals["state_counts"] = state_counts

    new_rows = by_state.get("new_inventory", [])
    exit_rows = by_state.get("inventory_exit", [])
    strengthen_rows = by_state.get("incumbent_strengthen", [])
    turnover_rows = by_state.get("inventory_turnover", [])

    caveats: List[str] = [
        "金额由基金报告持仓市值推导，不是实际成交金额。",
        "净增减 = 表面市值变化 − 股价贡献，是估算值。",
        "状态按报告期截面推导，反映披露截止时的筹码位置，不是今日买卖。",
    ]
    if preview:
        caveats.append("季报口径：两期均按前十大持仓对齐；未披露基金不会被视为退出。")
    if not preview and quality.get("holding_depth_pct") is not None:
        caveats.append(
            f"持仓深度 {quality.get('holding_depth_pct')}%：相对上一期全持仓的中位持仓覆盖比例。"
        )
    if any(str(row.get("market") or "").upper() == "HK" for row in usable):
        caveats.append("港股价格由基金报告推导，暂未校正拆股等公司行动。")
    if missing_flow:
        caveats.append(f"{len(missing_flow)} 只股票缺少完整价格/复权因子，未计入净增减汇总。")

    return {
        "period_curr": period_curr,
        "period_prev": period_prev,
        "compare_mode": compare_mode,
        "comparison_scope": comparison_scope,
        "quality_status": quality_status,
        "quality": dict(quality),
        "market": market,
        "top_n": top_n,
        "totals": totals,
        "state_counts": state_counts,
        "state_flow": state_flow,
        "industries": industry_rows,
        "top": {
            "net_inflow": _top(
                usable,
                key=lambda row: _num(row.get("net_ex_price")) or 0.0,
                reverse=True,
                limit=top_n,
                preview=preview,
            ),
            "net_outflow": _top(
                usable,
                key=lambda row: _num(row.get("net_ex_price")) or 0.0,
                reverse=False,
                limit=top_n,
                preview=preview,
            ),
            "new_inventory": _top(
                new_rows,
                key=lambda row: (
                    _num(row.get("new_inflow")) or 0.0,
                    _num(row.get("net_ex_price")) or 0.0,
                ),
                reverse=True,
                limit=top_n,
                preview=preview,
            ),
            "inventory_exit": _top(
                exit_rows,
                key=lambda row: _num(row.get("net_ex_price")) or 0.0,
                reverse=False,
                limit=top_n,
                preview=preview,
            ),
            "incumbent_strengthen": _top(
                strengthen_rows,
                key=lambda row: _num(row.get("net_ex_price")) or 0.0,
                reverse=True,
                limit=top_n,
                preview=preview,
            ),
            "inventory_turnover": _top(
                turnover_rows,
                key=_abs_net,
                reverse=True,
                limit=top_n,
                preview=preview,
            ),
        },
        "caveats": caveats,
    }
