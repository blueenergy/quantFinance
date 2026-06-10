from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends

from auth import get_current_active_user
from db import get_data_db, get_db_collections
from routers.stock import _json_safe, _symbol_candidates
from services.analysis_history import build_user_analysis_history_payload

router = APIRouter()


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


def _latest_quote(symbol: str) -> Optional[Dict[str, Any]]:
    data = get_data_db()
    candidates = _symbol_candidates(symbol)
    quote = data["volume_price_realtime_latest"].find_one(
        {"symbol": {"$in": candidates}},
        sort=[("trade_time", -1), ("trade_date", -1), ("_id", -1)],
    )
    if quote:
        quote["source"] = "realtime"
        return quote

    quote = data["volume_price"].find_one(
        {"symbol": {"$in": candidates}},
        sort=[("trade_date", -1), ("_id", -1)],
    )
    if quote:
        quote["source"] = "daily"
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
