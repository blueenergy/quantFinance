from typing import List, Optional
from datetime import date, datetime
from bson import ObjectId
from fastapi import APIRouter, Query, HTTPException, Depends
from db import get_db_collections
from utils import serialize_doc
from auth import get_current_active_user
router = APIRouter()

collections = get_db_collections()
collection = collections["volume_price"] # type: ignore

@router.get("/records/", response_model=List[dict])
def get_records(symbol: Optional[str] = Query(None), limit: int = 10, sort: Optional[str] = None):
    query = {}
    if symbol:
        query["symbol"] = symbol
    cursor = collection.find(query)
    if sort:
        # 支持 '-trade_date' 降序
        field = sort.lstrip('-')
        direction = -1 if sort.startswith('-') else 1
        cursor = cursor.sort(field, direction)
    docs = cursor.limit(limit)
    return [serialize_doc(doc) for doc in docs]

@router.get("/record/{record_id}")
def get_record(record_id: str):
    doc = collection.find_one({"_id": ObjectId(record_id)})
    if doc:
        return serialize_doc(doc)
    return {"error": "Not found"}

@router.get("/money-flow-records")
def get_money_flow_records(symbol: str, current_user: dict = Depends(get_current_active_user)):
    """
    获取某只股票的资金流数据，按 trade_date 升序排列
    """
    collections = get_db_collections()
    money_flow_col = collections["money_flow"]
    # 查询所有资金流数据
    records = list(money_flow_col.find(
        {"symbol": symbol}
    ).sort("trade_date", 1))
    # 格式化 trade_date 为字符串（如有需要）
    for r in records:
        if isinstance(r["trade_date"], (datetime, date)):
            r["trade_date"] = r["trade_date"].strftime("%Y%m%d")
        r["_id"] = str(r["_id"])
    return {"success": True, "data": records}