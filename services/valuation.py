from __future__ import annotations

from datetime import date, datetime
from statistics import median
from typing import Any, Dict, List, Optional, Tuple

from stock_data_access.score_query import merge_score_scope

VALUATION_SOURCE = "quant_data.financial_daily_basic + financial_balance + financial_* + financial_dividend + finance.stock_scores"

MARKET_MULTIPLE_FIELDS = ("pe", "pe_ttm", "pb", "ps", "ps_ttm", "dv_ttm")

INDUSTRY_KEYWORDS: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "financial": {
        "keywords": ("银行", "保险", "证券", "信托", "金融"),
        "whitelist": (),
        "blacklist": (),
    },
    "tech": {
        "keywords": (
            "半导体",
            "芯片",
            "集成电路",
            "软件",
            "计算机",
            "电子",
            "互联网",
            "通信",
            "人工智能",
            "云计算",
            "信息技术",
        ),
        "whitelist": (),
        "blacklist": (),
    },
    "pharma": {
        "keywords": ("医药", "生物", "制药", "医疗器械", "创新药"),
        "whitelist": (),
        "blacklist": (),
    },
}

DEFAULT_VALUATION_CONFIG = {
    "forecast_years": 5,
    "base_wacc": 0.09,
    "terminal_growth": 0.025,
    "growth_floor": -0.02,
    "growth_cap": 0.15,
    "ddm_ke": 0.085,
    "ddm_growth": 0.015,
    "risk_free": 0.025,
    "equity_risk_premium": 0.055,
    "ddm_growth_cap": 0.03,
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


def _latest_score(finance_db: Any, candidates: List[str], index_code=None) -> Optional[Dict[str, Any]]:
    collection = _collection(finance_db, "stock_scores")
    if collection is None:
        return None
    return collection.find_one(
        merge_score_scope({"symbol": {"$in": candidates}}, index_code),
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


def _financial_daily_mv_field(row: Dict[str, Any]) -> Tuple[Optional[float], str]:
    """Return (万元市值, field_name) from one `financial_daily_basic` row."""
    mv = _to_float(row.get("total_mv"))
    if mv is not None and mv > 0:
        return mv, "total_mv"
    circ = _to_float(row.get("circ_mv"))
    if circ is not None and circ > 0:
        return circ, "circ_mv"
    return None, ""


def _volume_price_close_rows(data_db: Any, candidates: List[str], *, limit: int) -> List[Dict[str, Any]]:
    coll = _collection(data_db, "volume_price")
    if coll is None or not candidates:
        return []
    for field in ("symbol", "ts_code"):
        query = {field: {"$in": candidates}}
        try:
            cur = coll.find(
                query,
                {"_id": 0, "trade_date": 1, "close": 1},
                sort=[("trade_date", -1), ("_id", -1)],
                limit=limit,
            )
            rows = list(cur)
        except Exception:
            continue
        if rows:
            return rows
    return []


def _volume_price_close_index(
    data_db: Any, candidates: List[str], *, limit: int = 220
) -> Tuple[Dict[str, float], Optional[Dict[str, Any]]]:
    """Map trade_date -> close from `volume_price`, plus latest bar (newest trade_date)."""
    rows = _volume_price_close_rows(data_db, candidates, limit=limit)
    by_td: Dict[str, float] = {}
    latest: Optional[Dict[str, Any]] = None
    for doc in rows:
        if latest is None:
            latest = doc
        td = str(doc.get("trade_date") or "")
        if not td or td in by_td:
            continue
        c = _to_float(doc.get("close"))
        if c is not None and c > 0:
            by_td[td] = c
    return by_td, latest


def _stock_info_mv_wan(stock: Dict[str, Any]) -> Tuple[Optional[float], str]:
    """万元市值：bak_basic 同步到 stock_info 的 total_mv / circ_mv 及兼容字段。"""
    for key, label in (
        ("total_mv", "total_mv"),
        ("market_cap", "market_cap"),
        ("circ_mv", "circ_mv"),
        ("circ_market_cap", "circ_market_cap"),
    ):
        mv = _to_float(stock.get(key))
        if mv is not None and mv > 0:
            return mv, label
    return None, ""


def _stock_info_shares_wan(stock: Dict[str, Any]) -> Tuple[Optional[float], str]:
    """Tushare `bak_basic` / stock list: total_share & float_share are 万股."""
    ts = _to_float(stock.get("total_share"))
    if ts is not None and ts > 0:
        return ts, "stock_info.total_share"
    fs = _to_float(stock.get("float_share"))
    if fs is not None and fs > 0:
        return fs, "stock_info.float_share"
    return None, ""


def _dcf_reference_close(
    daily_basic: List[Dict[str, Any]],
    market_current: Dict[str, Any],
    data_db: Any,
    candidates: List[str],
) -> Tuple[Optional[float], str]:
    """Best-effort 元/股收盘价（用于安全边际；股本来自 stock_info 时仍需要）。"""
    for row in daily_basic[:120]:
        c = _to_float(row.get("close"))
        if c is not None and c > 0:
            return c, "financial_daily_basic.close"
    _, vp_latest = _volume_price_close_index(data_db, candidates, limit=220)
    if vp_latest:
        c = _to_float(vp_latest.get("close"))
        if c is not None and c > 0:
            return c, "volume_price.latest_close"
    mc = market_current or {}
    c = _to_float(mc.get("close"))
    if c is not None and c > 0:
        return c, "market_multiples_current.close"
    return None, "none"


def _dcf_shares_market_inputs(
    daily_basic: List[Dict[str, Any]],
    market_current: Dict[str, Any],
    data_db: Any,
    candidates: List[str],
) -> Tuple[Optional[float], Optional[float], str]:
    """Resolve 市值(万) and 收盘价 for implied 万股.

    Primary source is `financial_daily_basic` (newest-first). Some deployments
    omit `close` on that collection while `volume_price` has the session close
    for the same `trade_date`; we fill from there. If the headline daily_basic
    date is newer than the latest K-line bar, we skip cross-session pairing to
    avoid mixing stale prices with fresh dates.
    """
    vp_by_td, vp_latest = _volume_price_close_index(data_db, candidates)
    db0_td = str((daily_basic[0] if daily_basic else {}).get("trade_date") or "")
    vp_td = str((vp_latest or {}).get("trade_date") or "")
    if vp_latest and db0_td and vp_td and db0_td > vp_td:
        vp_latest = None

    for row in daily_basic[:120]:
        mv, part = _financial_daily_mv_field(row)
        if mv is None:
            continue
        td = str(row.get("trade_date") or "")
        row_close = _to_float(row.get("close"))
        close = row_close if row_close is not None and row_close > 0 else None
        if close is None and td:
            vc = _to_float(vp_by_td.get(td))
            if vc is not None and vc > 0:
                close = vc
        if close is None or close <= 0:
            continue
        basis = f"financial_daily_basic.{part}"
        if row_close is None or row_close <= 0:
            basis += "+volume_price.close"
        return mv, close, basis

    if vp_latest:
        vp_close = _to_float(vp_latest.get("close"))
        vp_td2 = str(vp_latest.get("trade_date") or "")
        if vp_close and vp_close > 0 and vp_td2:
            for row in daily_basic[:120]:
                row_td = str(row.get("trade_date") or "")
                if row_td > vp_td2:
                    continue
                mv, part = _financial_daily_mv_field(row)
                if mv is None:
                    continue
                return mv, vp_close, f"financial_daily_basic.{part}+volume_price.latest_session_close"

    mc = market_current or {}
    mv = _to_float(mc.get("total_mv"))
    circ = _to_float(mc.get("circ_mv"))
    close = _to_float(mc.get("close"))
    if close and close > 0:
        if mv and mv > 0:
            return mv, close, "market_multiples_current.total_mv"
        if circ and circ > 0:
            return circ, close, "market_multiples_current.circ_mv"
    return None, None, "none"


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


def _stock_code_values(stock: Dict[str, Any]) -> set[str]:
    return {
        str(stock.get(key) or "").upper()
        for key in ("symbol", "ts_code")
        if stock.get(key)
    }


def _industry_tag(stock: Dict[str, Any]) -> set[str]:
    industry_text = " ".join(
        str(stock.get(key) or "")
        for key in ("industry", "industry_l1", "industry_l2", "industry_l3")
    )
    codes = _stock_code_values(stock)
    tags: set[str] = set()
    for tag, rule in INDUSTRY_KEYWORDS.items():
        blacklist = {str(code).upper() for code in rule.get("blacklist", ())}
        whitelist = {str(code).upper() for code in rule.get("whitelist", ())}
        if codes & blacklist:
            continue
        if codes & whitelist:
            tags.add(tag)
            continue
        if any(token in industry_text for token in rule.get("keywords", ())):
            tags.add(tag)
    return tags


def _financial_sector_stock(stock: Dict[str, Any]) -> bool:
    return "financial" in _industry_tag(stock)


def _net_debt_and_minority(balance: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Balance sheet amounts from Tushare are in yuan; convert to 万元 (wan)."""
    row = balance[0] if balance else None
    if not row:
        return {
            "status": "missing",
            "end_date": None,
            "interest_bearing_debt_wan": None,
            "cash_wan": None,
            "net_debt_wan": None,
            "minority_interest_wan": None,
        }
    money_cap = _to_float(row.get("money_cap")) or 0.0
    st_borr = _to_float(row.get("st_borr")) or 0.0
    lt_borr = _to_float(row.get("lt_borr")) or 0.0
    bond_payable = _to_float(row.get("bond_payable")) or 0.0
    non_cur_liab_due_1y = _to_float(row.get("non_cur_liab_due_1y")) or 0.0
    minority = _to_float(row.get("minority_int")) or 0.0
    interest_bearing_yuan = st_borr + lt_borr + bond_payable + non_cur_liab_due_1y
    net_debt_yuan = interest_bearing_yuan - money_cap
    return {
        "status": "ok",
        "end_date": row.get("end_date"),
        "interest_bearing_debt_wan": round(interest_bearing_yuan / 10000.0, 2),
        "cash_wan": round(money_cap / 10000.0, 2),
        "net_debt_wan": round(net_debt_yuan / 10000.0, 2),
        "minority_interest_wan": round(minority / 10000.0, 2),
    }


def _dcf_project_values(
    *,
    fcff_value: float,
    wacc: float,
    terminal_growth: float,
    initial_growth: float,
    config: Dict[str, Any],
    skip_net_debt: bool,
    net_debt_wan: float,
    minority_wan: float,
    shares_wan: Optional[float],
    close: Optional[float],
) -> Dict[str, Optional[float]]:
    projected_fcf = fcff_value
    present_value = 0.0
    years = int(config["forecast_years"])
    growth_rate = _clamp(initial_growth, config["growth_floor"], config["growth_cap"])
    for year in range(1, years + 1):
        decay = year / years
        year_growth = growth_rate * (1 - decay) + terminal_growth * decay
        projected_fcf *= 1 + year_growth
        present_value += projected_fcf / ((1 + wacc) ** year)
    if wacc <= terminal_growth:
        enterprise_value = None
    else:
        terminal_value = projected_fcf * (1 + terminal_growth) / (wacc - terminal_growth)
        enterprise_value = present_value + terminal_value / ((1 + wacc) ** years)
    enterprise_value_wan = enterprise_value / 10000 if enterprise_value is not None else None
    if enterprise_value_wan is None:
        equity_value_wan = None
    elif skip_net_debt:
        equity_value_wan = enterprise_value_wan
    else:
        equity_value_wan = enterprise_value_wan - float(net_debt_wan) - float(minority_wan)
    fair_value_per_share = equity_value_wan / shares_wan if equity_value_wan is not None and shares_wan else None
    safety_margin = (
        (fair_value_per_share - close) / fair_value_per_share * 100
        if fair_value_per_share and close
        else None
    )
    return {
        "enterprise_value_wan": enterprise_value_wan,
        "equity_value_wan": equity_value_wan,
        "fair_value_per_share": fair_value_per_share,
        "safety_margin_pct": safety_margin,
    }


def _dcf_sensitivity(
    *,
    fcff_value: float,
    base_wacc: float,
    base_terminal_growth: float,
    base_initial_growth: float,
    config: Dict[str, Any],
    skip_net_debt: bool,
    net_debt_wan: float,
    minority_wan: float,
    shares_wan: Optional[float],
    close: Optional[float],
) -> Dict[str, Any]:
    def case(
        *,
        label: str,
        fcff_multiplier: float,
        wacc: float,
        terminal_growth: float,
        initial_growth: float,
    ) -> Dict[str, Any]:
        values = _dcf_project_values(
            fcff_value=fcff_value * fcff_multiplier,
            wacc=wacc,
            terminal_growth=terminal_growth,
            initial_growth=initial_growth,
            config=config,
            skip_net_debt=skip_net_debt,
            net_debt_wan=net_debt_wan,
            minority_wan=minority_wan,
            shares_wan=shares_wan,
            close=close,
        )
        return {
            "label": label,
            "fcff_multiplier": round(fcff_multiplier, 4),
            "wacc": round(wacc, 4),
            "terminal_growth": round(terminal_growth, 4),
            "initial_growth": round(_clamp(initial_growth, config["growth_floor"], config["growth_cap"]), 4),
            "fair_value_per_share": (
                round(values["fair_value_per_share"], 2)
                if values["fair_value_per_share"] is not None
                else None
            ),
            "equity_value_wan": (
                round(values["equity_value_wan"], 2)
                if values["equity_value_wan"] is not None
                else None
            ),
        }

    return {
        "status": "ok",
        "method": "base scenario single-variable sensitivity",
        "base": {
            "wacc": round(base_wacc, 4),
            "terminal_growth": round(base_terminal_growth, 4),
            "initial_growth": round(_clamp(base_initial_growth, config["growth_floor"], config["growth_cap"]), 4),
            "fcff_multiplier": 1.0,
        },
        "variables": [
            {
                "key": "wacc",
                "label": "WACC",
                "cases": [
                    case(label="-1pct", fcff_multiplier=1.0, wacc=max(0.01, base_wacc - 0.01), terminal_growth=base_terminal_growth, initial_growth=base_initial_growth),
                    case(label="base", fcff_multiplier=1.0, wacc=base_wacc, terminal_growth=base_terminal_growth, initial_growth=base_initial_growth),
                    case(label="+1pct", fcff_multiplier=1.0, wacc=base_wacc + 0.01, terminal_growth=base_terminal_growth, initial_growth=base_initial_growth),
                ],
            },
            {
                "key": "terminal_growth",
                "label": "永续增长",
                "cases": [
                    case(label="-0.5pct", fcff_multiplier=1.0, wacc=base_wacc, terminal_growth=max(0.0, base_terminal_growth - 0.005), initial_growth=base_initial_growth),
                    case(label="base", fcff_multiplier=1.0, wacc=base_wacc, terminal_growth=base_terminal_growth, initial_growth=base_initial_growth),
                    case(label="+0.5pct", fcff_multiplier=1.0, wacc=base_wacc, terminal_growth=base_terminal_growth + 0.005, initial_growth=base_initial_growth),
                ],
            },
            {
                "key": "fcff_base",
                "label": "FCFF 基数",
                "cases": [
                    case(label="-20%", fcff_multiplier=0.8, wacc=base_wacc, terminal_growth=base_terminal_growth, initial_growth=base_initial_growth),
                    case(label="-10%", fcff_multiplier=0.9, wacc=base_wacc, terminal_growth=base_terminal_growth, initial_growth=base_initial_growth),
                    case(label="base", fcff_multiplier=1.0, wacc=base_wacc, terminal_growth=base_terminal_growth, initial_growth=base_initial_growth),
                    case(label="+10%", fcff_multiplier=1.1, wacc=base_wacc, terminal_growth=base_terminal_growth, initial_growth=base_initial_growth),
                    case(label="+20%", fcff_multiplier=1.2, wacc=base_wacc, terminal_growth=base_terminal_growth, initial_growth=base_initial_growth),
                ],
            },
        ],
    }


def _dcf_scenarios(
    indicators: List[Dict[str, Any]],
    cashflow: List[Dict[str, Any]],
    market: Dict[str, Any],
    config: Dict[str, Any],
    *,
    stock: Dict[str, Any],
    balance: List[Dict[str, Any]],
    daily_basic: List[Dict[str, Any]],
    data_db: Any,
    candidates: List[str],
) -> Dict[str, Any]:
    fcff = _fcff_input(indicators, cashflow)
    current = market.get("current") or {}
    total_mv_wan, close_from_mv, shares_basis = _dcf_shares_market_inputs(
        daily_basic, current, data_db, candidates
    )
    shares_wan = (
        total_mv_wan / close_from_mv
        if total_mv_wan and close_from_mv and close_from_mv > 0
        else None
    )
    close = close_from_mv
    if shares_wan is None:
        si_shares, si_basis = _stock_info_shares_wan(stock)
        if si_shares is not None and si_shares > 0:
            shares_wan = si_shares
            shares_basis = si_basis
            if close is None or close <= 0:
                close, _ = _dcf_reference_close(daily_basic, current, data_db, candidates)
    if shares_wan is None:
        si_mv, mv_key = _stock_info_mv_wan(stock)
        ref_close, ref_cb = _dcf_reference_close(daily_basic, current, data_db, candidates)
        if si_mv and ref_close and ref_close > 0:
            shares_wan = si_mv / ref_close
            shares_basis = f"stock_info.{mv_key}+close({ref_cb})"
            close = ref_close
    if fcff["value"] is None:
        return {
            "status": "insufficient_data",
            "reason": "缺少正向 FCFF 或经营现金流-资本开支样本",
            "input": fcff,
            "scenarios": [],
        }

    nd = _net_debt_and_minority(balance)
    skip_net_debt = _financial_sector_stock(stock)
    net_debt_wan = _to_float(nd.get("net_debt_wan")) if nd.get("status") == "ok" else None
    minority_wan = _to_float(nd.get("minority_interest_wan")) if nd.get("status") == "ok" else None
    if skip_net_debt:
        net_debt_wan = 0.0
        minority_wan = 0.0
    elif net_debt_wan is None or minority_wan is None:
        net_debt_wan = net_debt_wan or 0.0
        minority_wan = minority_wan or 0.0

    growth = _growth_assumption(indicators, config)
    base_growth = growth["normalized"]
    scenario_defs = [
        ("conservative", config["base_wacc"] + 0.015, max(0.0, config["terminal_growth"] - 0.005), base_growth - 0.03),
        ("base", config["base_wacc"], config["terminal_growth"], base_growth),
        ("optimistic", max(0.01, config["base_wacc"] - 0.01), config["terminal_growth"] + 0.005, base_growth + 0.03),
    ]
    scenarios = []
    for name, wacc, terminal_growth, initial_growth in scenario_defs:
        growth_rate = _clamp(initial_growth, config["growth_floor"], config["growth_cap"])
        values = _dcf_project_values(
            fcff_value=fcff["value"],
            wacc=wacc,
            terminal_growth=terminal_growth,
            initial_growth=initial_growth,
            config=config,
            skip_net_debt=skip_net_debt,
            net_debt_wan=float(net_debt_wan),
            minority_wan=float(minority_wan),
            shares_wan=shares_wan,
            close=close,
        )
        scenarios.append(
            {
                "name": name,
                "wacc": round(wacc, 4),
                "terminal_growth": round(terminal_growth, 4),
                "initial_growth": round(growth_rate, 4),
                "enterprise_value_wan": (
                    round(values["enterprise_value_wan"], 2)
                    if values["enterprise_value_wan"] is not None
                    else None
                ),
                "net_debt_wan": round(float(net_debt_wan), 2) if not skip_net_debt else None,
                "minority_interest_wan": round(float(minority_wan), 2) if not skip_net_debt else None,
                "equity_value_wan": (
                    round(values["equity_value_wan"], 2)
                    if values["equity_value_wan"] is not None
                    else None
                ),
                "fair_value_per_share": (
                    round(values["fair_value_per_share"], 2)
                    if values["fair_value_per_share"] is not None
                    else None
                ),
                "safety_margin_pct": (
                    round(values["safety_margin_pct"], 2)
                    if values["safety_margin_pct"] is not None
                    else None
                ),
            }
        )
    sensitivity = _dcf_sensitivity(
        fcff_value=fcff["value"],
        base_wacc=config["base_wacc"],
        base_terminal_growth=config["terminal_growth"],
        base_initial_growth=base_growth,
        config=config,
        skip_net_debt=skip_net_debt,
        net_debt_wan=float(net_debt_wan),
        minority_wan=float(minority_wan),
        shares_wan=shares_wan,
        close=close,
    )
    method = (
        "FCFF discounted by WACC; enterprise value less net debt and minority interest -> equity value"
        if not skip_net_debt
        else "FCFF discounted by WACC; financial sector: EV shown as equity reference (no net debt adjustment)"
    )
    net_debt_block = {
        **nd,
        "applied": not skip_net_debt and nd.get("status") == "ok",
        "skipped_financial_sector": skip_net_debt,
    }
    if skip_net_debt:
        net_debt_block["reason"] = "银行/保险/证券：FCFF DCF 口径与净债务调整不匹配，不作扣减"
    elif nd.get("status") != "ok":
        net_debt_block["reason"] = "缺少资产负债表，净债务与少数股东权益按 0 处理"

    if shares_wan is None:
        per_share = {
            "status": "missing_inputs",
            "reason": (
                "无法推算或读取万股股本：financial_daily_basic 中无有效 total_mv/circ_mv 与收盘价，"
                "且 volume_price 未能补齐；stock_info 中也无 total_share/float_share、"
                "total_mv/market_cap 与可用收盘价组合。"
            ),
            "total_mv_wan": total_mv_wan,
            "close": close,
            "shares_basis": shares_basis,
        }
    else:
        formula_mv = (
            "股权价值(万) ÷ (市值(万)÷股价)。市值来自 financial_daily_basic："
            "优先 total_mv，否则 circ_mv；"
            "收盘价优先同条 close，缺失时按 trade_date 对齐 volume_price，"
            "必要时用最新 K 线与最近可用市值配对。"
        )
        formula_si = (
            "股权价值(万) ÷ 万股股本。万股来自 stock_info：优先 total_share，否则 float_share（Tushare 口径）。"
            "安全边际仍尽量使用 financial_daily_basic / volume_price / 行情快照的收盘价。"
        )
        formula_si_mv = (
            "股权价值(万) ÷ (市值(万)÷股价)。万元市值来自 stock_info（bak_basic 同步的 total_mv/"
            "market_cap 或 circ_mv），收盘价来自日线基本面、K 线或行情快照。"
        )
        if str(shares_basis).startswith("stock_info.") and "+close(" in str(shares_basis):
            formula_pick = formula_si_mv
        elif str(shares_basis).startswith("stock_info."):
            formula_pick = formula_si
        else:
            formula_pick = formula_mv
        per_share = {
            "status": "ok",
            "shares_implied_wan": round(shares_wan, 6),
            "formula": formula_pick,
            "shares_basis": shares_basis,
            "total_mv_wan": total_mv_wan,
            "close": close,
        }

    return {
        "status": "ok",
        "method": method,
        "input": fcff,
        "growth_assumption": growth,
        "config": config,
        "net_debt_adjustment": net_debt_block,
        "per_share": per_share,
        "scenarios": scenarios,
        "sensitivity": sensitivity,
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


def _implemented_dividends(dividends: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = []
    for row in dividends:
        proc = str(row.get("div_proc") or "")
        cash_div = _to_float(row.get("cash_div_tax"))
        if cash_div is None:
            cash_div = _to_float(row.get("cash_div"))
        base_share = _to_float(row.get("base_share"))
        if "实施" not in proc or cash_div is None or cash_div <= 0:
            continue
        total_cash_wan = cash_div * base_share if base_share and base_share > 0 else None
        rows.append(
            {
                "ts_code": row.get("ts_code"),
                "end_date": row.get("end_date"),
                "ann_date": row.get("ann_date"),
                "div_proc": row.get("div_proc"),
                "cash_div": _to_float(row.get("cash_div")),
                "cash_div_tax": _to_float(row.get("cash_div_tax")),
                "record_date": row.get("record_date"),
                "ex_date": row.get("ex_date"),
                "pay_date": row.get("pay_date"),
                "base_share": base_share,
                "total_cash_dividend_wan": total_cash_wan,
            }
        )
    return rows


def _aggregate_dividend_years(dividends: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[str, Dict[str, Any]] = {}
    for row in _implemented_dividends(dividends):
        year = str(row.get("end_date") or "")[:4]
        if not year:
            continue
        item = grouped.setdefault(
            year,
            {
                "year": year,
                "rows": [],
                "cash_div_tax_sum": 0.0,
                "total_cash_dividend_wan": 0.0,
                "has_total_cash": False,
            },
        )
        item["rows"].append(row)
        item["cash_div_tax_sum"] += _to_float(row.get("cash_div_tax")) or 0.0
        total_cash = _to_float(row.get("total_cash_dividend_wan"))
        if total_cash is not None:
            item["total_cash_dividend_wan"] += total_cash
            item["has_total_cash"] = True
    out = []
    for item in grouped.values():
        if not item["has_total_cash"]:
            item["total_cash_dividend_wan"] = None
        item["events"] = len(item["rows"])
        item["latest_ex_date"] = max((str(row.get("ex_date") or "") for row in item["rows"]), default="")
        item.pop("rows", None)
        item.pop("has_total_cash", None)
        out.append(item)
    return sorted(out, key=lambda row: row["year"], reverse=True)


def _row_by_year(rows: List[Dict[str, Any]], date_field: str) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        year = str(row.get(date_field) or "")[:4]
        if year and year not in out:
            out[year] = row
    return out


def _roe_decimal(roe: Optional[float]) -> Optional[float]:
    if roe is None:
        return None
    if abs(roe) > 1.5:
        return roe / 100.0
    return roe


def _debt_to_assets_decimal(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    if value > 1.0:
        return value / 100.0
    return value


def _consecutive_dividend_years(annual: List[Dict[str, Any]]) -> int:
    years = []
    for row in annual:
        cash = _to_float(row.get("total_cash_dividend_wan"))
        year_text = str(row.get("year") or "")
        if cash is None or cash <= 0 or len(year_text) < 4:
            continue
        try:
            years.append(int(year_text[:4]))
        except ValueError:
            continue
    if not years:
        return 0
    years = sorted(set(years), reverse=True)
    count = 1
    previous = years[0]
    for year in years[1:]:
        if year != previous - 1:
            break
        count += 1
        previous = year
    return count


def _dividend_quality(annual: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Structured dividend sustainability signals (no single opaque score)."""
    if not annual:
        return {
            "status": "insufficient_data",
            "years_paid": 0,
            "consecutive_years": 0,
            "signals": {},
            "warnings": [],
        }

    paid_rows = [row for row in annual if _to_float(row.get("total_cash_dividend_wan"))]
    years_paid = len(paid_rows)
    consecutive_years = _consecutive_dividend_years(annual)
    recent = annual[:5]
    payouts = []
    ocfs = []
    for row in recent:
        pr = _to_float(row.get("payout_ratio"))
        if pr is not None:
            payouts.append(pr)
        oc = _to_float(row.get("ocf_coverage"))
        if oc is not None:
            ocfs.append(oc)

    med_payout = median(payouts) if payouts else None
    med_ocf = median(ocfs) if ocfs else None

    recent_three_pr = [_to_float(row.get("payout_ratio")) for row in annual[:3]]
    recent_three_pr = [x for x in recent_three_pr if x is not None]
    sustained_over_100 = (
        len(recent_three_pr) >= 2 and sum(1 for x in recent_three_pr if x > 1.0) >= 2
    )

    cashes: List[tuple[str, float]] = []
    for row in reversed(annual[:5]):
        v = _to_float(row.get("total_cash_dividend_wan"))
        if v is not None and v > 0:
            cashes.append((str(row.get("year") or ""), v))

    dividend_cagr: Optional[float] = None
    cagr_confidence = "low"
    if len(cashes) >= 4:
        cagr_confidence = "normal"
        y0, v0 = cashes[0]
        y1, v1 = cashes[-1]
        try:
            span = int(y1[:4]) - int(y0[:4])
        except ValueError:
            span = 0
        if span > 0 and v0 > 0:
            dividend_cagr = (v1 / v0) ** (1.0 / span) - 1.0

    warnings: List[str] = []
    if sustained_over_100:
        warnings.append(
            "近若干年分红率多期超过 100%，可能含特别分红或口径波动，需结合年报人工复核。"
        )
    if med_ocf is not None and med_ocf < 1.0:
        warnings.append("经营现金流对现金分红的中位覆盖不足 1 倍，分红可持续性承压。")

    signals = {
        "median_payout_ratio_3y": round(med_payout, 4) if med_payout is not None else None,
        "median_ocf_coverage_3y": round(med_ocf, 4) if med_ocf is not None else None,
        "sustained_payout_over_100pct": sustained_over_100,
        "dividend_cagr": round(dividend_cagr, 4) if dividend_cagr is not None else None,
        "cagr_confidence": cagr_confidence,
    }

    return {
        "status": "ok" if years_paid >= 2 else "insufficient_data",
        "years_paid": years_paid,
        "consecutive_years": consecutive_years,
        "median_payout_ratio_3y": signals["median_payout_ratio_3y"],
        "median_ocf_coverage_3y": signals["median_ocf_coverage_3y"],
        "dividend_cagr": signals["dividend_cagr"],
        "cagr_confidence": cagr_confidence,
        "signals": signals,
        "warnings": warnings,
    }


def _ddm_risk_adjustment(
    dividend_quality: Dict[str, Any],
    *,
    roe_dec: Optional[float],
    debt_to_assets_dec: Optional[float],
) -> float:
    risk_adj = 0.0
    signals = dividend_quality.get("signals") or {}
    if signals.get("sustained_payout_over_100pct"):
        risk_adj += 0.005
    med_ocf = _to_float(signals.get("median_ocf_coverage_3y"))
    if med_ocf is not None and med_ocf < 1.0:
        risk_adj += 0.01
    if med_ocf is not None and med_ocf >= 1.5:
        risk_adj -= 0.005
    if debt_to_assets_dec is not None and debt_to_assets_dec > 0.60:
        risk_adj += 0.01
    if roe_dec is not None and roe_dec < 0.08:
        risk_adj += 0.01
    if int(dividend_quality.get("years_paid") or 0) >= 5:
        risk_adj -= 0.003
    return _clamp(risk_adj, -0.02, 0.04)


def _ddm_validation(
    dividends: List[Dict[str, Any]],
    income: List[Dict[str, Any]],
    cashflow: List[Dict[str, Any]],
    market: Dict[str, Any],
    config: Dict[str, Any],
    *,
    indicators: List[Dict[str, Any]],
) -> Dict[str, Any]:
    annual = _aggregate_dividend_years(dividends)
    income_by_year = _row_by_year(income, "end_date")
    cashflow_by_year = _row_by_year(cashflow, "end_date")
    for row in annual:
        income_row = income_by_year.get(row["year"]) or {}
        cashflow_row = cashflow_by_year.get(row["year"]) or {}
        total_cash = _to_float(row.get("total_cash_dividend_wan"))
        net_profit = _to_float(income_row.get("n_income_attr_p") or income_row.get("n_income"))
        operating_cashflow = _to_float(cashflow_row.get("n_cashflow_act"))
        row["payout_ratio"] = total_cash / (net_profit / 10000) if total_cash and net_profit else None
        row["ocf_coverage"] = (operating_cashflow / 10000) / total_cash if total_cash and operating_cashflow else None

    dividend_quality = _dividend_quality(annual)

    usable_cash = [
        _to_float(row.get("total_cash_dividend_wan"))
        for row in annual[:5]
        if _to_float(row.get("total_cash_dividend_wan")) is not None
    ]
    if len(usable_cash) < 2:
        return {
            "status": "insufficient_data",
            "reason": "缺少可计算年度分红总额的实施分红记录",
            "annual_dividends": annual[:5],
            "scenarios": [],
            "dividend_quality": dividend_quality,
        }

    latest_ind = indicators[0] if indicators else {}
    roe_dec = _roe_decimal(_to_float(latest_ind.get("roe")))
    debt_to_assets_dec = _debt_to_assets_decimal(_to_float(latest_ind.get("debt_to_assets")))
    risk_adj = _ddm_risk_adjustment(
        dividend_quality,
        roe_dec=roe_dec,
        debt_to_assets_dec=debt_to_assets_dec,
    )
    rf = float(config["risk_free"])
    erp = float(config["equity_risk_premium"])
    base_wacc = float(config["base_wacc"])
    g_cap = float(config.get("ddm_growth_cap", 0.03))

    ke_base = max(rf + erp + risk_adj, base_wacc)
    ke_floor = base_wacc

    med_payout = _to_float((dividend_quality.get("signals") or {}).get("median_payout_ratio_3y"))
    if med_payout is None:
        med_payout = _to_float((annual[0] if annual else {}).get("payout_ratio"))
    if med_payout is not None:
        med_payout = _clamp(med_payout, 0.0, 1.0)

    if roe_dec is not None and med_payout is not None:
        g_base = roe_dec * (1.0 - med_payout)
        g_source = "sustainable_growth_roe_times_one_minus_payout"
    elif roe_dec is not None:
        g_base = roe_dec * (1.0 - 0.5)
        g_source = "sustainable_growth_roe_with_default_50pct_retention"
    else:
        g_base = float(config["ddm_growth"])
        g_source = "fallback_config_ddm_growth"

    g_base = _clamp(g_base, 0.0, g_cap)
    g_base = min(g_base, max(0.0, ke_base - 0.001))

    base_dividend = median(usable_cash[:3])
    scenario_defs = [
        ("conservative", max(usable_cash[:3]) * 0.8, ke_base + 0.015, 0.0, "conservative_spread"),
        ("base", base_dividend, ke_base, g_base, "base_case"),
        ("optimistic", max(usable_cash[:3]) * 1.05, max(rf + 0.005, ke_base - 0.01), min(g_base + 0.01, g_cap), "optimistic_spread"),
    ]
    current_mv = _to_float((market.get("current") or {}).get("total_mv"))
    scenarios = []
    for name, dividend_wan, ke_raw, growth_raw, spread_label in scenario_defs:
        ke = max(ke_raw, base_wacc)
        growth = min(growth_raw, max(0.0, ke - 0.001))
        growth = _clamp(growth, 0.0, g_cap)
        if ke <= growth:
            value_wan = None
        else:
            value_wan = dividend_wan * (1 + growth) / (ke - growth)
        upside = (value_wan - current_mv) / current_mv * 100 if value_wan and current_mv else None
        ke_breakdown = {
            "method": "buildup_not_capm",
            "note": "无 beta 的简化 buildup，仅作区间参考",
            "risk_free": round(rf, 4),
            "equity_risk_premium": round(erp, 4),
            "risk_adjustment": round(risk_adj, 4),
            "ke_before_wacc_floor": round(rf + erp + risk_adj, 4),
            "wacc_consistency_floor": round(ke_floor, 4),
            "scenario_spread": spread_label,
            "final_ke": round(ke, 4),
        }
        scenarios.append(
            {
                "name": name,
                "cash_dividend_wan": round(dividend_wan, 2),
                "ke": round(ke, 4),
                "growth": round(growth, 4),
                "growth_source": g_source,
                "equity_value_wan": round(value_wan, 2) if value_wan is not None else None,
                "upside_pct": round(upside, 2) if upside is not None else None,
                "ke_breakdown": ke_breakdown,
            }
        )
    latest = annual[0] if annual else {}
    return {
        "status": "ok",
        "method": "Gordon dividend discount model using implemented cash dividends; dynamic ke/g (buildup)",
        "annual_dividends": annual[:5],
        "latest": latest,
        "scenarios": scenarios,
        "dividend_quality": dividend_quality,
        "sustainability": {
            "consecutive_years": dividend_quality.get("consecutive_years", 0),
            "median_cash_dividend_wan_3y": round(base_dividend, 2),
            "latest_payout_ratio": latest.get("payout_ratio"),
            "latest_ocf_coverage": latest.get("ocf_coverage"),
        },
        "ddm_assumptions": {
            "risk_free": rf,
            "equity_risk_premium": erp,
            "risk_adjustment": round(risk_adj, 4),
            "ke_base": round(ke_base, 4),
            "g_cap": g_cap,
        },
    }


def _percent_to_decimal(value: Optional[float]) -> Optional[float]:
    """Tushare financial_indicator 的同比增速/利润率恒为百分数口径，统一 /100。"""
    if value is None:
        return None
    return value / 100.0


def _fcff_positive_count(indicators: List[Dict[str, Any]], *, limit: int = 8) -> int:
    count = 0
    for row in indicators[:limit]:
        value = _to_float(row.get("fcff"))
        if value is not None and value > 0:
            count += 1
    return count


def _applicability_item(
    status: str,
    *,
    reasons: List[str],
    signals: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "status": status,
        "reasons": reasons,
        "signals": signals,
    }


def _recommended_from_applicability(
    applicability: Dict[str, Dict[str, Any]],
    tags: set[str],
) -> Tuple[str, float]:
    if "financial" in tags:
        # 银行/保险/证券：FCFF DCF 口径不匹配，统一以 PB-ROE / DDM 为主框架；
        # DDM 可用时置信度更高，否则给 PB-ROE 兜底，不退化为纯相对估值。
        ddm_status = (applicability.get("ddm") or {}).get("status")
        if ddm_status == "primary":
            return "pb_roe_or_ddm", 0.7
        if ddm_status == "secondary":
            return "pb_roe_or_ddm", 0.55
        return "pb_roe_or_ddm", 0.5
    priority = ("dcf", "tech_ps", "ddm", "relative_valuation")
    for key in priority:
        if (applicability.get(key) or {}).get("status") == "primary":
            return key, 0.7 if key != "relative_valuation" else 0.6
    for key in priority:
        if (applicability.get(key) or {}).get("status") == "secondary":
            return key, 0.55
    return "relative_valuation", 0.45


def _model_suitability(
    stock: Dict[str, Any],
    indicators: List[Dict[str, Any]],
    dcf: Dict[str, Any],
    market: Dict[str, Any],
    relative: Dict[str, Any],
    ddm: Dict[str, Any],
) -> Dict[str, Any]:
    tags = _industry_tag(stock)
    latest = indicators[0] if indicators else {}
    roe = _to_float(latest.get("roe"))
    fcff_positive_count = _fcff_positive_count(indicators)
    current = market.get("current") or {}
    dividend = _to_float(current.get("dv_ttm"))
    total_mv_wan = _to_float(current.get("total_mv"))
    tr_yoy = _to_float(latest.get("tr_yoy"))
    revenue_growth = _percent_to_decimal(tr_yoy)
    net_margin_raw = _to_float(latest.get("netprofit_margin"))
    if net_margin_raw is None:
        net_margin_raw = _to_float(latest.get("net_profit_margin"))
    if net_margin_raw is None:
        net_margin_raw = _to_float(latest.get("net_margin"))
    net_margin = _percent_to_decimal(net_margin_raw)
    base_scenario = next(
        (row for row in dcf.get("scenarios", []) if row.get("name") == "base"),
        {},
    )
    base_equity_value_wan = _to_float(base_scenario.get("equity_value_wan"))
    relative_percentiles = relative.get("percentiles") or {}
    usable_relative_count = sum(
        1
        for key in ("pe_ttm", "pb", "ps_ttm")
        if relative_percentiles.get(key) is not None
    )
    dividend_quality = ddm.get("dividend_quality") or {}
    consecutive_dividend_years = int(dividend_quality.get("consecutive_years") or 0)
    dividend_warnings = dividend_quality.get("warnings") or []
    high_growth_unprofitable_tech = (
        "tech" in tags
        and revenue_growth is not None
        and revenue_growth >= 0.20
        and (fcff_positive_count <= 2 or (net_margin is not None and net_margin <= 0))
    )

    if "financial" in tags:
        dcf_status = "not_recommended"
        dcf_reasons = ["金融行业普通 FCFF DCF 口径与净债务调整不匹配，仅作参考"]
    elif dcf.get("status") == "ok" and fcff_positive_count >= 5 and not high_growth_unprofitable_tech:
        dcf_status = "primary"
        dcf_reasons = ["近 8 期 FCFF 中至少 5 期为正，现金流可用于 DCF"]
    elif dcf.get("status") == "ok" and 3 <= fcff_positive_count <= 4:
        dcf_status = "secondary"
        dcf_reasons = ["近 8 期 FCFF 有 3-4 期为正，DCF 可作辅助参考"]
    else:
        dcf_status = "not_recommended"
        dcf_reasons = ["现金流样本不足或波动较大，DCF 不宜作为主模型"]

    if usable_relative_count >= 2:
        relative_status = "primary"
        relative_reasons = ["PE/PB/PS 至少 2 个历史分位可用"]
    elif usable_relative_count == 1:
        relative_status = "secondary"
        relative_reasons = ["仅 1 个历史分位可用，相对估值只能辅助参考"]
    else:
        relative_status = "not_recommended"
        relative_reasons = ["缺少可用 PE/PB/PS 历史分位"]

    if consecutive_dividend_years >= 3 and not dividend_warnings:
        ddm_status = "primary"
        ddm_reasons = ["DDM：连续实施分红至少 3 年，且分红质量未触发重大告警"]
    elif consecutive_dividend_years >= 3:
        ddm_status = "secondary"
        ddm_reasons = ["DDM：连续实施分红至少 3 年，但分红质量有告警，仅作辅助"]
    elif 1 <= consecutive_dividend_years <= 2:
        ddm_status = "secondary"
        ddm_reasons = ["DDM：有实施分红记录，但连续年数不足 3 年"]
    else:
        ddm_status = "not_applicable"
        ddm_reasons = ["DDM：缺少连续实施分红记录，暂不适用"]

    if "tech" not in tags:
        tech_status = "not_applicable"
        tech_reasons = ["非科技行业标签"]
    elif high_growth_unprofitable_tech:
        tech_status = "primary"
        tech_reasons = ["科技行业且收入增速较高，同时 DCF 因现金流或盈利性被降权"]
    else:
        tech_status = "secondary"
        tech_reasons = ["科技行业但现金流/盈利性相对稳定，PS 框架作为辅助解释"]

    signals = {
        "roe": roe,
        "fcff_positive_count_8": fcff_positive_count,
        "dv_ttm": dividend,
        "industry_tags": sorted(tags),
        "tr_yoy": tr_yoy,
        "revenue_growth": round(revenue_growth, 4) if revenue_growth is not None else None,
        "net_margin": round(net_margin, 4) if net_margin is not None else None,
        "relative_percentile_count": usable_relative_count,
        "consecutive_dividend_years": consecutive_dividend_years,
        "high_growth_unprofitable_tech": high_growth_unprofitable_tech,
    }
    applicability = {
        "dcf": _applicability_item(dcf_status, reasons=dcf_reasons, signals=signals),
        "relative_valuation": _applicability_item(
            relative_status,
            reasons=relative_reasons,
            signals={"usable_percentile_count": usable_relative_count},
        ),
        "ddm": _applicability_item(
            ddm_status,
            reasons=ddm_reasons,
            signals={
                "consecutive_dividend_years": consecutive_dividend_years,
                "warning_count": len(dividend_warnings),
            },
        ),
        "tech_ps": _applicability_item(
            tech_status,
            reasons=tech_reasons,
            signals={
                "is_tech": "tech" in tags,
                "tr_yoy": tr_yoy,
                "revenue_growth": round(revenue_growth, 4) if revenue_growth is not None else None,
                "net_margin": round(net_margin, 4) if net_margin is not None else None,
                "fcff_positive_count_8": fcff_positive_count,
            },
        ),
    }
    recommended, confidence = _recommended_from_applicability(applicability, tags)

    reasons: List[str] = []
    for key in ("dcf", "tech_ps", "ddm", "relative_valuation"):
        item = applicability[key]
        if item["status"] == "primary":
            reasons.extend(item["reasons"][:1])
    if not reasons:
        reasons.append("缺少主模型信号，优先把估值结果作为数据质量提示而非目标价")
    if roe is not None:
        reasons.append(f"最新 ROE 为 {roe:.2f}")
    if dividend is not None and dividend > 0:
        reasons.append(f"dv_ttm 为 {dividend:.2f}，可与分红质量交叉验证")
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
        "signals": signals,
        "model_applicability": applicability,
    }


def _days_since_yyyymmdd(value: Any) -> Optional[int]:
    text = str(value or "")
    if len(text) < 8:
        return None
    try:
        parsed = datetime.strptime(text[:8], "%Y%m%d").date()
    except ValueError:
        return None
    return (date.today() - parsed).days


def _valuation_confidence(
    *,
    dcf: Dict[str, Any],
    indicators: List[Dict[str, Any]],
    daily_basic: List[Dict[str, Any]],
    market: Dict[str, Any],
) -> Dict[str, Any]:
    score = 100
    reasons: List[str] = []

    per_share = dcf.get("per_share") or {}
    shares_basis = str(per_share.get("shares_basis") or "")
    if per_share.get("status") != "ok":
        score -= 35
        reasons.append("未能可靠得到每股股本口径")
    elif shares_basis.startswith("financial_daily_basic.total_mv"):
        reasons.append("股本由 financial_daily_basic.total_mv/close 推算，质量较高")
    elif shares_basis.startswith("financial_daily_basic.circ_mv"):
        score -= 8
        reasons.append("股本由 circ_mv/close 推算，可能偏流通股口径")
    elif shares_basis == "stock_info.total_share":
        score -= 5
        reasons.append("股本来自 stock_info.total_share，未依赖当日市值")
    elif shares_basis.startswith("stock_info."):
        score -= 12
        reasons.append("股本来自 stock_info 兜底字段，需留意同步口径")
    else:
        score -= 15
        reasons.append("股本来源不是首选口径")

    fcff_values = [
        _to_float(row.get("fcff"))
        for row in indicators[:8]
        if _to_float(row.get("fcff")) is not None
    ]
    positive_count = sum(1 for value in fcff_values if value is not None and value > 0)
    if len(fcff_values) >= 5 and positive_count >= 5:
        reasons.append("近 8 期 FCFF 样本较充分且多为正")
    elif len(fcff_values) >= 3 and positive_count >= 3:
        score -= 12
        reasons.append("FCFF 样本可用但稳定性一般")
    else:
        score -= 25
        reasons.append("FCFF 样本不足或正值比例偏低")

    latest_indicator_date = (indicators[0] if indicators else {}).get("end_date")
    freshness_days = _days_since_yyyymmdd(latest_indicator_date)
    if freshness_days is None:
        score -= 15
        reasons.append("缺少最新财报日期，财务新鲜度无法判断")
    elif freshness_days > 730:
        score -= 25
        reasons.append("最新财报距今超过两年")
    elif freshness_days > 550:
        score -= 12
        reasons.append("最新财报距今超过约 18 个月")
    else:
        reasons.append("最新财报日期仍在可接受范围")

    current = market.get("current") or {}
    close = _to_float(current.get("close")) or _to_float(per_share.get("close"))
    total_mv = _to_float(current.get("total_mv")) or _to_float(per_share.get("total_mv_wan"))
    if close is None or close <= 0:
        score -= 10
        reasons.append("缺少有效收盘价")
    if total_mv is None or total_mv <= 0:
        score -= 10
        reasons.append("缺少有效总市值")
    if not daily_basic:
        score -= 10
        reasons.append("缺少 financial_daily_basic 历史样本")

    score = int(_clamp(score, 0, 100))
    if score >= 80:
        level = "high"
    elif score >= 55:
        level = "medium"
    else:
        level = "low"
    return {
        "level": level,
        "score": score,
        "reasons": reasons,
        "signals": {
            "shares_basis": shares_basis or None,
            "fcff_sample_count_8": len(fcff_values),
            "fcff_positive_count_8": positive_count,
            "latest_indicator_end_date": latest_indicator_date,
            "latest_indicator_age_days": freshness_days,
            "has_close": close is not None and close > 0,
            "has_total_mv": total_mv is not None and total_mv > 0,
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
            "netprofit_margin": 1,
            "net_profit_margin": 1,
            "net_margin": 1,
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
    stock = _latest_stock_info(data_db, candidates)
    balance = _recent_docs(
        _collection(data_db, "financial_balance"),
        candidates,
        date_field="end_date",
        limit=1,
        projection={
            "_id": 0,
            "ts_code": 1,
            "symbol": 1,
            "end_date": 1,
            "money_cap": 1,
            "st_borr": 1,
            "lt_borr": 1,
            "bond_payable": 1,
            "non_cur_liab_due_1y": 1,
            "minority_int": 1,
        },
    )
    market = _market_multiples(daily_basic)
    dcf = _dcf_scenarios(
        indicators,
        cashflow,
        market,
        valuation_config,
        stock=stock,
        balance=balance,
        daily_basic=daily_basic,
        data_db=data_db,
        candidates=candidates,
    )
    relative = _relative_valuation(market, indicators)
    dividends = _recent_docs(
        _collection(data_db, "financial_dividend"),
        candidates,
        date_field="end_date",
        limit=80,
        projection={
            "_id": 0,
            "ts_code": 1,
            "end_date": 1,
            "ann_date": 1,
            "div_proc": 1,
            "cash_div": 1,
            "cash_div_tax": 1,
            "record_date": 1,
            "ex_date": 1,
            "pay_date": 1,
            "base_share": 1,
        },
    )
    income = _recent_docs(
        _collection(data_db, "financial_income"),
        candidates,
        date_field="end_date",
        limit=20,
        projection={"_id": 0, "ts_code": 1, "end_date": 1, "n_income_attr_p": 1, "n_income": 1},
    )
    ddm = _ddm_validation(
        dividends,
        income,
        cashflow,
        market,
        valuation_config,
        indicators=indicators,
    )
    score = _latest_score(finance_db, candidates, None)
    suitability = _model_suitability(stock, indicators, dcf, market, relative, ddm)
    valuation_confidence = _valuation_confidence(
        dcf=dcf,
        indicators=indicators,
        daily_basic=daily_basic,
        market=market,
    )
    as_of = (
        (daily_basic[0] if daily_basic else {}).get("trade_date")
        or (indicators[0] if indicators else {}).get("end_date")
    )
    found = bool(daily_basic or indicators or cashflow or dividends or score)
    return {
        "symbol": symbol,
        "market_multiples": market,
        "dcf": dcf,
        "ddm": ddm,
        "relative_valuation": relative,
        "model_suitability": suitability,
        "valuation_confidence": valuation_confidence,
        "value_score_link": score,
        "inputs": {
            "daily_basic_count": len(daily_basic),
            "indicator_count": len(indicators),
            "cashflow_count": len(cashflow),
            "balance_count": len(balance),
            "dividend_count": len(dividends),
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
