from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Query, HTTPException
from db import get_db_collections
from utils import serialize_doc
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