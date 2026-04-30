"""与 GET /api/analysis-history 共享：当前用户自选股深度分析等写入 analysis_history 的记录。"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from db import get_analyzer_db


def _symbol_subquery(symbol: str) -> Dict[str, Any]:
    """支持 600519 与 600519.SH 等形式与库内字段对齐。"""
    sym = symbol.strip().upper()
    if re.fullmatch(r"\d{6}", sym):
        return {
            "symbol": {
                "$in": [sym, f"{sym}.SH", f"{sym}.SZ", f"{sym}.BJ"],
            }
        }
    return {"symbol": sym}


def build_user_analysis_history_payload(
    user_id: Any,
    *,
    symbol: Optional[str] = None,
    page: int = 1,
    limit: int = 50,
) -> Dict[str, Any]:
    """
    返回与 /api/analysis-history 相同结构的 JSON（供路由与 Hermes BFF 注入共用）。
    """
    analysis_col = get_analyzer_db()["analysis_history"]

    query: Dict[str, Any] = {"user_id": user_id}
    if symbol:
        query.update(_symbol_subquery(symbol))

    try:
        page = max(1, int(page))
    except Exception:
        page = 1
    try:
        limit = max(1, min(1000, int(limit)))
    except Exception:
        limit = 50

    skip = (page - 1) * limit

    total = analysis_col.count_documents(query)
    history = list(analysis_col.find(query).sort("timestamp", -1).skip(skip).limit(limit))

    normalized: List[Dict[str, Any]] = []
    for h in history:
        h_id = str(h.get("_id")) if h.get("_id") else ""
        analysis_result = h.get("analysis_result") if isinstance(h.get("analysis_result"), dict) else None
        analysis_obj = analysis_result.get("analysis") if isinstance(analysis_result, dict) else None

        if analysis_obj is None and isinstance(h.get("analysis"), dict):
            analysis_obj = h.get("analysis")
        if isinstance(analysis_obj, dict) and "analysis" in analysis_obj:
            core = analysis_obj.get("analysis")
            known_keys = {
                "technical_analysis",
                "investment_advice",
                "risk_level",
                "support_level",
                "resistance_level",
                "risk_price_levels",
                "long_term_forecast",
                "mid_term_forecast",
                "short_term_forecast",
                "key_points",
                "confidence_score",
                "stock_name",
                "symbol",
            }
            if not (known_keys & set(analysis_obj.keys())) and core is not None:
                analysis_obj = core

        symbol_val = h.get("symbol") or h.get("stock_code")
        if not symbol_val and isinstance(analysis_obj, dict):
            symbol_val = analysis_obj.get("symbol") or analysis_obj.get("stock_code")

        stock_name = h.get("stock_name")
        if not stock_name and isinstance(analysis_obj, dict):
            stock_name = analysis_obj.get("stock_name")

        created_at = h.get("created_at") or h.get("timestamp") or None

        normalized.append(
            {
                "id": h_id,
                "stock_name": stock_name,
                "symbol": symbol_val,
                "analysis": analysis_obj,
                "analysis_result": analysis_result,
                "provider": h.get("provider")
                or (analysis_result.get("provider") if analysis_result else None),
                "model": h.get("model")
                or (analysis_result.get("model") if analysis_result else None),
                "created_at": created_at,
            }
        )

    return {"success": True, "data": normalized, "page": page, "limit": limit, "total": total}
