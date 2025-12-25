import os
from typing import Optional, Dict, Any
from fastapi import APIRouter, Depends, Query, HTTPException
from models.all_models import StockAnalysisRequest, AnalysisHistoryRequest
from auth import get_current_active_user
from db import get_db_collections
from llm_service import llm_manager
from datetime import datetime
from utils.stock_utils import serialize_doc, format_trade_date

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
    """保存分析历史，统一存储为 analysis_result.analysis 结构。"""
    collections = get_db_collections()
    analysis_col = collections["analysis_history"]

    analysis_result = getattr(req, "analysis_result", None)
    if not isinstance(analysis_result, dict):
        raise HTTPException(status_code=400, detail="analysis_result 必须为对象，且包含 analysis 字段")

    analysis_obj = analysis_result.get("analysis")
    # 防止被二次包裹成 analysis_result.analysis.analysis
    if isinstance(analysis_obj, dict) and "analysis" in analysis_obj:
        core = analysis_obj.get("analysis")
        # 仅在缺少核心字段且发现再次嵌套时解包
        known_keys = {"technical_analysis", "investment_advice", "risk_level", "support_level", "resistance_level", "long_term_forecast", "mid_term_forecast", "short_term_forecast", "key_points", "confidence_score", "stock_name", "symbol"}
        if not (known_keys & set(analysis_obj.keys())) and core is not None:
            analysis_obj = core
    if analysis_obj is None:
        raise HTTPException(status_code=400, detail="analysis_result.analysis 不能为空")

    created_at = getattr(req, "timestamp", None) or datetime.utcnow().isoformat() + "Z"

    stock_name = getattr(req, "stock_name", None) or (analysis_obj.get("stock_name") if isinstance(analysis_obj, dict) else None)

    # 确保嵌套结构中包含股票代码/名称，便于前端展示
    if isinstance(analysis_obj, dict):
        updated_analysis = dict(analysis_obj)
        updated_analysis.setdefault("symbol", req.symbol)
        if stock_name:
            updated_analysis.setdefault("stock_name", stock_name)
    else:
        updated_analysis = analysis_obj

    stored_analysis_result = dict(analysis_result)
    stored_analysis_result["analysis"] = updated_analysis

    provider = req.provider or (analysis_result.get("provider") if isinstance(analysis_result, dict) else None)
    model = req.model or (analysis_result.get("model") if isinstance(analysis_result, dict) else None)

    history_doc = {
        "user_id": current_user["id"],
        "symbol": req.symbol,
        "stock_name": stock_name,
        "analysis_result": stored_analysis_result,
        "provider": provider,
        "model": model,
        "created_at": created_at,
        "timestamp": created_at,
        "prompt": req.prompt
    }
    analysis_col.insert_one(history_doc)
    return {"success": True}
@router.get("/analysis-history")
def get_analysis_history(symbol: Optional[str] = None, page: int = 1, limit: int = 50, current_user: dict = Depends(get_current_active_user)):
    """
    获取当前用户指定股票的分析历史。
    如果未提供 `symbol`，将返回当前用户的所有历史（按时间倒序），支持分页（page, limit）。
    """
    collections = get_db_collections()
    analysis_col = collections["analysis_history"]

    query = {"user_id": current_user.get("id")}
    if symbol:
        query["symbol"] = symbol

    try:
        page = max(1, int(page))
    except Exception:
        page = 1
    try:
        limit = max(1, min(1000, int(limit)))
    except Exception:
        limit = 50

    skip = (page - 1) * limit

    total = analysis_col.count_documents(query)
    history = list(analysis_col.find(query).sort("timestamp", -1).skip(skip).limit(limit))

    normalized = []
    for h in history:
        h_id = str(h.get("_id")) if h.get("_id") else ""
        analysis_result = h.get("analysis_result") if isinstance(h.get("analysis_result"), dict) else None
        analysis_obj = analysis_result.get("analysis") if isinstance(analysis_result, dict) else None

        # 兼容旧数据
        if analysis_obj is None and isinstance(h.get("analysis"), dict):
            analysis_obj = h.get("analysis")
        # 解包可能的二次嵌套
        if isinstance(analysis_obj, dict) and "analysis" in analysis_obj:
            core = analysis_obj.get("analysis")
            known_keys = {"technical_analysis", "investment_advice", "risk_level", "support_level", "resistance_level", "long_term_forecast", "mid_term_forecast", "short_term_forecast", "key_points", "confidence_score", "stock_name", "symbol"}
            if not (known_keys & set(analysis_obj.keys())) and core is not None:
                analysis_obj = core

        # 兼容旧数据: 若无 symbol 则从历史 stock_code 填充
        symbol_val = h.get("symbol") or h.get("stock_code")
        if not symbol_val and isinstance(analysis_obj, dict):
            symbol_val = analysis_obj.get("symbol") or analysis_obj.get("stock_code")

        stock_name = h.get("stock_name")
        if not stock_name and isinstance(analysis_obj, dict):
            stock_name = analysis_obj.get("stock_name")

        created_at = h.get("created_at") or h.get("timestamp") or None

        normalized.append({
            "id": h_id,
            "stock_name": stock_name,
            "symbol": symbol_val,
            "analysis": analysis_obj,
            "analysis_result": analysis_result,
            "provider": h.get("provider") or (analysis_result.get("provider") if analysis_result else None),
            "model": h.get("model") or (analysis_result.get("model") if analysis_result else None),
            "created_at": created_at
        })

    return {"success": True, "data": normalized, "page": page, "limit": limit, "total": total}

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