from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends

from auth import get_current_active_user
from db import get_analyzer_db, get_data_db, get_db_collections
from routers.stock import _json_safe, _symbol_candidates
from services.analysis_history import build_user_analysis_history_payload

router = APIRouter()


def _to_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _first_float(*values: Any) -> Optional[float]:
    for value in values:
        number = _to_float(value)
        if number is not None:
            return number
    return None


def _find_first(collection, candidates: List[str], projection: Optional[Dict[str, int]] = None) -> Optional[Dict[str, Any]]:
    for field in ("symbol", "ts_code", "con_code"):
        doc = collection.find_one({field: {"$in": candidates}}, projection)
        if doc:
            return doc
    return None


def _latest_score(symbol: str) -> Optional[Dict[str, Any]]:
    collections = get_db_collections()
    candidates = _symbol_candidates(symbol)
    return collections["stock_scores"].find_one(
        {"symbol": {"$in": candidates}},
        sort=[("score_date", -1), ("_id", -1)],
    )


def _score_history(symbol: str, limit: int = 60) -> List[Dict[str, Any]]:
    collections = get_db_collections()
    candidates = _symbol_candidates(symbol)
    cursor = collections["stock_scores"].find(
        {"symbol": {"$in": candidates}},
        {
            "_id": 0,
            "symbol": 1,
            "score_date": 1,
            "technical_score": 1,
            "fundamental_score": 1,
            "value_score": 1,
            "growth_score": 1,
            "money_flow_score": 1,
            "cycle_score": 1,
            "composite_score": 1,
        },
        sort=[("score_date", -1), ("_id", -1)],
        limit=limit,
    )
    return list(cursor)


def _latest_daily_quote(data: Any, candidates: List[str], trade_date: Any = None) -> Optional[Dict[str, Any]]:
    volume_price = data["volume_price"]
    query: Dict[str, Any] = {"symbol": {"$in": candidates}}
    if trade_date:
        same_day = volume_price.find_one(
            {**query, "trade_date": trade_date},
            sort=[("trade_date", -1), ("_id", -1)],
        )
        if same_day:
            return same_day
        query["trade_date"] = {"$lt": trade_date}

    return volume_price.find_one(query, sort=[("trade_date", -1), ("_id", -1)])


def _recent_docs_for_symbol(
    collection: Any,
    candidates: List[str],
    *,
    date_field: str,
    limit: int,
    projection: Optional[Dict[str, int]] = None,
    fields: tuple[str, ...] = ("ts_code", "symbol"),
) -> List[Dict[str, Any]]:
    for field in fields:
        cursor = collection.find(
            {field: {"$in": candidates}},
            projection,
            sort=[(date_field, -1), ("_id", -1)],
            limit=limit,
        )
        docs = list(cursor)
        if docs:
            return docs
    return []


def _quote_section(symbol: str) -> Dict[str, Any]:
    data = get_data_db()
    candidates = _symbol_candidates(symbol)
    quote = _latest_quote(symbol)
    daily_quotes = _recent_docs_for_symbol(
        data["volume_price"],
        candidates,
        date_field="trade_date",
        limit=160,
        projection={
            "_id": 0,
            "symbol": 1,
            "trade_date": 1,
            "open": 1,
            "high": 1,
            "low": 1,
            "close": 1,
            "pre_close": 1,
            "pct_chg": 1,
            "vol": 1,
            "volume": 1,
            "amount": 1,
        },
        fields=("symbol",),
    )
    daily_basic = _recent_docs_for_symbol(
        data["financial_daily_basic"],
        candidates,
        date_field="trade_date",
        limit=1,
        projection={
            "_id": 0,
            "symbol": 1,
            "ts_code": 1,
            "trade_date": 1,
            "close": 1,
            "turnover_rate": 1,
            "turnover_rate_f": 1,
            "volume_ratio": 1,
            "pe": 1,
            "pe_ttm": 1,
            "pb": 1,
            "ps_ttm": 1,
            "dv_ttm": 1,
            "total_mv": 1,
            "circ_mv": 1,
        },
    )
    money_flow = _recent_docs_for_symbol(
        data["money_flow"],
        candidates,
        date_field="trade_date",
        limit=1,
        projection={
            "_id": 0,
            "symbol": 1,
            "ts_code": 1,
            "trade_date": 1,
            "buy_sm_vol": 1,
            "buy_sm_amount": 1,
            "sell_sm_vol": 1,
            "sell_sm_amount": 1,
            "buy_md_vol": 1,
            "buy_md_amount": 1,
            "sell_md_vol": 1,
            "sell_md_amount": 1,
            "buy_lg_vol": 1,
            "buy_lg_amount": 1,
            "sell_lg_vol": 1,
            "sell_lg_amount": 1,
            "buy_elg_vol": 1,
            "buy_elg_amount": 1,
            "sell_elg_vol": 1,
            "sell_elg_amount": 1,
            "net_mf_vol": 1,
            "net_mf_amount": 1,
        },
    )
    status = _quote_status(quote)
    if not status["as_of"]:
        status["as_of"] = (daily_quotes[0] if daily_quotes else {}).get("trade_date")
    status["found"] = bool(quote or daily_quotes or daily_basic or money_flow)
    status["source"] = "quant_realtime.volume_price_realtime_latest + quant_data.volume_price/money_flow/financial_daily_basic"
    return {
        "symbol": symbol,
        "quote": quote,
        "daily_quotes": daily_quotes,
        "daily_basic": daily_basic[0] if daily_basic else None,
        "money_flow": money_flow[0] if money_flow else None,
        "data_status": status,
    }


def _attach_display_pct_change(data: Any, quote: Optional[Dict[str, Any]], candidates: List[str]) -> Optional[Dict[str, Any]]:
    """Normalize workbench quote change to the common A-share close/pre-close basis."""
    if not quote:
        return quote

    close = _first_float(quote.get("close"), quote.get("price"), quote.get("latest_price"))
    quote_trade_date = quote.get("trade_date")
    previous_close = _first_float(
        quote.get("pre_close"),
        quote.get("prev_close"),
        quote.get("previous_close"),
        quote.get("yesterday_close"),
    )
    previous_close_source = "quote.pre_close" if previous_close is not None else None

    daily_quote = None
    if previous_close is None:
        try:
            daily_quote = _latest_daily_quote(data, candidates, quote_trade_date)
            if daily_quote:
                if quote_trade_date and daily_quote.get("trade_date") == quote_trade_date:
                    previous_close = _first_float(
                        daily_quote.get("pre_close"),
                        daily_quote.get("prev_close"),
                        daily_quote.get("previous_close"),
                    )
                    previous_close_source = "daily.pre_close"
                else:
                    previous_close = _first_float(daily_quote.get("close"))
                    previous_close_source = "previous_daily.close"
        except Exception:
            previous_close = None
            previous_close_source = None

    if close is not None and previous_close and previous_close > 0:
        quote["display_pct_chg"] = (close - previous_close) / previous_close * 100.0
        quote["display_pct_chg_source"] = previous_close_source or "computed_from_pre_close"
        quote["display_pre_close"] = previous_close
        return quote

    fallback_pct = _first_float(quote.get("pct_chg"), quote.get("pct_change"), quote.get("change_percent"))
    if fallback_pct is not None:
        quote["display_pct_chg"] = fallback_pct
        quote["display_pct_chg_source"] = "quote_pct_field"
    else:
        quote["display_pct_chg"] = None
        quote["display_pct_chg_source"] = "unavailable"
    return quote


def _latest_quote(symbol: str) -> Optional[Dict[str, Any]]:
    data = get_data_db()
    candidates = _symbol_candidates(symbol)
    quote = data["volume_price_realtime_latest"].find_one(
        {"symbol": {"$in": candidates}},
        sort=[("trade_time", -1), ("trade_date", -1), ("_id", -1)],
    )
    if quote:
        quote["source"] = "realtime"
        return _attach_display_pct_change(data, quote, candidates)

    quote = _latest_daily_quote(data, candidates)
    if quote:
        quote["source"] = "daily"
        if quote.get("display_pct_chg") is None:
            quote["display_pct_chg"] = _first_float(quote.get("pct_chg"), quote.get("pct_change"))
            quote["display_pct_chg_source"] = "daily.pct_chg"
    return quote


def _stock_identity(symbol: str) -> Dict[str, Any]:
    data = get_data_db()
    candidates = _symbol_candidates(symbol)
    stock = _find_first(data["stock_info"], candidates) or {}
    if not stock:
        stock = _find_first(data["stock_basic"], candidates) or {}
    if not stock:
        stock = {"symbol": candidates[0]}

    industry = None
    member = None
    sw_member = data.get("sw_index_member")
    if sw_member is not None:
        for c in candidates:
            member = sw_member.find_one(
                {"con_code": c, "src": "SW2021", "is_new": "Y"},
                {
                    "_id": 0,
                    "con_code": 1,
                    "name": 1,
                    "l1_code": 1,
                    "l1_name": 1,
                    "l2_code": 1,
                    "l2_name": 1,
                    "l3_code": 1,
                    "l3_name": 1,
                    "src": 1,
                },
            )
            if member:
                break
        if not member:
            for c in candidates:
                member = sw_member.find_one(
                    {"con_code": c, "src": "SW2021"},
                    {
                        "_id": 0,
                        "con_code": 1,
                        "name": 1,
                        "l1_code": 1,
                        "l1_name": 1,
                        "l2_code": 1,
                        "l2_name": 1,
                        "l3_code": 1,
                        "l3_name": 1,
                        "src": 1,
                    },
                    sort=[("in_date", -1), ("_id", -1)],
                )
                if member:
                    break
    if member:
        industry = member

    return {
        **stock,
        "symbol": stock.get("symbol") or stock.get("ts_code") or stock.get("con_code") or candidates[0],
        "name": stock.get("name") or stock.get("stock_name") or (member or {}).get("name", ""),
        "industry": stock.get("industry") or (member or {}).get("l1_name", ""),
        "shenwan_industry": industry,
    }


def _latest_deep_analysis(user_id: Any, symbol: str) -> Optional[Dict[str, Any]]:
    payload = build_user_analysis_history_payload(user_id, symbol=symbol, page=1, limit=1)
    rows = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(rows, list) and rows:
        return rows[0]
    return None


def _analysis_history(user_id: Any, symbol: str, limit: int = 5) -> Dict[str, Any]:
    payload = build_user_analysis_history_payload(user_id, symbol=symbol, page=1, limit=limit)
    rows = payload.get("data") if isinstance(payload, dict) else []
    return {
        "rows": rows if isinstance(rows, list) else [],
        "total": payload.get("total") if isinstance(payload, dict) else 0,
    }


def _accuracy_label(value: Any) -> str:
    return {
        "accurate": "准确",
        "mixed": "部分准确",
        "inaccurate": "不准确",
    }.get(str(value or ""), str(value or "未评估"))


def _analysis_history_title(row: Dict[str, Any]) -> str:
    analysis = row.get("analysis") if isinstance(row.get("analysis"), dict) else {}
    return (
        analysis.get("final_conclusion")
        or analysis.get("investment_advice")
        or analysis.get("long_term_forecast")
        or analysis.get("mid_term_forecast")
        or analysis.get("short_term_forecast")
        or row.get("stock_name")
        or "历史分析"
    )


def _analysis_evaluation_summary(history: Dict[str, Any]) -> Dict[str, Any]:
    rows = history.get("rows") if isinstance(history, dict) else []
    rows = rows if isinstance(rows, list) else []
    history_ids = [str(row.get("id")) for row in rows if row.get("id")]
    if not history_ids:
        return {
            "total_analyses": history.get("total") if isinstance(history, dict) else 0,
            "recent_analyses": 0,
            "evaluated_count": 0,
            "pending_count": 0,
            "accuracy_counts": {},
            "accuracy_rate": None,
            "latest_evaluations": [],
            "data_status": _section_status(
                found=False,
                expected_freshness="用户手动触发评估后更新",
                source="quant_analyzer.analysis_evaluations",
            ),
        }

    eval_rows = list(
        get_analyzer_db()["analysis_evaluations"].find(
            {"history_id": {"$in": history_ids}},
            {"_id": 0},
        )
    )
    evaluations_by_history_id = {
        str(doc.get("history_id")): doc for doc in eval_rows if doc.get("history_id")
    }
    accuracy_counts: Dict[str, int] = {}
    latest_evaluations: List[Dict[str, Any]] = []
    latest_as_of = None

    for row in rows:
        history_id = str(row.get("id") or "")
        doc = evaluations_by_history_id.get(history_id)
        if not doc:
            continue
        review = doc.get("llm_review") if isinstance(doc.get("llm_review"), dict) else {}
        accuracy = review.get("outcome_accuracy")
        if accuracy:
            accuracy_counts[accuracy] = accuracy_counts.get(accuracy, 0) + 1
        as_of = doc.get("evaluated_at") or doc.get("updated_at") or doc.get("created_at")
        if as_of and (latest_as_of is None or str(as_of) > str(latest_as_of)):
            latest_as_of = as_of
        latest_evaluations.append(
            {
                "history_id": history_id,
                "analysis_created_at": row.get("created_at"),
                "analysis_mode": row.get("analysis_mode"),
                "title": _analysis_history_title(row),
                "evaluation_status": doc.get("evaluation_status"),
                "evaluated_at": as_of,
                "outcome_accuracy": accuracy,
                "outcome_accuracy_label": _accuracy_label(accuracy),
                "reasoning_quality": review.get("reasoning_quality"),
                "risk_calibration": review.get("risk_calibration"),
                "summary": review.get("summary"),
                "short_review": review.get("short_review"),
                "price_windows": doc.get("price_windows") or {},
            }
        )

    evaluated_count = len(evaluations_by_history_id)
    accurate_count = accuracy_counts.get("accurate", 0)
    mixed_count = accuracy_counts.get("mixed", 0)
    mature_count = sum(accuracy_counts.values())
    return {
        "total_analyses": history.get("total") if isinstance(history, dict) else len(rows),
        "recent_analyses": len(rows),
        "evaluated_count": evaluated_count,
        "pending_count": max(0, len(history_ids) - evaluated_count),
        "accuracy_counts": accuracy_counts,
        "accuracy_rate": round(accurate_count / mature_count * 100, 2) if mature_count else None,
        "hit_rate": round((accurate_count + mixed_count * 0.5) / mature_count * 100, 2) if mature_count else None,
        "latest_evaluations": latest_evaluations,
        "data_status": _section_status(
            found=bool(evaluated_count),
            as_of=latest_as_of,
            expected_freshness="用户手动触发评估后更新",
            source="quant_analyzer.analysis_evaluations",
        ),
    }


def _user_id_candidates(user_id: Any) -> List[Any]:
    values = [user_id, str(user_id)]
    return list(dict.fromkeys(value for value in values if value is not None))


def _symbol_matches_any(value: Any, candidates: List[str]) -> bool:
    norm = str(value or "").upper()
    if not norm:
        return False
    candidate_set = {str(item).upper() for item in candidates}
    candidate_set.update(item.split(".", 1)[0] for item in candidate_set)
    return norm in candidate_set or norm.split(".", 1)[0] in candidate_set


def _trading_status(signals: List[Dict[str, Any]], positions: List[Dict[str, Any]], in_watchlist: bool) -> Dict[str, Any]:
    latest = signals[0] if signals else None
    return _section_status(
        found=bool(in_watchlist or signals or positions),
        as_of=(latest or {}).get("created_at") or (latest or {}).get("timestamp") or (latest or {}).get("updated_at"),
        expected_freshness="随用户自选、持仓和交易信号更新",
        source="finance.user_watchlists/watchlist_strategies/trader_positions/trade_signals",
    )


def _trading_section(user_id: Any, symbol: str) -> Dict[str, Any]:
    collections = get_db_collections()
    candidates = _symbol_candidates(symbol)
    user_ids = _user_id_candidates(user_id)

    watchlist_doc = collections["user_watchlists"].find_one({"user_id": {"$in": user_ids}}) or {}
    watchlist_symbols = watchlist_doc.get("symbols") or []
    in_watchlist = any(_symbol_matches_any(item, candidates) for item in watchlist_symbols)

    strategies = list(
        collections["watchlist_strategies"].find(
            {"user_id": {"$in": user_ids}, "symbol": {"$in": candidates}},
            {"_id": 0},
            sort=[("updated_at", -1)],
            limit=20,
        )
    )
    positions = list(
        collections["trader_positions"].find(
            {"user_id": {"$in": user_ids}, "symbol": {"$in": candidates}},
            {"_id": 0},
            sort=[("updated_at", -1), ("timestamp", -1)],
            limit=10,
        )
    )
    signals = list(
        collections["trade_signals"].find(
            {"user_id": {"$in": user_ids}, "symbol": {"$in": candidates}},
            {"_id": 0},
            sort=[("created_at", -1), ("timestamp", -1)],
            limit=10,
        )
    )

    return {
        "symbol": symbol,
        "watchlist": {
            "in_watchlist": in_watchlist,
            "symbol_count": len(watchlist_symbols),
        },
        "watchlist_strategies": strategies,
        "positions": positions,
        "recent_signals": signals,
        "data_status": _trading_status(signals, positions, in_watchlist),
    }


def _section_status(
    *,
    found: bool,
    as_of: Any = None,
    source: str,
    expected_freshness: str = "",
    stale: bool = False,
) -> Dict[str, Any]:
    return {
        "found": bool(found),
        "as_of": as_of,
        "stale": stale,
        "expected_freshness": expected_freshness,
        "source": source,
    }


def _quote_status(quote: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return _section_status(
        found=bool(quote),
        as_of=(quote or {}).get("trade_date") or (quote or {}).get("datetime") or (quote or {}).get("trade_time"),
        expected_freshness="盘中实时快照优先，缺失时使用最近交易日 T+1 日线",
        source=(quote or {}).get("source") or "quant_data.volume_price",
    )


def _score_status(score: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return _section_status(
        found=bool(score),
        as_of=(score or {}).get("score_date"),
        expected_freshness="随评分任务刷新",
        source="finance.stock_scores",
    )


def _financial_status(indicators: List[Dict[str, Any]]) -> Dict[str, Any]:
    latest = indicators[0] if indicators else {}
    return _section_status(
        found=bool(indicators),
        as_of=latest.get("end_date") or latest.get("period") or latest.get("ann_date"),
        expected_freshness="财报披露后同步，通常按季更新",
        source="quant_data.financial_* + quant_data.earnings_*",
    )


def _ai_status(deep_analysis: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return _section_status(
        found=bool(deep_analysis),
        as_of=(deep_analysis or {}).get("created_at"),
        expected_freshness="用户手动触发后更新",
        source="finance.analysis_history",
    )


def _financials_section(symbol: str, periods: int = 40) -> Dict[str, Any]:
    data = get_data_db()
    candidates = _symbol_candidates(symbol)
    income = _recent_docs_for_symbol(
        data["financial_income"],
        candidates,
        date_field="end_date",
        limit=periods,
        projection={"_id": 0},
    )
    balance = _recent_docs_for_symbol(
        data["financial_balance"],
        candidates,
        date_field="end_date",
        limit=periods,
        projection={"_id": 0},
    )
    cashflow = _recent_docs_for_symbol(
        data["financial_cashflow"],
        candidates,
        date_field="end_date",
        limit=periods,
        projection={"_id": 0},
    )
    indicators = _recent_docs_for_symbol(
        data["financial_indicator"],
        candidates,
        date_field="end_date",
        limit=periods,
        projection={"_id": 0},
    )
    earnings_forecast = _recent_docs_for_symbol(
        data["earnings_forecast"],
        candidates,
        date_field="ann_date",
        limit=3,
        projection={"_id": 0},
    )
    earnings_express = _recent_docs_for_symbol(
        data["earnings_express"],
        candidates,
        date_field="ann_date",
        limit=3,
        projection={"_id": 0},
    )
    earnings_disclosure = _recent_docs_for_symbol(
        data["earnings_disclosure"],
        candidates,
        date_field="ann_date",
        limit=3,
        projection={"_id": 0},
    )
    earnings_report_rc = _recent_docs_for_symbol(
        data["earnings_report_rc"],
        candidates,
        date_field="ann_date",
        limit=3,
        projection={"_id": 0},
    )
    return {
        "symbol": symbol,
        "income": income,
        "balance": balance,
        "cashflow": cashflow,
        "indicators": indicators,
        "earnings": {
            "forecast": earnings_forecast,
            "express": earnings_express,
            "disclosure": earnings_disclosure,
            "report_rc": earnings_report_rc,
        },
        "data_status": _financial_status(indicators or income),
    }


@router.get("/{symbol}/workbench")
def get_stock_workbench(
    symbol: str,
    current_user: Dict[str, Any] = Depends(get_current_active_user),
):
    """Single-stock research workbench payload."""
    stock = _stock_identity(symbol)
    quote = _latest_quote(symbol)
    score = _latest_score(symbol)
    deep_analysis = _latest_deep_analysis(current_user.get("id"), symbol)

    data_status = {
        "stock_found": bool(stock),
        "quote_found": bool(quote),
        "score_found": bool(score),
        "deep_analysis_found": bool(deep_analysis),
        "quote_date": (quote or {}).get("trade_date") or (quote or {}).get("datetime") or (quote or {}).get("trade_time"),
        "score_date": (score or {}).get("score_date"),
        "analysis_created_at": (deep_analysis or {}).get("created_at"),
        "sections": {
            "quote": _quote_status(quote),
            "scores": _score_status(score),
            "financials": _section_status(
                found=False,
                expected_freshness="进入财务业绩页签后按需加载",
                source="quant_data.financial_* + quant_data.earnings_*",
            ),
            "ai": _ai_status(deep_analysis),
            "trading": _section_status(
                found=False,
                expected_freshness="进入 AI 与交易页签后按需加载",
                source="finance.user_watchlists/watchlist_strategies/trader_positions/trade_signals",
            ),
        },
    }

    return {
        "success": True,
        "data": _json_safe(
            {
                "symbol": symbol,
                "stock": stock,
                "quote": quote,
                "score": score,
                "deep_analysis": deep_analysis,
                "data_status": data_status,
            }
        ),
    }


@router.get("/{symbol}/workbench/quote")
def get_stock_workbench_quote(
    symbol: str,
    current_user: Dict[str, Any] = Depends(get_current_active_user),
):
    """Market quote section for the single-stock research workbench."""
    return {
        "success": True,
        "data": _json_safe(_quote_section(symbol)),
    }


@router.get("/{symbol}/workbench/scores")
def get_stock_workbench_scores(
    symbol: str,
    current_user: Dict[str, Any] = Depends(get_current_active_user),
):
    """Quant score section for the single-stock research workbench."""
    score = _latest_score(symbol)
    history = _score_history(symbol)
    return {
        "success": True,
        "data": _json_safe(
            {
                "symbol": symbol,
                "score": score,
                "score_history": history,
                "data_status": _score_status(score),
            }
        ),
    }


@router.get("/{symbol}/workbench/financials")
def get_stock_workbench_financials(
    symbol: str,
    current_user: Dict[str, Any] = Depends(get_current_active_user),
):
    """Financial performance section for the single-stock research workbench."""
    return {
        "success": True,
        "data": _json_safe(_financials_section(symbol)),
    }


@router.get("/{symbol}/workbench/ai")
def get_stock_workbench_ai(
    symbol: str,
    current_user: Dict[str, Any] = Depends(get_current_active_user),
):
    """AI analysis section for the single-stock research workbench."""
    history = _analysis_history(current_user.get("id"), symbol)
    analysis_rows = history["rows"]
    deep_analysis = analysis_rows[0] if analysis_rows else None
    evaluation_summary = _analysis_evaluation_summary(history)
    status = _ai_status(deep_analysis)
    if evaluation_summary["data_status"]["found"]:
        status["found"] = True
        status["as_of"] = status["as_of"] or evaluation_summary["data_status"]["as_of"]
    return {
        "success": True,
        "data": _json_safe(
            {
                "symbol": symbol,
                "deep_analysis": deep_analysis,
                "analysis_history": analysis_rows,
                "analysis_history_total": history["total"],
                "evaluation_summary": evaluation_summary,
                "data_status": status,
            }
        ),
    }


@router.get("/{symbol}/workbench/trading")
def get_stock_workbench_trading(
    symbol: str,
    current_user: Dict[str, Any] = Depends(get_current_active_user),
):
    """User trading context section for the single-stock research workbench."""
    return {
        "success": True,
        "data": _json_safe(_trading_section(current_user.get("id"), symbol)),
    }
