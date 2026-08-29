"""主动公募库存快照：读 Mongo、换算亿元、回答四个持仓问题。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

YI = 1e8

STATE_LABELS = {
    "incumbent_strengthen": "存量强化",
    "new_inventory": "新建库存",
    "inventory_turnover": "库存换手",
    "inventory_exit": "库存退出",
}

STATE_HINTS = {
    "incumbent_strengthen": "剔除股价后，净增加主要来自已持有该股的基金。",
    "new_inventory": "剔除股价后，净增加主要来自本期新出现的基金。",
    "inventory_turnover": "新基金进入的同时，原有基金持仓下降，内部发生换手。",
    "inventory_exit": "剔除股价后，主动基金整体持仓下降。",
}


def yuan_to_yi(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return round(float(value) / YI, 2)
    except (TypeError, ValueError):
        return None


def pct_points(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return round(float(value) * 100.0, 2)
    except (TypeError, ValueError):
        return None


def public_snapshot(doc: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not doc:
        return None
    price_adjusted = bool(doc.get("price_adjusted"))
    state = doc.get("state") if price_adjusted else None
    return {
        "symbol": doc.get("symbol"),
        "ts_code": doc.get("ts_code"),
        "name": doc.get("name"),
        "industry": doc.get("industry"),
        "period_prev": doc.get("period_prev"),
        "period_curr": doc.get("period_curr"),
        "compare_mode": doc.get("compare_mode"),
        "disclosure_prev": doc.get("disclosure_prev"),
        "disclosure_curr": doc.get("disclosure_curr"),
        "price_adjusted": price_adjusted,
        "analysis_available": price_adjusted,
        "price_ret_pct": pct_points(doc.get("price_ret")),
        "state": state,
        "state_label": (
            doc.get("state_label") or STATE_LABELS.get(state) or state
            if price_adjusted
            else "数据不足"
        ),
        "state_hint": (
            STATE_HINTS.get(state, "")
            if price_adjusted
            else "缺少完整行情或复权因子，未计算库存四态。"
        ),
        "mv_prev_yi": yuan_to_yi(doc.get("mv_prev")),
        "mv_curr_yi": yuan_to_yi(doc.get("mv_curr")),
        "surface_mv_chg_yi": yuan_to_yi(doc.get("surface_mv_chg")),
        "mv_price_effect_yi": yuan_to_yi(doc.get("mv_price_effect")),
        "net_ex_price_yi": yuan_to_yi(doc.get("net_ex_price")) if price_adjusted else None,
        "new_inflow_yi": yuan_to_yi(doc.get("new_inflow")),
        "incumbent_delta_yi": (
            yuan_to_yi(doc.get("incumbent_delta")) if price_adjusted else None
        ),
        "exited_prev_mv_yi": yuan_to_yi(doc.get("exited_prev_mv")),
        "n_funds_prev": doc.get("n_funds_prev"),
        "n_funds_curr": doc.get("n_funds_curr"),
        "n_new": doc.get("n_new"),
        "n_exited": doc.get("n_exited"),
        "n_incumbent": doc.get("n_incumbent"),
        "lag_note": (
            f"数据截止报告期 {doc.get('period_curr')}，"
            "反映的是季报结束后筹码堆在哪里，不是今日买卖。"
        ),
        "four_questions": _four_questions(doc) if price_adjusted else [],
    }


def _four_questions(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        {
            "id": "price_effect",
            "question": "股价上涨贡献了多少持仓市值？",
            "value_yi": yuan_to_yi(doc.get("mv_price_effect")),
            "detail": f"区间涨跌 {pct_points(doc.get('price_ret')) or 0}%",
        },
        {
            "id": "net_shares",
            "question": "剔除股价以后，机构有没有增加持股？",
            "value_yi": yuan_to_yi(doc.get("net_ex_price")),
            "detail": "正数为估算净增持，负数为净减持（亿元）",
        },
        {
            "id": "new_vs_incumbent",
            "question": "新增筹码是原来的老机构买的，还是新基金买的？",
            "value_yi": yuan_to_yi(doc.get("new_inflow")),
            "detail": (
                f"新进 {yuan_to_yi(doc.get('new_inflow'))} 亿，"
                f"存量变化 {yuan_to_yi(doc.get('incumbent_delta'))} 亿"
            ),
        },
        {
            "id": "incumbent_selling",
            "question": "新基金进场的同时，老机构还有没有在卖？",
            "value_yi": yuan_to_yi(doc.get("incumbent_delta")),
            "detail": "存量变化为负即老机构净卖出（含退出）",
        },
    ]
