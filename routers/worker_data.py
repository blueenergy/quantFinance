"""Worker Data Access API

为远程 Worker 提供数据访问接口，代理 MongoDB 操作。
Worker 不直接连接数据库，所有数据操作通过此 API 完成。
"""

from fastapi import APIRouter, HTTPException, Header
from typing import Optional, List, Dict, Any
from pydantic import BaseModel
from db import get_db_collections
from datetime import datetime

router = APIRouter()


# ==================== Request/Response Models ====================

class MinuteBarsRequest(BaseModel):
    """查询分钟 K 线数据请求"""
    symbol: str
    start_date: Optional[str] = None  # YYYYMMDD or YYYYMMDDHHmm
    end_date: Optional[str] = None
    limit: Optional[int] = 1000


class RuntimeStatusRequest(BaseModel):
    """上报运行状态请求"""
    symbol: str
    status: str  # starting, running, idle, error, stopped
    fields: Optional[Dict[str, Any]] = None


# ==================== 数据查询 API ====================

@router.post("/minute-bars")
def get_minute_bars(req: MinuteBarsRequest, x_node_id: Optional[str] = Header(None)):
    """获取分钟 K 线数据
    
    供 Worker 查询历史或实时分钟K线数据
    """
    try:
        collections = get_db_collections()
        minute_bars = collections["minute_bars"]
        
        # 构建查询条件
        query = {"symbol": req.symbol}
        
        if req.start_date:
            query["trade_date"] = {"$gte": req.start_date}
        
        if req.end_date:
            if "trade_date" in query:
                query["trade_date"]["$lte"] = req.end_date
            else:
                query["trade_date"] = {"$lte": req.end_date}
        
        # 查询数据
        cursor = minute_bars.find(query).sort("trade_date", 1)
        
        if req.limit:
            cursor = cursor.limit(req.limit)
        
        bars = []
        for doc in cursor:
            # 移除 _id，转换为 Worker 可用格式
            doc.pop("_id", None)
            bars.append(doc)
        
        return {
            "success": True,
            "data": {
                "bars": bars,
                "count": len(bars),
                "symbol": req.symbol,
            }
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询分钟K线失败: {str(e)}")


@router.get("/volume-price")
def get_volume_price(
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 1000,
    x_node_id: Optional[str] = Header(None)
):
    """获取日线量价数据
    
    供 Worker 查询历史日线数据（回测用）
    """
    try:
        collections = get_db_collections()
        volume_price = collections["volume_price"]
        
        # 构建查询条件
        query = {"symbol": symbol}
        
        if start_date:
            query["trade_date"] = {"$gte": start_date}
        
        if end_date:
            if "trade_date" in query:
                query["trade_date"]["$lte"] = end_date
            else:
                query["trade_date"] = {"$lte": end_date}
        
        # 查询数据
        cursor = volume_price.find(query).sort("trade_date", 1).limit(limit)
        
        bars = []
        for doc in cursor:
            doc.pop("_id", None)
            bars.append(doc)
        
        return {
            "success": True,
            "data": {
                "bars": bars,
                "count": len(bars),
                "symbol": symbol,
            }
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询日线数据失败: {str(e)}")


# ==================== 状态上报 API ====================

@router.post("/runtime-status")
def upsert_runtime_status(req: RuntimeStatusRequest, x_node_id: Optional[str] = Header(None)):
    """上报 Worker 运行状态
    
    替代原来的 shared.db.upsert_runtime_status
    """
    try:
        collections = get_db_collections()
        runtime_status = collections["runtime_status"]
        
        # 构建更新文档
        update_doc = {
            "symbol": req.symbol,
            "status": req.status,
            "updated_at": datetime.utcnow(),
        }
        
        # 合并额外字段
        if req.fields:
            update_doc.update(req.fields)
        
        # 添加来源信息
        if x_node_id:
            update_doc["node_id"] = x_node_id
        
        # Upsert
        runtime_status.update_one(
            {"symbol": req.symbol},
            {"$set": update_doc},
            upsert=True
        )
        
        return {
            "success": True,
            "data": {
                "symbol": req.symbol,
                "status": req.status,
            }
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"上报状态失败: {str(e)}")


@router.get("/runtime-status/{symbol}")
def get_runtime_status(symbol: str, x_node_id: Optional[str] = Header(None)):
    """查询指定股票的运行状态"""
    try:
        collections = get_db_collections()
        runtime_status = collections["runtime_status"]
        
        doc = runtime_status.find_one({"symbol": symbol})
        
        if not doc:
            return {
                "success": True,
                "data": None
            }
        
        doc.pop("_id", None)
        
        return {
            "success": True,
            "data": doc
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询状态失败: {str(e)}")
