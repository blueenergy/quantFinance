from __future__ import annotations

from statistics import median
from typing import Any, Dict, List, Optional


VALUATION_SOURCE = "quant_data.financial_daily_basic + financial_* + finance.stock_scores"

MARKET_MULTIPLE_FIELDS = ("pe", "pe_ttm", "pb", "ps", "ps_ttm", "dv_ttm")

DEFAULT_VALUATION_CONFIG = {
    "forecast_years": 5,
    "base_wacc": 0.09,
    "terminal_growth": 0.025,
    "growth_floor": -0.02,
    "growth_cap": 0.15,
}


def _to_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _collection(container: Any, name: str) -> Any:
    if isinstance(container, dict):
        return container.get(name)
    try:
        return container[name]
    except Exception:
        return None


def _recent_docs(
    collection: Any,
    candidates: List[str],
    *,
    date_field: str,
    limit: int,
    projection: Optional[Dict[str, int]] = None,
    fields: tuple[str, ...] = ("ts_code", "symbol"),
) -> List[Dict[str, Any]]:
    if collection is None:
        return []
    for field in fields:
        cursor = collection.find(
            {field: {"$in": candidates}},
            projection,
            sort=[(date_field, -1)],
            limit=limit,
        )
        docs = list(cursor)
        if docs:
            return docs
    return []


def _latest_score(finance_db: Any, candidates: List[str]) -> Optional[Dict[str, Any]]:
    collection = _collection(finance_db, "stock_scores")
    if collection is None:
        return None
    return collection.find_one(
        {"symbol": {"$in": candidates}},
        {"_id": 0, "symbol": 1, "score_date": 1, "value_score": 1},
        sort=[("score_date", -1), ("_id", -1)],
    )


def _latest_stock_info(data_db: Any, candidates: List[str]) -> Dict[str, Any]:
    collection = _collection(data_db, "stock_info")
    if collection is None:
        return {}
    for field in ("symbol", "ts_code"):
        doc = collection.find_one({field: {"$in": candidates}}, {"_id": 0})
        if doc:
            return doc
    return {}


def _percentile_rank(history: List[Dict[str, Any]], field: str, current: Optional[float]) -> Optional[float]:
    if current is None:
        return None
    values = [_to_float(row.get(field)) for row in history]
    values = [value for value in values if value is not None and value > 0]
    if len(values) < 30:
        return None
    return round(sum(1 for value in values if value <= current) / len(values) * 100, 2)


def _market_multiples(daily_basic: List[Dict[str, Any]]) -> Dict[str, Any]:
    latest = daily_basic[0] if daily_basic else {}
    current = {field: _to_float(latest.get(field)) for field in MARKET_MULTIPLE_FIELDS}
    current.update(
        {
            "trade_date": latest.get("trade_date"),
            "total_mv": _to_float(latest.get("total_mv")),
            "circ_mv": _to_float(latest.get("circ_mv")),
            "close": _to_float(latest.get("close")),
        }
    )
    percentiles = {
        field: _percentile_rank(daily_basic, field, current.get(field))
        for field in ("pe_ttm", "pb", "ps_ttm", "dv_ttm")
    }
    return {
        "current": current,
        "history_sample_count": len(daily_basic),
        "percentiles": percentiles,
        "percentile_window": "latest 1250 trading days",
    }


def _growth_assumption(indicators: List[Dict[str, Any]], config: Dict[str, Any]) -> Dict[str, Any]:
    latest = indicators[0] if indicators else {}
    raw_growth = _to_float(latest.get("netprofit_yoy")) or _to_float(latest.get("tr_yoy"))
    if raw_growth is None:
        normalized = 0.05
        source = "default"
    else:
        normalized = raw_growth / 100.0
        source = "financial_indicator.netprofit_yoy/tr_yoy"
    growth = _clamp(normalized, config["growth_floor"], config["growth_cap"])
    return {
        "raw": raw_growth,
        "normalized": round(growth, 4),
        "source": source,
    }


def _period_year(period: Any) -> Optional[int]:
    text = str(period or "")
    if len(text) < 8:
        return None
    try:
        return int(text[:4])
    except ValueError:
        return None


def _period_suffix(period: Any) -> str:
    return str(period or "")[4:8]


def _ttm_from_cumulative(rows: List[Dict[str, Any]], field: str) -> Optional[Dict[str, Any]]:
    """Build a TTM value from cumulative report rows when possible.

    Tushare financial indicators are cumulative within the fiscal year for
    quarterly rows. Mixing Q1/H1/Q3/year values directly understates annual
    cash flow, so DCF should prefer TTM or full-year values.
    """
    by_period = {str(row.get("end_date") or ""): row for row in rows if row.get("end_date")}
    for row in rows:
        period = str(row.get("end_date") or "")
        value = _to_float(row.get(field))
        if value is None or value <= 0:
            continue
        suffix = _period_suffix(period)
        year = _period_year(period)
        if suffix == "1231":
            return {
                "value": value,
                "field": f"financial_indicator.{field}",
                "sample_count": 1,
                "method": "latest_full_year",
                "period": period,
            }
        if year is None or suffix not in {"0331", "0630", "0930"}:
            continue
        prev_year_row = by_period.get(f"{year - 1}1231")
        prev_same_row = by_period.get(f"{year - 1}{suffix}")
        prev_year_value = _to_float((prev_year_row or {}).get(field))
        prev_same_value = _to_float((prev_same_row or {}).get(field))
        if prev_year_value is None or prev_same_value is None:
            continue
        ttm = value + prev_year_value - prev_same_value
        if ttm > 0:
            return {
                "value": ttm,
                "field": f"financial_indicator.{field}",
                "sample_count": 3,
                "method": "ttm_from_cumulative_indicator",
                "period": period,
                "formula": f"{period} + {year - 1}1231 - {year - 1}{suffix}",
            }
    return None


def _fcff_input(indicators: List[Dict[str, Any]], cashflow: List[Dict[str, Any]]) -> Dict[str, Any]:
    fcff_ttm = _ttm_from_cumulative(indicators, "fcff")
    if fcff_ttm:
        return fcff_ttm

    fallback_values = []
    for row in cashflow[:4]:
        operating = _to_float(row.get("n_cashflow_act"))
        capex = _to_float(row.get("c_pay_acq_const_fiolta"))
        if operating is not None and capex is not None:
            fallback = operating - abs(capex)
            if fallback > 0:
                fallback_values.append(fallback)
    if fallback_values:
        return {
            "value": median(fallback_values),
            "field": "financial_cashflow.n_cashflow_act - c_pay_acq_const_fiolta",
            "sample_count": len(fallback_values),
            "method": "median_latest_positive_operating_cashflow_less_capex",
        }

    return {
        "value": None,
        "field": None,
        "sample_count": 0,
        "method": "insufficient_data",
    }


def _dcf_scenarios(
    indicators: List[Dict[str, Any]],
    cashflow: List[Dict[str, Any]],
    market: Dict[str, Any],
    config: Dict[str, Any],
) -> Dict[str, Any]:
    fcff = _fcff_input(indicators, cashflow)
    current = market.get("current") or {}
    total_mv_wan = _to_float(current.get("total_mv"))
    close = _to_float(current.get("close"))
    shares_wan = total_mv_wan / close if total_mv_wan and close and close > 0 else None
    if fcff["value"] is None:
        return {
            "status": "insufficient_data",
            "reason": "缺少正向 FCFF 或经营现金流-资本开支样本",
            "input": fcff,
            "scenarios": [],
        }

    growth = _growth_assumption(indicators, config)
    base_growth = growth["normalized"]
    scenario_defs = [
        ("conservative", config["base_wacc"] + 0.015, max(0.0, config["terminal_growth"] - 0.005), base_growth - 0.03),
        ("base", config["base_wacc"], config["terminal_growth"], base_growth),
        ("optimistic", max(0.01, config["base_wacc"] - 0.01), config["terminal_growth"] + 0.005, base_growth + 0.03),
    ]
    scenarios = []
    for name, wacc, terminal_growth, initial_growth in scenario_defs:
        projected_fcf = fcff["value"]
        present_value = 0.0
        years = int(config["forecast_years"])
        growth_rate = _clamp(initial_growth, config["growth_floor"], config["growth_cap"])
        for year in range(1, years + 1):
            decay = year / years
            year_growth = growth_rate * (1 - decay) + terminal_growth * decay
            projected_fcf *= 1 + year_growth
            present_value += projected_fcf / ((1 + wacc) ** year)
        if wacc <= terminal_growth:
            terminal_value = None
            equity_value = None
        else:
            terminal_value = projected_fcf * (1 + terminal_growth) / (wacc - terminal_growth)
            equity_value = present_value + terminal_value / ((1 + wacc) ** years)
        equity_value_wan = equity_value / 10000 if equity_value is not None else None
        fair_value_per_share = equity_value_wan / shares_wan if equity_value_wan is not None and shares_wan else None
        safety_margin = (
            round((fair_value_per_share - close) / fair_value_per_share * 100, 2)
            if fair_value_per_share and close
            else None
        )
        scenarios.append(
            {
                "name": name,
                "wacc": round(wacc, 4),
                "terminal_growth": round(terminal_growth, 4),
                "initial_growth": round(growth_rate, 4),
                "equity_value_wan": round(equity_value_wan, 2) if equity_value_wan is not None else None,
                "fair_value_per_share": round(fair_value_per_share, 2) if fair_value_per_share is not None else None,
                "safety_margin_pct": safety_margin,
            }
        )
    return {
        "status": "ok",
        "method": "FCFF discounted by WACC; no net debt adjustment in v1",
        "input": fcff,
        "growth_assumption": growth,
        "config": config,
        "scenarios": scenarios,
    }


def _relative_valuation(market: Dict[str, Any], indicators: List[Dict[str, Any]]) -> Dict[str, Any]:
    current = market.get("current") or {}
    percentiles = market.get("percentiles") or {}
    latest = indicators[0] if indicators else {}
    growth_pct = _to_float(latest.get("netprofit_yoy")) or _to_float(latest.get("tr_yoy"))
    pe_ttm = _to_float(current.get("pe_ttm"))
    peg = pe_ttm / growth_pct if pe_ttm and growth_pct and growth_pct > 0 else None
    usable = {
        key: value for key, value in percentiles.items()
        if value is not None and key in {"pe_ttm", "pb", "ps_ttm"}
    }
    if not usable:
        verdict = "insufficient_data"
    else:
        avg_percentile = sum(usable.values()) / len(usable)
        if avg_percentile <= 35:
            verdict = "below_own_history"
        elif avg_percentile >= 70:
            verdict = "above_own_history"
        else:
            verdict = "near_own_history_midrange"
    return {
        "status": "ok" if usable else "insufficient_data",
        "verdict": verdict,
        "method": "current multiples vs own 3-5y history percentile",
        "peg": round(peg, 2) if peg is not None else None,
        "percentiles": percentiles,
    }


def _model_suitability(
    stock: Dict[str, Any],
    indicators: List[Dict[str, Any]],
    dcf: Dict[str, Any],
    market: Dict[str, Any],
) -> Dict[str, Any]:
    industry_text = " ".join(
        str(stock.get(key) or "")
        for key in ("industry", "industry_l1", "industry_l2", "industry_l3")
    )
    latest = indicators[0] if indicators else {}
    roe = _to_float(latest.get("roe"))
    fcff_positive_count = 0
    for row in indicators[:8]:
        value = _to_float(row.get("fcff"))
        if value is not None and value > 0:
            fcff_positive_count += 1
    current = market.get("current") or {}
    dividend = _to_float(current.get("dv_ttm"))
    total_mv_wan = _to_float(current.get("total_mv"))
    base_scenario = next(
        (row for row in dcf.get("scenarios", []) if row.get("name") == "base"),
        {},
    )
    base_equity_value_wan = _to_float(base_scenario.get("equity_value_wan"))
    reasons = []
    recommended = "relative_valuation"
    confidence = 0.45
    if any(token in industry_text for token in ("银行", "保险", "证券")):
        recommended = "pb_roe_or_ddm"
        confidence = 0.65
        reasons.append("金融行业更适合 PB-ROE 或 DDM，普通 FCFF DCF 仅作参考")
    elif dcf.get("status") == "ok" and fcff_positive_count >= 4:
        recommended = "dcf"
        confidence = 0.7
        reasons.append("近 8 期 FCFF 中至少 4 期为正，现金流可用于 DCF")
    else:
        reasons.append("现金流样本不足，优先参考历史分位相对估值")
    if roe is not None:
        reasons.append(f"最新 ROE 为 {roe:.2f}")
    if dividend is not None and dividend > 0:
        reasons.append(f"dv_ttm 为 {dividend:.2f}，后续可扩展 DDM")
    if (
        dividend is not None and dividend >= 4
        and roe is not None and roe >= 20
        and total_mv_wan is not None and total_mv_wan > 0
        and base_equity_value_wan is not None and base_equity_value_wan > total_mv_wan * 1.3
    ):
        reasons.append("高股息率与高 ROE 推高 DCF 估值，建议优先用 DDM/分红持续性进行交叉验证")
    return {
        "recommended_model": recommended,
        "confidence": round(confidence, 2),
        "reasons": reasons,
        "signals": {
            "roe": roe,
            "fcff_positive_count_8": fcff_positive_count,
            "dv_ttm": dividend,
        },
    }


def build_stock_valuation(
    symbol: str,
    *,
    data_db: Any,
    finance_db: Any,
    candidates: List[str],
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    valuation_config = {**DEFAULT_VALUATION_CONFIG, **(config or {})}
    daily_basic = _recent_docs(
        _collection(data_db, "financial_daily_basic"),
        candidates,
        date_field="trade_date",
        limit=1250,
        projection={
            "_id": 0,
            "symbol": 1,
            "ts_code": 1,
            "trade_date": 1,
            "close": 1,
            "pe": 1,
            "pe_ttm": 1,
            "pb": 1,
            "ps": 1,
            "ps_ttm": 1,
            "dv_ttm": 1,
            "total_mv": 1,
            "circ_mv": 1,
        },
    )
    indicators = _recent_docs(
        _collection(data_db, "financial_indicator"),
        candidates,
        date_field="end_date",
        limit=40,
        projection={
            "_id": 0,
            "ts_code": 1,
            "symbol": 1,
            "end_date": 1,
            "roe": 1,
            "fcff": 1,
            "fcfe": 1,
            "netprofit_yoy": 1,
            "tr_yoy": 1,
            "debt_to_assets": 1,
        },
    )
    cashflow = _recent_docs(
        _collection(data_db, "financial_cashflow"),
        candidates,
        date_field="end_date",
        limit=12,
        projection={
            "_id": 0,
            "ts_code": 1,
            "symbol": 1,
            "end_date": 1,
            "n_cashflow_act": 1,
            "c_pay_acq_const_fiolta": 1,
        },
    )
    market = _market_multiples(daily_basic)
    dcf = _dcf_scenarios(indicators, cashflow, market, valuation_config)
    relative = _relative_valuation(market, indicators)
    stock = _latest_stock_info(data_db, candidates)
    score = _latest_score(finance_db, candidates)
    suitability = _model_suitability(stock, indicators, dcf, market)
    as_of = (
        (daily_basic[0] if daily_basic else {}).get("trade_date")
        or (indicators[0] if indicators else {}).get("end_date")
    )
    found = bool(daily_basic or indicators or cashflow or score)
    return {
        "symbol": symbol,
        "market_multiples": market,
        "dcf": dcf,
        "relative_valuation": relative,
        "model_suitability": suitability,
        "value_score_link": score,
        "inputs": {
            "daily_basic_count": len(daily_basic),
            "indicator_count": len(indicators),
            "cashflow_count": len(cashflow),
            "config": valuation_config,
        },
        "data_status": {
            "found": found,
            "as_of": as_of,
            "stale": False,
            "expected_freshness": "daily_basic 日更；财务指标随财报披露更新",
            "source": VALUATION_SOURCE,
        },
    }
