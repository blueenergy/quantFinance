from calendar import monthrange
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query

from db import get_data_db, get_db_collections
from stock_data_access.prices import AdjustedPriceDataAccess


router = APIRouter(tags=["熵值因子排名"])


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _build_info_map(symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    if not symbols:
        return {}
    stock_info = get_db_collections()["stock_info"]
    docs = stock_info.find(
        {"symbol": {"$in": symbols}},
        {"_id": 0, "symbol": 1, "ts_code": 1, "name": 1, "industry": 1},
    )
    out: Dict[str, Dict[str, Any]] = {}
    for doc in docs:
        for key in (doc.get("symbol"), doc.get("ts_code")):
            if key:
                out[str(key)] = doc
    return out


def _symbol_variants(symbol: str) -> List[str]:
    s = (symbol or "").strip()
    if not s:
        return []
    out: List[str] = []
    seen = set()

    def add(value: str) -> None:
        if value and value not in seen:
            seen.add(value)
            out.append(value)

    add(s)
    if "." in s:
        add(s.split(".", 1)[0])
    else:
        for suffix in (".SH", ".SZ", ".BJ"):
            add(f"{s}{suffix}")
    return out


def _close_from_price_doc(doc: Optional[Dict[str, Any]]) -> Optional[float]:
    if not doc:
        return None
    for field in ("close", "close_price", "adj_close"):
        value = _safe_float(doc.get(field))
        if value is not None and value > 0:
            return value
    return None


def _normalize_date(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "strftime"):
        return value.strftime("%Y%m%d")
    text = str(value).replace("-", "")[:8]
    return text if len(text) == 8 else None


def _add_months(date_str: str, months: int) -> Optional[str]:
    text = (date_str or "").replace("-", "")[:8]
    if len(text) != 8:
        return None
    try:
        year, month, day = int(text[:4]), int(text[4:6]), int(text[6:8])
    except ValueError:
        return None
    month += months
    while month > 12:
        year += 1
        month -= 12
    day = min(day, monthrange(year, month)[1])
    return f"{year:04d}{month:02d}{day:02d}"


def _find_price_doc(volume_price: Any, symbol: str, date_str: str, *, direction: str) -> Optional[Dict[str, Any]]:
    variants = _symbol_variants(symbol)
    if not variants or not date_str:
        return None
    if direction == "forward":
        query = {"symbol": {"$in": variants}, "trade_date": {"$gte": date_str}}
        sort = [("trade_date", 1)]
    else:
        query = {"symbol": {"$in": variants}, "trade_date": {"$lte": date_str}}
        sort = [("trade_date", -1)]
    return volume_price.find_one(query, sort=sort)


def _hfq_return_pct(
    base_doc: Optional[Dict[str, Any]],
    future_doc: Optional[Dict[str, Any]],
    symbol: str,
    adj_dao: AdjustedPriceDataAccess,
) -> Optional[float]:
    base_close = _close_from_price_doc(base_doc)
    future_close = _close_from_price_doc(future_doc)
    if base_close is None or future_close is None:
        return None
    base_td = _normalize_date(base_doc.get("trade_date") if base_doc else None)
    future_td = _normalize_date(future_doc.get("trade_date") if future_doc else None)
    base_af = adj_dao.factor_for(symbol, base_td or "", variants=_symbol_variants(symbol)) if base_td else None
    future_af = adj_dao.factor_for(symbol, future_td or "", variants=_symbol_variants(symbol)) if future_td else None
    # 任一端缺因子就整体退回裸价，避免「后复权 ÷ 裸价」跨口径相除。
    if base_af is None or future_af is None:
        base_af = future_af = 1.0
    base_adj = base_close * base_af
    future_adj = future_close * future_af
    if base_adj <= 0:
        return None
    return round((future_adj / base_adj - 1.0) * 100.0, 2)


def _future_returns(symbol: str, score_date: Optional[str]) -> Dict[str, Any]:
    empty = {
        "future_return_1m_pct": None,
        "future_return_2m_pct": None,
        "future_return_3m_pct": None,
        "future_return_base_date": None,
        "future_return_1m_date": None,
        "future_return_2m_date": None,
        "future_return_3m_date": None,
    }
    if not score_date:
        return empty

    volume_price = get_data_db()["volume_price"]
    adj_dao = AdjustedPriceDataAccess(db=get_data_db())
    base_doc = _find_price_doc(volume_price, symbol, str(score_date), direction="backward")
    base_close = _close_from_price_doc(base_doc)
    if base_close is None:
        return empty

    out = dict(empty)
    out["future_return_base_date"] = _normalize_date(base_doc.get("trade_date"))
    for months in (1, 2, 3):
        target = _add_months(str(score_date), months)
        future_doc = _find_price_doc(volume_price, symbol, target or "", direction="forward")
        date_key = f"future_return_{months}m_date"
        pct_key = f"future_return_{months}m_pct"
        out[date_key] = _normalize_date(future_doc.get("trade_date")) if future_doc else None
        pct = _hfq_return_pct(base_doc, future_doc, symbol, adj_dao)
        if pct is not None:
            out[pct_key] = pct
    return out


def _serialize_score(doc: Dict[str, Any], info_map: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    symbol = str(doc.get("symbol") or doc.get("ts_code") or "")
    info = info_map.get(symbol, {})
    row = {
        "symbol": symbol,
        "ts_code": doc.get("ts_code") or symbol,
        "name": info.get("name") or doc.get("name") or "",
        "industry": doc.get("industry") or info.get("industry") or "",
        "score_date": doc.get("score_date"),
        "entropy_score": _safe_float(doc.get("entropy_score")),
        "entropy_score_z": _safe_float(doc.get("entropy_score_z")),
        "final_entropy_score": _safe_float(doc.get("final_entropy_score")),
        "final_entropy_score_z": _safe_float(doc.get("final_entropy_score_z")),
        "not_lifted_gate": _safe_float(doc.get("not_lifted_gate")),
        "winner_rate": _safe_float(doc.get("winner_rate")),
        "factors_used": _safe_float(doc.get("factors_used")),
        "z_s_chip_slope": _safe_float(doc.get("z_s_chip_slope")),
        "z_chip_concentration": _safe_float(doc.get("z_chip_concentration")),
        "z_holder_change": _safe_float(doc.get("z_holder_change")),
        "z_energy_vr": _safe_float(doc.get("z_energy_vr")),
        "z_s_vp_slope": _safe_float(doc.get("z_s_vp_slope")),
        "z_int_signal": _safe_float(doc.get("z_int_signal")),
        "z_int_momentum": _safe_float(doc.get("z_int_momentum")),
        "z_int_disorder": _safe_float(doc.get("z_int_disorder")),
        "synced_at": doc.get("synced_at"),
    }
    row.update(_future_returns(symbol, doc.get("score_date")))
    return row


def _entropy_sort_key(doc: Dict[str, Any]) -> tuple:
    # 熵值排名方向：越小越好；缺失值放到最后。
    value = _safe_float(doc.get("final_entropy_score"))
    if value is None:
        value = _safe_float(doc.get("entropy_score"))
    return (value is None, value if value is not None else 0.0, str(doc.get("symbol") or ""))


_MISSING_ENTROPY_SENTINEL = 1e18


def _rank_entropy_docs(coll, query: Dict[str, Any], limit: int) -> List[Dict[str, Any]]:
    """在 MongoDB 内按熵值升序排序并截断，缺失分数排末尾。"""
    pipeline = [
        {"$match": query},
        {
            "$addFields": {
                "_rank_entropy": {
                    "$ifNull": [
                        "$final_entropy_score",
                        {"$ifNull": ["$entropy_score", _MISSING_ENTROPY_SENTINEL]},
                    ]
                }
            }
        },
        {"$sort": {"_rank_entropy": 1, "symbol": 1}},
        {"$limit": int(limit)},
        {"$project": {"_id": 0, "_rank_entropy": 0}},
    ]
    return list(coll.aggregate(pipeline))


@router.get("/entropy/dates")
def get_entropy_dates(limit: int = Query(60, ge=1, le=500)):
    coll = get_db_collections()["entropy_scores"]
    dates = sorted([str(d) for d in coll.distinct("score_date") if d], reverse=True)
    return {"success": True, "data": dates[:limit]}


@router.get("/entropy/rankings")
def get_entropy_rankings(
    score_date: Optional[str] = None,
    index_code: str = Query("csi1000", description="指数池: hs300/a500/csi500/csi1000/csi2000/star50"),
    limit: int = Query(100, ge=1, le=1000),
):
    coll = get_db_collections()["entropy_scores"]
    if not score_date:
        latest = coll.find_one({}, sort=[("score_date", -1)])
        score_date = latest.get("score_date") if latest else None
    if not score_date:
        return {"success": True, "data": {"score_date": None, "rows": []}}

    query: Dict[str, Any] = {"score_date": score_date}
    if index_code:
        query["index_codes"] = index_code
    docs = _rank_entropy_docs(coll, query, limit)
    info_map = _build_info_map([str(d.get("symbol") or "") for d in docs])
    rows = [_serialize_score(doc, info_map) for doc in docs]
    return {"success": True, "data": {"score_date": score_date, "rows": rows}}


@router.get("/entropy/history/{symbol}")
def get_symbol_entropy_history(symbol: str, limit: int = Query(120, ge=1, le=500)):
    coll = get_db_collections()["entropy_scores"]
    docs = list(
        coll.find({"symbol": symbol}, {"_id": 0})
        .sort("score_date", 1)
        .limit(limit)
    )
    info_map = _build_info_map([symbol])
    return {"success": True, "data": [_serialize_score(doc, info_map) for doc in docs]}
