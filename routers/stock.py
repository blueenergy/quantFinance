import math
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List

from bson import ObjectId
from fastapi import APIRouter, HTTPException, Query
from db import get_data_db

router = APIRouter()


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
    elif "." in sym:
        candidates.append(sym.split(".")[0])
    return list(dict.fromkeys(candidates))




@router.get("/info/{symbol}")
def get_stock_info(symbol: str):
    """
    获取指定股票的基本信息
    """
    data_collections = get_data_db()
    stock_col = data_collections["stock_info"]
    stock_info = stock_col.find_one({"symbol": symbol})

    if stock_info:
        stock_info["_id"] = str(stock_info["_id"])
        return {"success": True, "data": stock_info}
    return {"success": False, "message": "未找到股票信息"}


@router.get("/search")
def search_stocks(q: str):
    """
    搜索股票，支持模糊匹配股票代码或名称
    """
    data_collections = get_data_db()
    stock_col = data_collections["stock_info"]
    etf_col = data_collections.get("etf_basic")
    
    # 搜索匹配的股票，支持股票代码和名称的模糊匹配
    # 如果查询是数字开头，优先匹配股票代码
    if q.isdigit():
        # 对于数字查询，查找以该数字开头的股票代码
        stock_query = {"symbol": {"$regex": f"^{q}", "$options": "i"}}
        etf_query = {"ts_code": {"$regex": f"^{q}", "$options": "i"}}
    else:
        # 对于非数字查询，匹配股票代码、名称或拼音
        stock_query = {"$or": [
            {"symbol": {"$regex": q, "$options": "i"}},
            {"name": {"$regex": q, "$options": "i"}},
            {"pinyin": {"$regex": q, "$options": "i"}}
        ]}
        etf_query = {"$or": [
            {"ts_code": {"$regex": q, "$options": "i"}},
            {"name": {"$regex": q, "$options": "i"}}
        ]}
    
    stocks = list(stock_col.find(stock_query).limit(15))  # 限制返回数量
    etfs = list(etf_col.find(etf_query).limit(10)) if etf_col is not None else []
    
    # 处理返回数据
    results = []
    for stock in stocks:
        stock["_id"] = str(stock["_id"])
        results.append(stock)
        
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

