import os
from typing import Optional, Dict, Any
from fastapi import APIRouter, Depends, Query, HTTPException
from models import StockAnalysisRequest, AnalysisHistoryRequest
from auth import get_current_active_user
from db import get_db_collections
from llm_service import llm_manager
from datetime import datetime
from utils import serialize_doc,format_trade_date

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
        if not stock_data:
            raise HTTPException(status_code=404, detail="未找到该股票的量价或资金流数据")
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
    """获取用于分析的股票数据（包含量价和资金流）"""
    try:
        collections = get_db_collections()
        volume_col = collections["volume_price"]
        money_flow_col = collections["money_flow"]

        # 获取最近30天的量价数据
        volume_records = list(volume_col.find(
            {"symbol": symbol},
            sort=[("trade_date", -1)]
        ).limit(30))

        # 获取最近30天的资金流数据
        money_flow_records = list(money_flow_col.find(
            {"symbol": symbol},
            sort=[("trade_date", -1)]
        ).limit(30))

        # 以 trade_date 为键，合并两类数据
        money_flow_map = {format_trade_date(doc["trade_date"]): serialize_doc(doc) for doc in money_flow_records}
        merged_records = []
        for vdoc in volume_records:
            trade_date_str = format_trade_date(vdoc.get("trade_date"))
            merged = {
                "trade_date": trade_date_str,
                "volume_price": serialize_doc(vdoc),
                "money_flow": money_flow_map.get(trade_date_str)
            }
            merged_records.append(merged)
            print(merged)

        # 最新一条量价数据
        latest_record = volume_records[0] if volume_records else None

        return {
            "symbol": symbol,
            "latest_record": serialize_doc(latest_record) if latest_record else None,
            "historical_records": merged_records
        }
    except Exception as e:
        print(f"获取股票数据失败: {e}")
        return None