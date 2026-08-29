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
PREVIEW_STATE_LABELS = {
    "incumbent_strengthen": "前十大强化",
    "new_inventory": "新进前十大",
    "inventory_turnover": "前十大换手",
    "inventory_exit": "退出前十大",
}
PREVIEW_STATE_HINTS = {
    "incumbent_strengthen": "已披露基金的前十大持仓净增加，主要来自原有基金。",
    "new_inventory": "已披露基金的前十大持仓净增加，主要来自本期新出现的基金。",
    "inventory_turnover": "已披露基金中，新基金进入前十大时原有基金的前十大持仓下降。",
    "inventory_exit": "仅表示已披露基金的前十大持仓下降，不代表全市场基金已经退出。",
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
    quality = doc.get("quality") if isinstance(doc.get("quality"), dict) else {}
    quality_status = doc.get("quality_status") or quality.get("status") or "official"
    provisional = quality_status == "provisional"
    market = doc.get("market") or (
        "HK" if str(doc.get("ts_code") or "").endswith(".HK") else "A"
    )
    state_labels = PREVIEW_STATE_LABELS if provisional else STATE_LABELS
    state_hints = PREVIEW_STATE_HINTS if provisional else STATE_HINTS
    return {
        "symbol": doc.get("symbol"),
        "ts_code": doc.get("ts_code"),
        "name": doc.get("name"),
        "industry": doc.get("industry"),
        "market": market,
        "period_prev": doc.get("period_prev"),
        "period_curr": doc.get("period_curr"),
        "compare_mode": doc.get("compare_mode"),
        "comparison_scope": doc.get("comparison_scope") or quality.get("comparison_scope"),
        "quality_status": quality_status,
        "quality": quality,
        "disclosure_prev": doc.get("disclosure_prev"),
        "disclosure_curr": doc.get("disclosure_curr"),
        "price_adjusted": price_adjusted,
        "price_source": doc.get("price_source"),
        "corporate_action_adjusted": doc.get("corporate_action_adjusted"),
        "price_note": doc.get("price_note"),
        "analysis_available": price_adjusted,
        "price_ret_pct": pct_points(doc.get("price_ret")),
        "state": state,
        "state_label": (
            state_labels.get(state) or doc.get("state_label") or state
            if price_adjusted
            else "数据不足"
        ),
        "state_hint": (
            state_hints.get(state, "")
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
            (
                f"数据截止报告期 {doc.get('period_curr')}，当前是前十大预览；"
                "未披露基金未计入退出，不代表完整全持仓。"
            )
            if provisional
            else (
                f"数据截止报告期 {doc.get('period_curr')}，"
                "反映的是季报结束后筹码堆在哪里，不是今日买卖。"
            )
        ) + (
            " 港股按报告内隐价格剔除价格和汇率影响，暂未校正拆股等公司行动。"
            if market == "HK"
            else ""
        ),
        "four_questions": _four_questions(doc, provisional=provisional) if price_adjusted else [],
    }


def _four_questions(
    doc: Dict[str, Any],
    *,
    provisional: bool = False,
) -> List[Dict[str, Any]]:
    scope = "前十大" if provisional else ""
    return [
        {
            "id": "price_effect",
            "question": f"股价上涨贡献了多少{scope}持仓市值？",
            "value_yi": yuan_to_yi(doc.get("mv_price_effect")),
            "detail": f"区间涨跌 {pct_points(doc.get('price_ret')) or 0}%",
        },
        {
            "id": "net_shares",
            "question": f"剔除股价以后，已披露基金的{scope}持仓有没有增加？",
            "value_yi": yuan_to_yi(doc.get("net_ex_price")),
            "detail": "正数为估算净增持，负数为净减持（亿元）",
        },
        {
            "id": "new_vs_incumbent",
            "question": f"新增{scope}筹码是原来的老基金买的，还是新基金买的？",
            "value_yi": yuan_to_yi(doc.get("new_inflow")),
            "detail": (
                f"新进 {yuan_to_yi(doc.get('new_inflow'))} 亿，"
                f"存量变化 {yuan_to_yi(doc.get('incumbent_delta'))} 亿"
            ),
        },
        {
            "id": "incumbent_selling",
            "question": f"新基金进入{scope}的同时，原有基金的{scope}持仓是否下降？",
            "value_yi": yuan_to_yi(doc.get("incumbent_delta")),
            "detail": "存量变化为负即老机构净卖出（含退出）",
        },
    ]
