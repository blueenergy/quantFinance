from typing import List, Optional, Any
from datetime import date, datetime
from bson import ObjectId
from fastapi import APIRouter, Query, HTTPException, Depends
from pydantic import BaseModel
from db import get_db_collections
from utils.stock_utils import serialize_doc
from auth import get_current_active_user
router = APIRouter()

collections = get_db_collections()
collection = collections["volume_price"] # type: ignore
class RecordResponseModel(BaseModel):
    symbol: Optional[str]
    trade_date: Optional[Any]
    name: Optional[str] = None
    open: Optional[float] = None
    close: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    volume: Optional[float] = None
    composite_score: Optional[float] = None
    cycle_score: Optional[float] = None
    growth_score: Optional[float] = None
    fundamental_score: Optional[float] = None
    value_score: Optional[float] = None
    technical_score: Optional[float] = None
    money_flow_score: Optional[float] = None
    score_date: Optional[str] = None

@router.get("/records/", response_model=List[RecordResponseModel])
def get_records(
    symbol: Optional[str] = Query(None), 
    limit: int = 10, 
    sort: Optional[str] = None,
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None)
):
    query = {}
    if symbol:
        query["symbol"] = symbol
    
    # 构建日期范围查询
    date_query = {}
    if start_date:
        s_date = start_date.replace('-', '')
        date_query["$gte"] = s_date
    if end_date:
        e_date = end_date.replace('-', '')
        date_query["$lte"] = e_date
    
    # 在初始 match 阶段尝试过滤（针对字符串类型的 trade_date）
    if date_query:
        query["trade_date"] = date_query

    # 构建聚合管道
    pipeline = [
        {"$match": query},
        {"$addFields": {
            "trade_date_str": {
                "$cond": [
                    {"$eq": [{"$type": "$trade_date"}, "date"]},
                    {"$dateToString": {"format": "%Y%m%d", "date": "$trade_date"}},
                    "$trade_date"
                ]
            }
        }}
    ]

    # 如果存在过滤需求，在标准化日期后再次过滤，以防 trade_date 是日期对象
    if date_query:
        match_stage = {}
        if date_query.get("$gte"):
            match_stage["$gte"] = date_query["$gte"]
        if date_query.get("$lte"):
            match_stage["$lte"] = date_query["$lte"]
        if match_stage:
            pipeline.append({"$match": {"trade_date_str": match_stage}})
    if sort:
        # 支持 '-trade_date' 降序
        field = sort.lstrip('-')
        direction = -1 if sort.startswith('-') else 1
        pipeline.append({"$sort": {field: direction}})
    pipeline.append({"$limit": limit})
    # 左连接 stock_scores，按 symbol+trade_date_str 对 score_date
    pipeline.append({
        "$lookup": {
            "from": "stock_scores",
            "let": {"symbol": "$symbol", "trade_date_str": "$trade_date_str"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$symbol", "$$symbol"]},
                    {"$eq": ["$score_date", "$$trade_date_str"]}
                ]}}}
            ],
            "as": "score_docs"
        }
    })
    pipeline.append({"$addFields": {
        "score_doc": {"$arrayElemAt": ["$score_docs", 0]}
    }})
    # 合并评分字段到主文档
    pipeline.append({"$addFields": {
        "composite_score": {"$ifNull": ["$score_doc.composite_score", None]},
        "cycle_score": {"$ifNull": ["$score_doc.cycle_score", None]},
        "growth_score": {"$ifNull": ["$score_doc.growth_score", None]},
        "fundamental_score": {"$ifNull": ["$score_doc.fundamental_score", None]},
        "value_score": {"$ifNull": ["$score_doc.value_score", None]},
        "technical_score": {"$ifNull": ["$score_doc.technical_score", None]},
        "money_flow_score": {"$ifNull": ["$score_doc.money_flow_score", None]},
        "score_date": {"$ifNull": ["$score_doc.score_date", None]}
    }})
    # composite_score 兼容 dict，始终返回 float（如 balanced）
    pipeline.append({
        "$addFields": {
            "composite_score": {
                "$cond": [
                    {"$eq": [ {"$type": "$composite_score"}, "object"]},
                    "$composite_score.balanced",
                    "$composite_score"
                ]
            }
        }
    })
    
    # 添加 stock_info 左连接来获取股票名称
    pipeline.append({
        "$lookup": {
            "from": "stock_info",
            "localField": "symbol",
            "foreignField": "symbol",
            "as": "stock_info_docs"
        }
    })
    pipeline.append({"$addFields": {
        "stock_info": {"$arrayElemAt": ["$stock_info_docs", 0]}
    }})
    pipeline.append({"$addFields": {
        "name": {"$ifNull": ["$stock_info.name", ""]}
    }})
    
    # 最后的 $project 保留所需字段
    pipeline.append({"$project": {
        "symbol": 1,
        "trade_date": 1,
        "open": 1,
        "close": 1,
        "high": 1,
        "low": 1,
        "volume": 1,
        "name": 1,
        "composite_score": 1,
        "cycle_score": 1,
        "fundamental_score": 1,
        "growth_score": 1,
        "technical_score": 1,
        "money_flow_score": 1,
        "score_date": 1
    }})
    
    docs = list(collection.aggregate(pipeline))
    return [serialize_doc(doc) for doc in docs]

@router.get("/record/{record_id}")
def get_record(record_id: str):
    doc = collection.find_one({"_id": ObjectId(record_id)})
    if doc:
        return serialize_doc(doc)
    return {"error": "Not found"}

@router.get("/money-flow-records")
def get_money_flow_records(
    symbol: str, 
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    current_user: dict = Depends(get_current_active_user)
):
    """
    获取某只股票的资金流数据，按 trade_date 升序排列，支持日期范围。
    """
    collections = get_db_collections()
    money_flow_col = collections["money_flow"]
    
    query = {"symbol": symbol}
    date_query = {}
    if start_date:
        date_query["$gte"] = start_date.replace('-', '')
    if end_date:
        date_query["$lte"] = end_date.replace('-', '')
    
    if date_query:
        query["trade_date"] = date_query

    # 查询资金流数据
    records = list(money_flow_col.find(query).sort("trade_date", 1))
    
    # 格式化 trade_date 为字符串
    for r in records:
        if isinstance(r["trade_date"], (datetime, date)):
            r["trade_date"] = r["trade_date"].strftime("%Y%m%d")
        r["_id"] = str(r["_id"])
    return {"success": True, "data": records}