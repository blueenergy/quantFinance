from fastapi import APIRouter, Query, HTTPException
from typing import List, Optional
from db import db
import pandas as pd

router = APIRouter(prefix="/etf", tags=["etf"])

@router.get("/list")
async def get_etf_list(
    q: Optional[str] = Query(None, description="Search by symbol or name"),
    limit: int = 50
):
    """
    Get a list of ETFs, optionally filtered by search query.
    """
    query = {}
    if q:
        import re
        # Case insensitive regex search on symbol or name
        pattern = re.compile(q, re.IGNORECASE)
        query = {"$or": [{"ts_code": pattern}, {"name": pattern}]}
        
    cursor = db["etf_basic"].find(query, {"_id": 0}).limit(limit)
    results = list(cursor)
    
    import math
    import pandas as pd
    
    clean_results = []
    for row in results:
        clean_row = {}
        for k, v in row.items():
            if pd.isna(v) or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
                clean_row[k] = None
            else:
                if hasattr(v, 'item'):
                    v = v.item()
                clean_row[k] = v
        clean_results.append(clean_row)
    
    return {
        "success": True,
        "count": len(clean_results),
        "data": clean_results
    }

@router.get("/{symbol}/kline")
async def get_etf_kline(
    symbol: str, 
    start_date: Optional[str] = None, 
    end_date: Optional[str] = None,
    limit: int = 1000
):
    """
    Get ETF daily K-line data, merged with adjustment factors (for pre-adjusted prices) 
    and daily share scale.
    """
    # Base query for the specific ETF
    query = {"ts_code": symbol}
    
    # Add date filters if provided
    date_filter = {}
    if start_date:
        date_filter["$gte"] = start_date.replace('-', '')
    if end_date:
        date_filter["$lte"] = end_date.replace('-', '')
        
    if date_filter:
        query["trade_date"] = date_filter
        
    # Execute queries synchronously (pymongo client is sync)
    daily_results = list(db["etf_daily"].find(query, {"_id": 0}).sort("trade_date", -1).limit(limit))
    adj_results = list(db["etf_adj_factor"].find(query, {"_id": 0}).sort("trade_date", -1).limit(limit))
    share_results = list(db["etf_shares"].find(query, {"_id": 0}).sort("trade_date", -1).limit(limit))
    
    if not daily_results:
        return {"success": True, "symbol": symbol, "data": []}
        
    # Convert to pandas DataFrames for easy merging and calculation
    df_daily = pd.DataFrame(daily_results)
    
    # --- 1. Process Adjustment Factors (Pre-adjustment) ---
    if adj_results:
        df_adj = pd.DataFrame(adj_results)
        # Merge on trade_date
        df = pd.merge(df_daily, df_adj[['trade_date', 'adj_factor']], on='trade_date', how='left')
        
        # Forward fill adj_factors if some days are missing but earlier days exist
        df['adj_factor'] = df['adj_factor'].fillna(method='ffill').fillna(method='bfill').fillna(1.0)
        
        # Calculate pre-adjusted prices
        # Pre-adjusted price = Current Price * (Current Adj Factor / Latest Adj Factor)
        # Since we sorted by trade_date descending, the latest available is at index 0
        latest_adj_factor = df['adj_factor'].iloc[0]
        
        for col in ['open', 'high', 'low', 'close']:
            df[f'pre_adj_{col}'] = df[col] * (df['adj_factor'] / latest_adj_factor)
            # Round to 3 decimal places for ETFs
            df[f'pre_adj_{col}'] = df[f'pre_adj_{col}'].round(3)
    else:
        df = df_daily
        for col in ['open', 'high', 'low', 'close']:
            df[f'pre_adj_{col}'] = df[col]

    # --- 2. Process Share Scale ---
    if share_results:
        df_share = pd.DataFrame(share_results)
        # Tushare fund_share might not be updated daily for all ETFs, typically week/month ends for some, daily for others.
        # We merge and forward fill the share scale so user always sees the 'latest known' scale.
        df = pd.merge(df, df_share[['trade_date', 'fd_share']], on='trade_date', how='left')
        
        # Need to sort by date ascending to safely forward fill shares into the future
        df = df.sort_values('trade_date', ascending=True)
        df['fd_share'] = df['fd_share'].fillna(method='ffill')
        
        # Sort back to descending for API output
        df = df.sort_values('trade_date', ascending=False)
    else:
        df['fd_share'] = None
        
    
    import math
    merged_data = []
    for row in df.to_dict('records'):
        clean_row = {}
        for k, v in row.items():
            if pd.isna(v) or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
                clean_row[k] = None
            else:
                # specifically convert NumPy floats to regular Python floats
                if hasattr(v, 'item'):
                    v = v.item()
                clean_row[k] = v
        merged_data.append(clean_row)
    
    return {
        "success": True,
        "symbol": symbol,
        "count": len(merged_data),
        "data": merged_data
    }

from pydantic import BaseModel
from auth import get_current_active_user
from fastapi import Depends
from bson import ObjectId
from datetime import datetime

class AnalyzeRequest(BaseModel):
    symbol: str
    force: bool = False

@router.post("/analyze", summary="手动触发ETF智能分析")
async def submit_etf_analysis(
    request: AnalyzeRequest,
    current_user: dict = Depends(get_current_active_user),
):
    """Submit an ETF analysis task to the background worker (quantAnalyzer)."""
    symbol = request.symbol.upper()
    basic = db["etf_basic"].find_one({"ts_code": symbol})
    if not basic:
        raise HTTPException(status_code=404, detail=f"ETF {symbol} not found")

    # Check if a task is already running
    existing = db["analysis_tasks"].find_one({
        "type": "etf_analysis",
        "params.symbol": symbol,
        "status": {"$in": ["pending", "processing"]},
    })
    
    if existing:
        return {
            "success": True,
            "task_id": str(existing["_id"]),
            "message": "已有该ETF的分析任务正在处理中",
            "status": existing["status"],
        }

    task_doc = {
        "user_id": current_user.get("username", "unknown"),
        "type": "etf_analysis",
        "priority": 20,
        "status": "pending",
        "params": {
            "symbol": symbol,
            "force": request.force,
        },
        "created_at": datetime.now(),
        "updated_at": datetime.now(),
    }
    
    result = db["analysis_tasks"].insert_one(task_doc)

    return {
        "success": True,
        "task_id": str(result.inserted_id),
        "message": f"ETF分析任务已提交 ({symbol})",
        "status": "pending",
    }


@router.get("/task/{task_id}", summary="查询分析任务状态")
async def get_etf_task_status(
    task_id: str,
    current_user: dict = Depends(get_current_active_user),
):
    """Poll task status."""
    try:
        task = db["analysis_tasks"].find_one({"_id": ObjectId(task_id)})
    except Exception:
        raise HTTPException(400, "Invalid task_id")

    if not task:
        raise HTTPException(404, "Task not found")

    response = {
        "success": True,
        "task_id": task_id,
        "status": task.get("status"),
        "created_at": task.get("created_at").isoformat() if task.get("created_at") else None,
    }

    if task["status"] == "completed":
        result = task.get("result", {})
        response["analysis"] = result.get("analysis")
        response["symbol"] = result.get("ts_code")
        response["trade_date"] = result.get("trade_date")
        response["completed_at"] = (
            task.get("completed_at").isoformat() if task.get("completed_at") else None
        )
    elif task["status"] == "failed":
        response["error"] = task.get("error", "Unknown error")

    return response


@router.get("/{symbol}/analysis", summary="获取ETF最新智能分析结果")
async def get_etf_latest_analysis(
    symbol: str,
    current_user: dict = Depends(get_current_active_user),
):
    """Get the most recent cached analysis for an ETF."""
    symbol = symbol.upper()
    doc = db["etf_analysis"].find_one(
        {"ts_code": symbol}, 
        sort=[("created_at", -1)]
    )
    
    if not doc:
        return {
            "success": False,
            "error": f"暂无 {symbol} 的深度分析报告，请点击分析按钮生成。",
        }

    return {
        "success": True,
        "symbol": symbol,
        "trade_date": doc.get("trade_date"),
        "latest_kline_date": doc.get("latest_kline_date"),
        "analysis": doc.get("analysis"),
        "model": doc.get("model"),
        "created_at": doc.get("created_at").isoformat() if doc.get("created_at") else None,
    }
