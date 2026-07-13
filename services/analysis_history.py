"""与 GET /api/analysis-history 共享：当前用户自选股深度分析等写入 analysis_history 的记录。"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from bson import ObjectId

from db import get_analyzer_db

# 列表接口仅返回摘要字段，避免把 module_reports / 财务表等大对象打进首屏响应。
_LIST_ANALYSIS_SUMMARY_KEYS = (
    "investment_advice",
    "risk_level",
    "technical_analysis",
    "final_conclusion",
    "long_term_forecast",
    "mid_term_forecast",
    "short_term_forecast",
    "stock_name",
    "symbol",
    "stock_code",
    "analysis_mode",
    "confidence_score",
)

_LIST_PROJECTION: Dict[str, int] = {
    "_id": 1,
    "symbol": 1,
    "stock_code": 1,
    "stock_name": 1,
    "timestamp": 1,
    "created_at": 1,
    "analysis_mode": 1,
    "provider": 1,
    "model": 1,
    "user_id": 1,
}
for _key in _LIST_ANALYSIS_SUMMARY_KEYS:
    _LIST_PROJECTION[f"analysis.{_key}"] = 1
    _LIST_PROJECTION[f"analysis_result.analysis.{_key}"] = 1
    _LIST_PROJECTION[f"analysis_result.analysis_mode"] = 1
    _LIST_PROJECTION[f"analysis_result.provider"] = 1
    _LIST_PROJECTION[f"analysis_result.model"] = 1

_TECH_PREVIEW_CHARS = 200


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


def _unwrap_analysis_obj(raw: Any, analysis_result: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    analysis_obj = analysis_result.get("analysis") if isinstance(analysis_result, dict) else None
    if analysis_obj is None and isinstance(raw, dict):
        analysis_obj = raw.get("analysis")
    if not isinstance(analysis_obj, dict):
        return None
    if "analysis" not in analysis_obj:
        return analysis_obj

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
        "deep_review_pipeline",
        "revision_log",
        "thought_process_raw",
        "quant_score_snapshot_tag",
        "quant_score_cross_check",
        "chip_analysis",
        "institutional_deep_meta",
        "institutional_deep_user_visible",
    }
    if not (known_keys & set(analysis_obj.keys())) and core is not None:
        return core if isinstance(core, dict) else analysis_obj
    return analysis_obj


def _trim_analysis_summary(analysis_obj: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(analysis_obj, dict):
        return None
    summary = {k: analysis_obj.get(k) for k in _LIST_ANALYSIS_SUMMARY_KEYS if k in analysis_obj}
    tech = summary.get("technical_analysis")
    if isinstance(tech, str) and len(tech) > _TECH_PREVIEW_CHARS:
        summary["technical_analysis"] = tech[:_TECH_PREVIEW_CHARS]
    return summary or None


def _normalize_history_doc(h: Dict[str, Any], *, include_full: bool) -> Dict[str, Any]:
    h_id = str(h.get("_id")) if h.get("_id") else ""
    analysis_result = h.get("analysis_result") if isinstance(h.get("analysis_result"), dict) else None
    analysis_obj = _unwrap_analysis_obj(h, analysis_result)

    symbol_val = h.get("symbol") or h.get("stock_code")
    if not symbol_val and isinstance(analysis_obj, dict):
        symbol_val = analysis_obj.get("symbol") or analysis_obj.get("stock_code")

    stock_name = h.get("stock_name")
    if not stock_name and isinstance(analysis_obj, dict):
        stock_name = analysis_obj.get("stock_name")

    created_at = h.get("created_at") or h.get("timestamp") or None
    analysis_mode = (
        h.get("analysis_mode")
        or (analysis_result.get("analysis_mode") if analysis_result else None)
        or (analysis_obj.get("analysis_mode") if isinstance(analysis_obj, dict) else None)
        or "classic"
    )

    if include_full:
        normalized_analysis = analysis_obj
        normalized_result = analysis_result
    else:
        normalized_analysis = _trim_analysis_summary(analysis_obj)
        normalized_result = None

    return {
        "id": h_id,
        "stock_name": stock_name,
        "symbol": symbol_val,
        "analysis_mode": analysis_mode,
        "analysis": normalized_analysis,
        "analysis_result": normalized_result,
        "provider": h.get("provider") or (analysis_result.get("provider") if analysis_result else None),
        "model": h.get("model") or (analysis_result.get("model") if analysis_result else None),
        "created_at": created_at,
    }


def build_user_analysis_history_payload(
    user_id: Any,
    *,
    symbol: Optional[str] = None,
    page: int = 1,
    limit: int = 50,
    include_full: bool = False,
) -> Dict[str, Any]:
    """
    返回与 /api/analysis-history 相同结构的 JSON（供路由与 Hermes BFF 注入共用）。
    默认列表模式仅含分析摘要；include_full=True 时返回完整正文（Hermes / 工作台最新分析）。
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
    projection = None if include_full else _LIST_PROJECTION

    total = analysis_col.count_documents(query)
    cursor = analysis_col.find(query, projection).sort("timestamp", -1).skip(skip).limit(limit)
    normalized = [_normalize_history_doc(h, include_full=include_full) for h in cursor]

    return {
        "success": True,
        "data": normalized,
        "page": page,
        "limit": limit,
        "total": total,
        "has_more": (skip + len(normalized)) < total,
    }


def get_user_analysis_history_detail(user_id: Any, history_id: str) -> Optional[Dict[str, Any]]:
    """按 ID 返回单条完整分析记录；不存在或无权访问时返回 None。"""
    try:
        oid = ObjectId(str(history_id).strip())
    except Exception:
        return None

    doc = get_analyzer_db()["analysis_history"].find_one({"_id": oid, "user_id": user_id})
    if not doc:
        return None
    return {"success": True, "data": _normalize_history_doc(doc, include_full=True)}
