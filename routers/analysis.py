import os
from typing import Optional, Dict, Any
from fastapi import APIRouter, Depends, Query, HTTPException
from models import StockAnalysisRequest, AnalysisHistoryRequest
from auth import get_current_active_user
from db import get_db_collections
from llm_service import llm_manager
from datetime import datetime
from utils import serialize_doc

router = APIRouter()

@router.post("/analyze-stock")
async def analyze_stock(request: StockAnalysisRequest, current_user: dict = Depends(get_current_active_user)):
    """
    调用 LLM 服务进行股票分析，并保存分析历史
    """
    symbol = request.symbol
    start_date = request.start_date
    end_date = request.end_date
    prompt = request.prompt

    # 调用 LLM 服务分析
    try:
        stock_data = get_stock_data_for_analysis(request.symbol)
            # 获取LLM服务
        llm_service = llm_manager.get_service(
        provider=request.provider or os.getenv('DEFAULT_LLM_PROVIDER', 'Custom'),
        model=request.model or os.getenv('DEFAULT_MODEL', 'qwen-plus')
        )
        analysis_result = await llm_service.analyze_stock(symbol, stock_data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI分析失败: {str(e)}")

    # # 保存分析历史
    # collections = get_db_collections()
    # analysis_col = collections["analysis_history"]
    # history_doc = {
    #     "user_id": current_user["id"],
    #     "symbol": symbol,
    #     "start_date": start_date,
    #     "end_date": end_date,
    #     "prompt": prompt,
    #     "result": analysis_result,
    #     "timestamp": datetime.now()
    # }
    # analysis_col.insert_one(history_doc)

    analysis_result["user_id"] = current_user["id"]
    analysis_result["username"] = current_user["username"]

    return analysis_result

@router.post("/analysis-history")
def save_analysis_history(req: AnalysisHistoryRequest, current_user: dict = Depends(get_current_active_user)):
    """
    保存分析历史（手动保存）
    """
    collections = get_db_collections()
    analysis_col = collections["analysis_history"]
    history_doc = {
        "user_id": current_user["id"],
        "symbol": req.symbol,
        "prompt": req.prompt,
        "analysis_result": req.analysis_result,
        "provider": req.provider,
        "model": req.model,
        "timestamp": req.timestamp
    }
    analysis_col.insert_one(history_doc)
    return {"success": True}

@router.get("/analysis-history")
def get_analysis_history(symbol: str, current_user: dict = Depends(get_current_active_user)):
    """
    获取当前用户指定股票的分析历史
    """
    collections = get_db_collections()
    analysis_col = collections["analysis_history"]
    query = {
        "user_id": current_user["id"],
        "symbol": symbol
    }
    history = list(analysis_col.find(query).sort("timestamp", -1))
    for h in history:
        print(h)
        h["_id"] = str(h["_id"])

    return {"success": True, "data": history}

def get_stock_data_for_analysis(symbol: str) -> Optional[Dict[str, Any]]:
    """获取用于分析的股票数据"""
    try:
        # 获取最新记录
        collections = get_db_collections()
        collection = collections["volume_price"]  # 或你实际存储股票行情的集合名
        latest_record = collection.find_one(
            {"symbol": symbol},
            sort=[("trade_date", -1)]
        )
        
        if not latest_record:
            return None
        
        # 获取最近30天的历史记录
        historical_records = list(collection.find(
            {"symbol": symbol},
            sort=[("trade_date", -1)]
        ).limit(30))
        
        return {
            "symbol": symbol,
            "latest_record": serialize_doc(latest_record),
            "historical_records": [serialize_doc(doc) for doc in historical_records]
        }
    except Exception as e:
        print(f"获取股票数据失败: {e}")
        return None