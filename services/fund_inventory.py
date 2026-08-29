"""主动公募库存快照：读 Mongo、换算亿元、回答四个持仓问题。"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

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

CANDIDATE_CATEGORY_LABELS = {
    "underpriced": "尚未充分定价",
    "crowded_trend": "强势但可能拥挤",
    "earnings_inflection": "盈利拐点新库存",
    "high_level_turnover": "高位库存换手",
    "negative_combo": "明显负面组合",
    "other": "其他 / 待观察",
    "insufficient_data": "数据不足",
}

CANDIDATE_CATEGORY_HINTS = {
    "underpriced": "基金强化、盈利改善且年内涨幅温和，可能尚未充分定价。",
    "crowded_trend": "基金强化但涨幅和估值均处高位，趋势较强且拥挤风险上升。",
    "earnings_inflection": "新建库存与盈利拐点同时出现，值得继续跟踪。",
    "high_level_turnover": "大幅上涨后发生库存换手，可能是原有资金兑现、新资金接盘。",
    "negative_combo": "正式全持仓退出与盈利恶化同时出现，基本面和持仓共振转弱。",
    "other": "暂未同时满足五类组合条件。",
    "insufficient_data": "缺少同口径盈利、估值或年内涨幅数据，暂不强行分类。",
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


def _optional_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _profit_yoy(row: Mapping[str, Any]) -> Optional[float]:
    """Prefer comparable single-quarter YoY, then fall back to cumulative YoY."""
    quarterly = _optional_float(row.get("q_netprofit_yoy"))
    if quarterly is not None:
        return quarterly
    return _optional_float(row.get("netprofit_yoy"))


def classify_inventory_candidate(
    doc: Mapping[str, Any],
    context: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Classify one inventory row using deterministic, auditable thresholds."""
    context = context or {}
    score = context.get("score") if isinstance(context.get("score"), Mapping) else {}
    financials_raw = context.get("financials")
    financials: Sequence[Mapping[str, Any]] = (
        financials_raw
        if isinstance(financials_raw, (list, tuple))
        else ()
    )
    latest_financial = financials[0] if financials else {}
    previous_financial = financials[1] if len(financials) > 1 else {}

    growth_score = _optional_float(score.get("growth_score"))
    fundamental_score = _optional_float(score.get("fundamental_score"))
    value_score = _optional_float(score.get("value_score"))
    latest_profit_yoy = _profit_yoy(latest_financial)
    previous_profit_yoy = _profit_yoy(previous_financial)
    profit_yoy_change = (
        latest_profit_yoy - previous_profit_yoy
        if latest_profit_yoy is not None and previous_profit_yoy is not None
        else None
    )
    ytd_return_pct = _optional_float(context.get("ytd_return_pct"))
    ytd_adjustment_degraded = bool(context.get("ytd_adjustment_degraded"))

    state = str(doc.get("state") or "")
    quality = doc.get("quality") if isinstance(doc.get("quality"), Mapping) else {}
    quality_status = str(
        doc.get("quality_status") or quality.get("status") or "official"
    )
    provisional = quality_status == "provisional"
    market = str(
        doc.get("market")
        or ("HK" if str(doc.get("ts_code") or "").endswith(".HK") else "A")
    )

    profit_improving = (
        latest_profit_yoy is not None
        and latest_profit_yoy > 0
        and growth_score is not None
        and growth_score >= 60
        and fundamental_score is not None
        and fundamental_score >= 50
    )
    profit_inflection = (
        latest_profit_yoy is not None
        and latest_profit_yoy > 0
        and previous_profit_yoy is not None
        and (
            previous_profit_yoy <= 0
            or (profit_yoy_change is not None and profit_yoy_change >= 20)
        )
    )
    profit_deteriorating = (
        latest_profit_yoy is not None
        and latest_profit_yoy < 0
        and (
            (profit_yoy_change is not None and profit_yoy_change < 0)
            or (growth_score is not None and growth_score <= 40)
        )
    )
    mild_rise = (
        ytd_return_pct is not None
        and not ytd_adjustment_degraded
        and -10 <= ytd_return_pct <= 30
    )
    large_rise = (
        ytd_return_pct is not None
        and not ytd_adjustment_degraded
        and ytd_return_pct >= 50
    )
    high_valuation = value_score is not None and value_score <= 35

    category = "other"
    caveat = ""
    if state == "incumbent_strengthen" and profit_improving and mild_rise:
        category = "underpriced"
    elif state == "incumbent_strengthen" and large_rise and high_valuation:
        category = "crowded_trend"
    elif state == "new_inventory" and profit_inflection:
        category = "earnings_inflection"
    elif state == "inventory_turnover" and large_rise:
        category = "high_level_turnover"
        if provisional:
            caveat = "当前仅表示已披露基金前十大换手，结论弱于正式全持仓。"
    elif (
        state == "inventory_exit"
        and not provisional
        and profit_deteriorating
    ):
        category = "negative_combo"
    elif state == "inventory_exit" and provisional:
        caveat = "当前仅代表退出已披露基金前十大，不按完整持仓退出判负面。"

    has_context = any(
        value is not None
        for value in (
            growth_score,
            fundamental_score,
            value_score,
            latest_profit_yoy,
            ytd_return_pct,
        )
    )
    if category == "other" and not has_context:
        category = "insufficient_data"
        if market == "HK":
            hk_caveat = "港股暂缺与 A 股同口径的盈利和估值评分。"
            caveat = f"{caveat} {hk_caveat}".strip()

    state_labels = PREVIEW_STATE_LABELS if provisional else STATE_LABELS
    evidence = [state_labels.get(state) or state or "库存状态未知"]
    if latest_profit_yoy is not None:
        evidence.append(f"单季净利同比 {latest_profit_yoy:+.1f}%")
    if profit_yoy_change is not None:
        evidence.append(f"较上期变化 {profit_yoy_change:+.1f}pp")
    if growth_score is not None:
        evidence.append(f"成长 {growth_score:.1f}")
    if fundamental_score is not None:
        evidence.append(f"基本面 {fundamental_score:.1f}")
    if ytd_return_pct is not None:
        suffix = "（复权数据不足）" if ytd_adjustment_degraded else ""
        evidence.append(f"年内涨幅 {ytd_return_pct:+.1f}%{suffix}")
    if value_score is not None:
        evidence.append(f"价值 {value_score:.1f}")

    return {
        "category": category,
        "label": CANDIDATE_CATEGORY_LABELS[category],
        "hint": CANDIDATE_CATEGORY_HINTS[category],
        "evidence": evidence,
        "caveat": caveat,
        "rule_version": "fund_inventory_candidate_v1",
        "metrics": {
            "score_date": score.get("score_date"),
            "financial_period": latest_financial.get("end_date"),
            "growth_score": growth_score,
            "fundamental_score": fundamental_score,
            "value_score": value_score,
            "latest_profit_yoy": latest_profit_yoy,
            "previous_profit_yoy": previous_profit_yoy,
            "profit_yoy_change_pp": profit_yoy_change,
            "ytd_return_pct": ytd_return_pct,
            "ytd_price_date": context.get("ytd_price_date"),
            "ytd_adjustment_degraded": ytd_adjustment_degraded,
        },
    }


def public_snapshot(
    doc: Optional[Dict[str, Any]],
    candidate_context: Optional[Mapping[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
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
        "candidate_classification": classify_inventory_candidate(
            doc,
            candidate_context,
        ),
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
