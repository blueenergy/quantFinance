import os
from typing import Optional, Dict, Any, List
from fastapi import APIRouter, Depends, Query, HTTPException
from models.all_models import StockAnalysisRequest, AnalysisHistoryRequest
from auth import get_current_active_user
from db import get_db_collections
from llm_service import llm_manager
from datetime import datetime
from utils.stock_utils import serialize_doc, format_trade_date
from bson import ObjectId

router = APIRouter()


@router.post("/analyze/portfolio-opportunities")
async def analyze_portfolio_opportunities(current_user: dict = Depends(get_current_active_user)):
    """
    分析用户自选股和持仓的投资机会
    
    基于连板天梯数据，识别：
    1. 与主线板块关联的补涨机会
    2. 持仓股票的高抛低吸建议
    """
    user_id = current_user["id"]
    collections = get_db_collections()
    
    # 1. 获取用户自选股
    watchlist_col = collections["user_watchlists"]
    stock_info_col = collections["stock_info"]
    
    # 自选股数据结构: { user_id, symbols: [...] }
    watchlist_doc = watchlist_col.find_one({"user_id": user_id})
    watchlist_symbols = watchlist_doc.get("symbols", []) if watchlist_doc else []
    
    # 获取自选股的行业信息
    watchlist = []
    if watchlist_symbols:
        stock_info_docs = {doc["symbol"]: doc for doc in stock_info_col.find({"symbol": {"$in": watchlist_symbols}})}
        for symbol in watchlist_symbols:
            info = stock_info_docs.get(symbol, {})
            watchlist.append({
                "symbol": symbol,
                "name": info.get("name", ""),
                "industry": info.get("industry", ""),
                "change_pct": info.get("change_pct", 0)
            })
    
    # 2. 获取用户持仓
    positions_col = collections["trader_positions"]
    positions_docs = list(positions_col.find({"user_id": user_id}))
    
    # 获取持仓的行业信息
    positions = []
    if positions_docs:
        position_symbols = [doc.get("symbol") for doc in positions_docs if doc.get("symbol")]
        stock_info_docs = {doc["symbol"]: doc for doc in stock_info_col.find({"symbol": {"$in": position_symbols}})}
        
        for doc in positions_docs:
            symbol = doc.get("symbol")
            info = stock_info_docs.get(symbol, {})
            cost_price = doc.get("avg_price", 0)
            current_price = doc.get("last_price", 0)
            quantity = doc.get("qty", 0)
            
            # 计算盈亏
            if cost_price > 0 and quantity > 0:
                pnl_pct = ((current_price - cost_price) / cost_price) * 100
            else:
                pnl_pct = 0
            
            positions.append({
                "symbol": symbol,
                "stock_name": info.get("name", ""),
                "name": info.get("name", ""),
                "industry": info.get("industry", ""),
                "cost_price": cost_price,
                "current_price": current_price,
                "quantity": quantity,
                "unrealized_pnl_pct": pnl_pct
            })
    
    # 3. 获取连板天梯数据
    ladder_data = _fetch_ladder_data_for_portfolio()
    
    if not ladder_data:
        return {
            "success": False,
            "error": "无法获取连板天梯数据",
            "watchlist_count": len(watchlist),
            "positions_count": len(positions)
        }
    
    # 4. 获取用户LLM配置
    users_col = collections["users"]
    user_doc = users_col.find_one({"_id": ObjectId(user_id)})
    
    if not user_doc:
        raise HTTPException(status_code=404, detail="用户不存在")
    
    active_config_id = user_doc.get("active_llm_config_id")
    llm_configs = user_doc.get("llm_configs", [])
    
    if not active_config_id and llm_configs:
        active_config_id = str(llm_configs[0].get("_id"))
    
    if not active_config_id:
        raise HTTPException(status_code=400, detail="请先配置LLM")
    
    active_config = None
    for config in llm_configs:
        if str(config.get("_id")) == active_config_id:
            active_config = config
            break
    
    if not active_config:
        raise HTTPException(status_code=404, detail="未找到激活的LLM配置")
    
    # 5. 调用LLM分析
    llm_service = llm_manager.get_service(
        provider=active_config.get("provider"),
        model=active_config.get("model"),
        api_key=active_config.get("api_key"),
        base_url=active_config.get("base_url")
    )
    
    analysis_result = await llm_service.analyze_portfolio_opportunities(
        watchlist=watchlist,
        positions=positions,
        ladder_data=ladder_data
    )
    
    # 6. 保存分析结果到数据库
    if analysis_result.get("success"):
        portfolio_analysis_col = collections["portfolio_analysis"]
        
        analysis_doc = {
            "user_id": user_id,
            "type": "combined",  # 区分类型: watchlist, positions, combined
            "analysis": analysis_result.get("analysis"),
            "provider": analysis_result.get("provider"),
            "model": analysis_result.get("model"),
            "watchlist_count": len(watchlist),
            "positions_count": len(positions),
            "ladder_date": ladder_data.get("date"),
            "created_at": datetime.now(),
            "timestamp": datetime.now().timestamp()
        }
        
        portfolio_analysis_col.insert_one(analysis_doc)
    
    # 7. 返回结果
    return {
        "success": analysis_result.get("success", False),
        "type": "combined",
        "analysis": analysis_result.get("analysis"),
        "provider": analysis_result.get("provider"),
        "model": analysis_result.get("model"),
        "watchlist_count": len(watchlist),
        "positions_count": len(positions),
        "ladder_date": ladder_data.get("date"),
        "created_at": datetime.now().isoformat(),
        "error": analysis_result.get("error")
    }


@router.post("/analyze/watchlist-opportunities")
async def analyze_watchlist_opportunities(current_user: dict = Depends(get_current_active_user)):
    """
    仅分析用户自选股的投资机会（补涨机会分析）
    """
    user_id = current_user["id"]
    collections = get_db_collections()
    
    # 获取用户自选股
    watchlist_col = collections["user_watchlists"]
    stock_info_col = collections["stock_info"]
    
    # 自选股数据结构: { user_id, symbols: [...] }
    watchlist_doc = watchlist_col.find_one({"user_id": user_id})
    watchlist_symbols = watchlist_doc.get("symbols", []) if watchlist_doc else []
    
    watchlist = []
    if watchlist_symbols:
        stock_info_docs = {doc["symbol"]: doc for doc in stock_info_col.find({"symbol": {"$in": watchlist_symbols}})}
        for symbol in watchlist_symbols:
            info = stock_info_docs.get(symbol, {})
            watchlist.append({
                "symbol": symbol,
                "name": info.get("name", ""),
                "industry": info.get("industry", ""),
                "change_pct": info.get("change_pct", 0)
            })
    
    if not watchlist:
        return {"success": False, "error": "暂无自选股"}
    
    # 获取连板天梯数据
    ladder_data = _fetch_ladder_data_for_portfolio()
    if not ladder_data:
        return {"success": False, "error": "无法获取连板天梯数据"}
    
    # 获取用户LLM配置
    llm_service = await _get_user_llm_service(user_id, collections)
    if not llm_service:
        return {"success": False, "error": "请先配置LLM"}
    
    # 调用LLM分析（只传自选股，不传持仓）
    analysis_result = await llm_service.analyze_portfolio_opportunities(
        watchlist=watchlist,
        positions=[],
        ladder_data=ladder_data
    )
    
    # 保存分析结果
    if analysis_result.get("success"):
        portfolio_analysis_col = collections["portfolio_analysis"]
        analysis_doc = {
            "user_id": user_id,
            "type": "watchlist",
            "analysis": analysis_result.get("analysis"),
            "provider": analysis_result.get("provider"),
            "model": analysis_result.get("model"),
            "watchlist_count": len(watchlist),
            "positions_count": 0,
            "ladder_date": ladder_data.get("date"),
            "created_at": datetime.now(),
            "timestamp": datetime.now().timestamp()
        }
        portfolio_analysis_col.insert_one(analysis_doc)
    
    return {
        "success": analysis_result.get("success", False),
        "type": "watchlist",
        "analysis": analysis_result.get("analysis"),
        "provider": analysis_result.get("provider"),
        "model": analysis_result.get("model"),
        "watchlist_count": len(watchlist),
        "ladder_date": ladder_data.get("date"),
        "created_at": datetime.now().isoformat(),
        "error": analysis_result.get("error")
    }


@router.post("/analyze/positions-opportunities")
async def analyze_positions_opportunities(current_user: dict = Depends(get_current_active_user)):
    """
    仅分析用户持仓的投资机会（高抛低吸建议）
    """
    user_id = current_user["id"]
    collections = get_db_collections()
    
    # 获取用户持仓
    positions_col = collections["trader_positions"]
    stock_info_col = collections["stock_info"]
    
    positions_docs = list(positions_col.find({"user_id": user_id}))
    
    positions = []
    if positions_docs:
        position_symbols = [doc.get("symbol") for doc in positions_docs if doc.get("symbol")]
        stock_info_docs = {doc["symbol"]: doc for doc in stock_info_col.find({"symbol": {"$in": position_symbols}})}
        
        for doc in positions_docs:
            symbol = doc.get("symbol")
            info = stock_info_docs.get(symbol, {})
            cost_price = doc.get("avg_price", 0)
            current_price = doc.get("last_price", 0)
            quantity = doc.get("qty", 0)
            
            if cost_price > 0 and quantity > 0:
                pnl_pct = ((current_price - cost_price) / cost_price) * 100
            else:
                pnl_pct = 0
            
            positions.append({
                "symbol": symbol,
                "stock_name": info.get("name", ""),
                "name": info.get("name", ""),
                "industry": info.get("industry", ""),
                "cost_price": cost_price,
                "current_price": current_price,
                "quantity": quantity,
                "unrealized_pnl_pct": pnl_pct
            })
    
    if not positions:
        return {"success": False, "error": "暂无持仓"}
    
    # 获取连板天梯数据
    ladder_data = _fetch_ladder_data_for_portfolio()
    if not ladder_data:
        return {"success": False, "error": "无法获取连板天梯数据"}
    
    # 获取用户LLM配置
    llm_service = await _get_user_llm_service(user_id, collections)
    if not llm_service:
        return {"success": False, "error": "请先配置LLM"}
    
    # 调用LLM分析（只传持仓，不传自选股）
    analysis_result = await llm_service.analyze_portfolio_opportunities(
        watchlist=[],
        positions=positions,
        ladder_data=ladder_data
    )
    
    # 保存分析结果
    if analysis_result.get("success"):
        portfolio_analysis_col = collections["portfolio_analysis"]
        analysis_doc = {
            "user_id": user_id,
            "type": "positions",
            "analysis": analysis_result.get("analysis"),
            "provider": analysis_result.get("provider"),
            "model": analysis_result.get("model"),
            "watchlist_count": 0,
            "positions_count": len(positions),
            "ladder_date": ladder_data.get("date"),
            "created_at": datetime.now(),
            "timestamp": datetime.now().timestamp()
        }
        portfolio_analysis_col.insert_one(analysis_doc)
    
    return {
        "success": analysis_result.get("success", False),
        "type": "positions",
        "analysis": analysis_result.get("analysis"),
        "provider": analysis_result.get("provider"),
        "model": analysis_result.get("model"),
        "positions_count": len(positions),
        "ladder_date": ladder_data.get("date"),
        "created_at": datetime.now().isoformat(),
        "error": analysis_result.get("error")
    }


@router.get("/analyze/portfolio-opportunities/latest")
async def get_latest_portfolio_analysis(
    analysis_type: Optional[str] = Query(None, description="分析类型: watchlist, positions, combined"),
    current_user: dict = Depends(get_current_active_user)
):
    """
    获取用户最新的组合分析结果
    """
    user_id = current_user["id"]
    collections = get_db_collections()
    portfolio_analysis_col = collections["portfolio_analysis"]
    
    # 构建查询条件
    query = {"user_id": user_id}
    if analysis_type:
        query["type"] = analysis_type
    
    # 查找最新的分析结果
    latest = portfolio_analysis_col.find_one(
        query,
        sort=[("created_at", -1)]
    )
    
    if not latest:
        return {
            "success": False,
            "error": "暂无分析历史"
        }
    
    return {
        "success": True,
        "type": latest.get("type", "combined"),
        "analysis": latest.get("analysis"),
        "provider": latest.get("provider"),
        "model": latest.get("model"),
        "watchlist_count": latest.get("watchlist_count", 0),
        "positions_count": latest.get("positions_count", 0),
        "ladder_date": latest.get("ladder_date"),
        "created_at": latest.get("created_at").isoformat() if latest.get("created_at") else None,
        "timestamp": latest.get("timestamp")
    }


async def _get_user_llm_service(user_id: str, collections):
    """获取用户配置的LLM服务"""
    users_col = collections["users"]
    user_doc = users_col.find_one({"_id": ObjectId(user_id)})
    
    if not user_doc:
        return None
    
    active_config_id = user_doc.get("active_llm_config_id")
    llm_configs = user_doc.get("llm_configs", [])
    
    if not active_config_id and llm_configs:
        active_config_id = str(llm_configs[0].get("_id"))
    
    if not active_config_id:
        return None
    
    active_config = None
    for config in llm_configs:
        if str(config.get("_id")) == active_config_id:
            active_config = config
            break
    
    if not active_config:
        return None
    
    return llm_manager.get_service(
        provider=active_config.get("provider"),
        model=active_config.get("model"),
        api_key=active_config.get("api_key"),
        base_url=active_config.get("base_url")
    )


def _fetch_ladder_data_for_portfolio() -> Optional[Dict[str, Any]]:
    """获取连板天梯数据（给 portfolio 分析使用）"""
    collections = get_db_collections()
    ladder_col = collections["limit_up_ladder"]
    daily_col = collections["daily_limit_up"]
    
    today = datetime.now().strftime("%Y%m%d")
    
    ladder = ladder_col.find_one({"date": today})
    if not ladder:
        ladder = ladder_col.find_one(
            {"date": {"$lte": today}},
            sort=[("date", -1)]
        )
    
    if not ladder:
        return None
    
    actual_date = ladder.get("date", today)
    indicators = ladder.get("indicators", {})
    
    # 梯队分布
    tier_distribution = {}
    total_limit_up = 0
    
    for tier_key, tier_data in ladder.get("tiers", {}).items():
        count = tier_data.get("count", 0)
        tier_distribution[tier_key] = count
        total_limit_up += count
    
    # 最高连板
    highest_stock = daily_col.find_one(
        {"date": actual_date},
        sort=[("streak", -1)]
    )
    max_streak = highest_stock.get("streak", 0) if highest_stock else 0
    
    # 主线板块
    sector_ranking = ladder.get("sector_ranking", [])
    mainlines = [s.get("sector", "") for s in sector_ranking[:5] if s.get("sector")]
    
    # 晋级率
    high_tier_count = sum(tier_distribution.get(k, 0) for k in ["2", "3", "4+"])
    promotion_rate = high_tier_count / total_limit_up if total_limit_up > 0 else 0
    
    # 情绪等级
    sentiment_score = indicators.get("sentiment_score", 50)
    if sentiment_score >= 80:
        sentiment_level = "极度乐观"
    elif sentiment_score >= 60:
        sentiment_level = "谨慎乐观"
    elif sentiment_score >= 40:
        sentiment_level = "中性观望"
    elif sentiment_score >= 20:
        sentiment_level = "谨慎悲观"
    else:
        sentiment_level = "极度悲观"
    
    return {
        "date": actual_date,
        "sentiment_score": sentiment_score,
        "sentiment_level": sentiment_level,
        "total_limit_up": total_limit_up,
        "max_streak": max_streak,
        "promotion_rate": round(promotion_rate, 3),
        "mainlines": mainlines,
        "tier_distribution": tier_distribution,
        "highest_stock": {
            "name": highest_stock.get("name", "") if highest_stock else "",
            "streak": max_streak
        } if highest_stock else None
    }


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
        
        # 获取用户激活的LLM配置
        collections = get_db_collections()
        users_col = collections["users"]
        
        user_doc = users_col.find_one({"_id": ObjectId(current_user["id"])})
        if not user_doc:
            raise HTTPException(status_code=404, detail="用户不存在")
        
        active_config_id = user_doc.get("active_llm_config_id")
        llm_configs = user_doc.get("llm_configs", [])
        
        # 如果没有激活配置但有可用配置，自动激活第一个
        if not active_config_id and llm_configs:
            active_config_id = str(llm_configs[0].get("_id"))
            # 更新用户文档以激活该配置
            users_col.update_one(
                {"_id": ObjectId(current_user["id"])},
                {"$set": {"active_llm_config_id": active_config_id}}
            )
        
        if not active_config_id:
            raise HTTPException(status_code=400, detail="请先在用户配置中配置一个LLM配置")
        
        # 查找激活的配置
        active_config = None
        for config in llm_configs:
            if str(config.get("_id")) == active_config_id:
                active_config = config
                break
        
        if not active_config:
            raise HTTPException(status_code=404, detail="激活的LLM配置不存在")
        
        # 获取配置参数
        user_llm_provider = active_config.get("provider")
        user_llm_api_key = active_config.get("api_key")
        user_llm_base_url = active_config.get("base_url")
        user_llm_model = active_config.get("model")
        
        # 使用用户配置，不使用任何默认值
        # 如果请求中有指定，优先使用请求参数（用于特殊场景）
        provider = request.provider or user_llm_provider
        model = request.model or user_llm_model
        
        # 验证必需参数
        if not provider or not model or not user_llm_api_key:
            raise HTTPException(
                status_code=400, 
                detail="LLM配置不完整，请在用户设置中配置完整的LLM信息(provider、model、api_key)"
            )
        
        # 获取LLM服务，优先使用用户特定的API密钥和基础URL
        llm_service = llm_manager.get_service(
            provider=provider,
            model=model,
            api_key=user_llm_api_key,
            base_url=user_llm_base_url
        )
        analysis_result = await llm_service.analyze_stock(symbol, stock_data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI分析失败: {str(e)}")

    # # 保存分析历史
    # # collections = get_db_collections()
    # # analysis_col = collections["analysis_history"]
    # # history_doc = {
    # #     "user_id": current_user["id"],
    # #     "symbol": symbol,
    # #     "start_date": start_date,
    # #     "end_date": end_date,
    # #     "prompt": prompt,
    # #     "result": analysis_result,
    # #     "timestamp": datetime.now()
    # # }
    # # analysis_col.insert_one(history_doc)

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