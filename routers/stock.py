import math
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List

from bson import ObjectId
from fastapi import APIRouter, HTTPException, Query
from db import get_data_db
from utils.pinyin_utils import stock_name_pinyin_fields

router = APIRouter()

SEARCH_PROJECTION = {
    "_id": 1,
    "symbol": 1,
    "ts_code": 1,
    "name": 1,
    "industry": 1,
    "pinyin": 1,
    "pinyin_full": 1,
}


def _json_safe(value: Any) -> Any:
    """Convert Mongo/numpy-ish values into JSON-safe primitives."""
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if hasattr(value, "to_decimal"):
        try:
            return float(value.to_decimal())
        except Exception:
            return str(value)
    return value


def _symbol_candidates(symbol: str) -> List[str]:
    sym = symbol.strip().upper()
    candidates = [sym]
    if "." not in sym and len(sym) == 6 and sym.isdigit():
        candidates.extend([f"{sym}.SH", f"{sym}.SZ", f"{sym}.BJ"])
    elif "." not in sym and sym.isdigit() and 1 <= len(sym) <= 5:
        candidates.append(f"{sym.zfill(5)}.HK")
    elif "." in sym:
        candidates.append(sym.split(".")[0])
        prefix, _, suffix = sym.partition(".")
        if suffix == "HK" and prefix.isdigit() and len(prefix) <= 5:
            candidates.append(f"{prefix.zfill(5)}.HK")
    return list(dict.fromkeys(candidates))


def _dedupe_docs(docs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    result = []
    for doc in docs:
        key = str(doc.get("symbol") or doc.get("ts_code") or doc.get("_id") or "")
        if key in seen:
            continue
        seen.add(key)
        result.append(doc)
    return result


def _build_stock_search_query(q: str) -> Dict[str, Any]:
    """Build an index-friendly stock_info query for autocomplete."""
    q_esc = re.escape(q)
    if q.isdigit():
        return {"symbol": {"$regex": f"^{q_esc}"}}
    if re.fullmatch(r"[A-Za-z]+", q):
        needle = re.escape(q.upper())
        return {
            "$or": [
                {"symbol": {"$regex": f"^{needle}"}},
                {"pinyin": {"$regex": f"^{needle}"}},
                {"pinyin_full": {"$regex": f"^{needle}"}},
            ]
        }
    return {
        "$or": [
            {"symbol": {"$regex": q_esc, "$options": "i"}},
            {"name": {"$regex": q_esc, "$options": "i"}},
            {"pinyin": {"$regex": q_esc, "$options": "i"}},
        ]
    }


def _build_etf_search_query(q: str) -> Dict[str, Any]:
    q_esc = re.escape(q)
    if q.isdigit():
        return {"ts_code": {"$regex": f"^{q_esc}", "$options": "i"}}
    if re.fullmatch(r"[A-Za-z]+", q):
        needle = re.escape(q.upper())
        return {
            "$or": [
                {"ts_code": {"$regex": f"^{needle}"}},
                {"name": {"$regex": q_esc, "$options": "i"}},
            ]
        }
    return {
        "$or": [
            {"ts_code": {"$regex": q_esc, "$options": "i"}},
            {"name": {"$regex": q_esc, "$options": "i"}},
        ]
    }


def _ensure_display_pinyin(doc: Dict[str, Any]) -> Dict[str, Any]:
    if doc.get("pinyin"):
        return doc
    name = doc.get("name") or doc.get("stock_name")
    fields = stock_name_pinyin_fields(name)
    if fields.get("pinyin"):
        doc["pinyin"] = fields["pinyin"]
    return doc


def _build_hk_search_query(q: str) -> Dict[str, Any]:
    q_esc = re.escape(q)
    if q.isdigit() and 1 <= len(q) <= 5:
        padded = q.zfill(5)
        return {
            "$or": [
                {"symbol": {"$regex": f"^{re.escape(padded)}"}},
                {"ts_code": {"$regex": f"^{re.escape(padded)}"}},
                {"symbol": {"$regex": f"^{q_esc}"}},
                {"name": {"$regex": q_esc, "$options": "i"}},
            ]
        }
    return _build_stock_search_query(q)


def _ohlcv_collection_name(symbol: str) -> str:
    """Pick the Mongo bars collection for a symbol without mixing markets."""
    candidates = _symbol_candidates(symbol)
    joined = " ".join(candidates).upper()
    if ".HK" in joined:
        return "hk_volume_price"
    if any(("." not in c and any(ch.isalpha() for ch in c)) for c in candidates):
        return "us_volume_price"
    return "volume_price"


@router.get("/info/{symbol}")
def get_stock_info(symbol: str):
    """
    获取指定股票的基本信息
    """
    data_collections = get_data_db()
    candidates = _symbol_candidates(symbol)
    for coll_name in ("stock_info", "hk_stock_info", "us_stock_info"):
        col = data_collections.get(coll_name)
        if col is None:
            continue
        for cand in candidates:
            stock_info = col.find_one({"$or": [{"symbol": cand}, {"ts_code": cand}]})
            if stock_info:
                stock_info["_id"] = str(stock_info["_id"])
                stock_info.setdefault("market", {"hk_stock_info": "HK", "us_stock_info": "US"}.get(coll_name, "A"))
                return {"success": True, "data": stock_info}
    return {"success": False, "message": "未找到股票信息"}


@router.get("/search")
def search_stocks(q: str):
    """
    搜索股票，支持模糊匹配股票代码或名称
    """
    q = (q or "").strip()
    if not q:
        return {"success": True, "data": []}

    data_collections = get_data_db()
    stock_col = data_collections["stock_info"]
    etf_col = data_collections.get("etf_basic")
    hk_col = data_collections.get("hk_stock_info")
    us_col = data_collections.get("us_stock_info")

    stock_query = _build_stock_search_query(q)
    etf_query = _build_etf_search_query(q)
    hk_query = _build_hk_search_query(q)
    us_query = _build_stock_search_query(q)

    stocks = list(stock_col.find(stock_query, SEARCH_PROJECTION).limit(30))
    stocks = _dedupe_docs(stocks)
    etfs = list(etf_col.find(etf_query, {"_id": 1, "ts_code": 1, "name": 1}).limit(15)) if etf_col is not None else []
    hk_stocks = list(hk_col.find(hk_query, SEARCH_PROJECTION).limit(15)) if hk_col is not None else []
    us_stocks = list(us_col.find(us_query, SEARCH_PROJECTION).limit(15)) if us_col is not None else []

    def rank(doc: Dict[str, Any], *, is_etf: bool = False) -> tuple:
        symbol = str(doc.get("symbol") or doc.get("ts_code") or "").upper()
        name = str(doc.get("name") or "").upper()
        pinyin = str(doc.get("pinyin") or "").upper()
        needle = q.upper()
        if symbol.startswith(needle):
            bucket = 0
        elif pinyin.startswith(needle):
            bucket = 1
        elif name.startswith(needle):
            bucket = 2
        elif needle in symbol:
            bucket = 3
        elif needle in pinyin:
            bucket = 4
        elif needle in name:
            bucket = 5
        else:
            bucket = 9
        return (bucket, 1 if is_etf else 0, symbol)

    stocks = sorted(stocks, key=rank)[:25]
    etfs = sorted(etfs, key=lambda doc: rank(doc, is_etf=True))[:10]
    hk_stocks = sorted(_dedupe_docs(hk_stocks), key=rank)[:10]
    us_stocks = sorted(_dedupe_docs(us_stocks), key=rank)[:10]
    
    # 处理返回数据
    results = []
    for stock in stocks:
        stock["_id"] = str(stock["_id"])
        stock.setdefault("market", "A")
        results.append(_ensure_display_pinyin(stock))

    for stock in hk_stocks:
        stock["_id"] = str(stock["_id"])
        stock.setdefault("market", "HK")
        results.append(_ensure_display_pinyin(stock))

    for stock in us_stocks:
        stock["_id"] = str(stock["_id"])
        stock.setdefault("market", "US")
        results.append(_ensure_display_pinyin(stock))
        
    for etf in etfs:
        etf["_id"] = str(etf["_id"])
        etf["symbol"] = etf.get("ts_code") # Map ts_code to symbol for consistency
        etf["is_etf"] = True 
        
        # FastAPI's jsonable_encoder cannot handle NaN values
        import math
        for k, v in etf.items():
            if isinstance(v, float) and math.isnan(v):
                etf[k] = None
                
        results.append(etf)
    
    return {"success": True, "data": results}


@router.get("/{symbol}/financial")
def get_stock_financial(
    symbol: str,
    statement: str = Query(
        "indicator",
        pattern="^(income|balance|cashflow|indicator|daily_basic)$",
        description="财务报表类型",
    ),
    periods: int = Query(8, ge=1, le=40, description="返回最近多少期"),
):
    """获取单股财务数据，用于股票工作台的财务快照。"""
    data_collections = get_data_db()
    collection_map = {
        "income": "financial_income",
        "balance": "financial_balance",
        "cashflow": "financial_cashflow",
        "indicator": "financial_indicator",
        "daily_basic": "financial_daily_basic",
    }
    collection_name = collection_map[statement]
    collection = data_collections.get(collection_name)
    if collection is None:
        raise HTTPException(status_code=404, detail=f"未配置集合: {collection_name}")

    symbol_values = _symbol_candidates(symbol)
    query: Dict[str, Any] = {
        "$or": [
            {"symbol": {"$in": symbol_values}},
            {"ts_code": {"$in": symbol_values}},
        ]
    }
    sort_field = "trade_date" if statement == "daily_basic" else "end_date"
    docs = list(
        collection.find(query, {"_id": 0})
        .sort(sort_field, -1)
        .limit(int(periods))
    )

    return {
        "success": True,
        "data": _json_safe(docs),
        "meta": {
            "symbol": symbol,
            "statement": statement,
            "collection": collection_name,
            "periods": int(periods),
            "sort_field": sort_field,
            "count": len(docs),
        },
    }

