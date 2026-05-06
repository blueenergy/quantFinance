import os
import logging
from typing import Optional, Dict, Any, List
from fastapi import APIRouter, Depends, Query, HTTPException
from auth import get_current_active_user
from db import get_db_collections, get_analyzer_db, get_data_db
from datetime import datetime
from utils.stock_utils import serialize_doc, format_trade_date
from bson import ObjectId
from models.analysis_task import AnalysisTaskCreate
from services.quota_manager import QuotaManager
from services.analysis_tasks import build_task_doc, can_access_task
from services.analysis_history import build_user_analysis_history_payload
from db import db
from routers.entitlements_matrix import (
    FEATURE_WATCHLIST_OPPORTUNITIES,
    resolve_action_entitlement,
    resolve_deep_analysis_entitlement,
)

router = APIRouter()
logger = logging.getLogger(__name__)


def _normalize_task_status(raw_status: Any) -> str:
    """
    Normalize task status to canonical lowercase strings expected by frontend:
    pending / processing / completed / failed.
    """
    if raw_status is None:
        return "unknown"
    # Enum instance (e.g., TaskStatus.PENDING)
    if hasattr(raw_status, "value"):
        try:
            return str(raw_status.value).lower()
        except Exception:
            pass
    # String forms, including legacy "TaskStatus.PENDING"
    s = str(raw_status)
    if s.startswith("TaskStatus."):
        s = s.split(".", 2)[1]
    return s.lower()


def _resolve_user_level(current_user: Dict[str, Any]) -> str:
    """Resolve user level for default QuotaManager path."""
    if current_user.get("is_admin") or current_user.get("is_vip"):
        return "vip"

    level = current_user.get("level")
    service_level = current_user.get("service_level")
    if level == "vip" or service_level == "vip":
        return "vip"
    return level or service_level or "free"


def _build_entitlement_denied_message(
    *,
    current_user: Dict[str, Any],
    entitlement: Dict[str, Any],
    quota_manager: QuotaManager,
    feature_label: str,
) -> Optional[str]:
    """Return denial reason if blocked or quota exceeded; otherwise None."""
    if entitlement.get("type") == "blocked":
        return entitlement.get("detail", f"{feature_label}不可用")

    user_level = _resolve_user_level(current_user)
    user_id = current_user["id"]

    if entitlement.get("type") == "matrix":
        feature_key = entitlement.get("feature_key", "")
        matrix_limit = int(entitlement.get("weekly_limit", 0) or 0)
        if not quota_manager.check_feature_weekly_quota(user_id, feature_key, matrix_limit):
            rem = quota_manager.get_remaining_feature_weekly_quota(user_id, feature_key, matrix_limit)
            return f"本周{feature_label}次数已用完（权益矩阵限制 {matrix_limit} 次/周）。剩余: {rem}"
        return None

    if not quota_manager.check_quota(user_id, user_level):
        limit = quota_manager.get_limit(user_level)
        period_key = quota_manager.get_period_key(user_level)
        return (
            f"配额已用完。{user_level}用户限制暂为 {limit}次/"
            f"{'月' if user_level == 'vip' else '周'}。当前周期: {period_key}"
        )
    return None


def _enforce_entitlement_and_quota(
    *,
    current_user: Dict[str, Any],
    entitlement: Dict[str, Any],
    quota_manager: QuotaManager,
    feature_label: str,
    on_denied: str = "http",  # "http" | "json"
) -> Optional[Dict[str, Any]]:
    denied_message = _build_entitlement_denied_message(
        current_user=current_user,
        entitlement=entitlement,
        quota_manager=quota_manager,
        feature_label=feature_label,
    )
    if denied_message is None:
        return None

    logger.info(
        "[entitlement] denied feature=%s user_id=%s reason=%s",
        entitlement.get("feature_key") or feature_label,
        current_user.get("id"),
        denied_message,
    )
    if on_denied == "json":
        return {"success": False, "error": denied_message}
    raise HTTPException(status_code=403, detail=denied_message)


def _consume_quota_after_submit(
    *,
    current_user: Dict[str, Any],
    entitlement: Dict[str, Any],
    quota_manager: QuotaManager,
) -> int:
    """Consume quota after successful task submission and return remaining."""
    user_id = current_user["id"]
    user_level = _resolve_user_level(current_user)

    if entitlement.get("type") == "matrix":
        feature_key = entitlement.get("feature_key", "")
        matrix_limit = int(entitlement.get("weekly_limit", 0) or 0)
        quota_manager.consume_feature_weekly_quota(user_id, feature_key)
        return quota_manager.get_remaining_feature_weekly_quota(user_id, feature_key, matrix_limit)

    quota_manager.consume_quota(user_id, user_level)
    return quota_manager.get_remaining(user_id, user_level)


@router.post("/analyze/portfolio-opportunities")
async def analyze_portfolio_opportunities(current_user: dict = Depends(get_current_active_user)):
    """
    分析用户自选股和持仓的投资机会（异步任务模式）
    
    基于连板天梯数据，识别：
    1. 与主线板块关联的补涨机会
    2. 持仓股票的高抛低吸建议
    """
    user_id = current_user["id"]
    collections = get_db_collections()
    quota_manager = QuotaManager(db)
    ent = resolve_action_entitlement(
        user=current_user,
        feature_key=FEATURE_WATCHLIST_OPPORTUNITIES,
        blocked_detail="当前等级未开放机会分析（权益矩阵已关闭）。",
    )
    denied = _enforce_entitlement_and_quota(
        current_user=current_user,
        entitlement=ent,
        quota_manager=quota_manager,
        feature_label="机会分析",
        on_denied="json",
    )
    if denied:
        return denied
    
    watchlist = _fetch_user_watchlist(user_id, collections)
    positions = _fetch_user_positions(user_id, collections)
    ladder_data = _fetch_ladder_data_for_portfolio()
    
    if not ladder_data:
        return {
            "success": False,
            "error": "无法获取连板天梯数据",
            "watchlist_count": len(watchlist),
            "positions_count": len(positions)
        }
    
    task_id = _submit_portfolio_task(
        current_user=current_user,
        watchlist=watchlist,
        positions=positions,
        ladder_data=ladder_data,
        analysis_type="combined",
        collections=collections
    )
    remaining = _consume_quota_after_submit(
        current_user=current_user,
        entitlement=ent,
        quota_manager=quota_manager,
    )
    
    return {
        "success": True,
        "task_id": task_id,
        "status": "pending",
        "message": "组合分析任务已提交",
        "type": "combined",
        "watchlist_count": len(watchlist),
        "positions_count": len(positions),
        "quota_remaining": remaining,
    }


@router.post("/analyze/watchlist-opportunities")
async def analyze_watchlist_opportunities(current_user: dict = Depends(get_current_active_user)):
    """
    仅分析用户自选股的投资机会（补涨机会分析，异步任务模式）
    """
    user_id = current_user["id"]
    collections = get_db_collections()
    quota_manager = QuotaManager(db)
    ent = resolve_action_entitlement(
        user=current_user,
        feature_key=FEATURE_WATCHLIST_OPPORTUNITIES,
        blocked_detail="当前等级未开放机会分析（权益矩阵已关闭）。",
    )
    denied = _enforce_entitlement_and_quota(
        current_user=current_user,
        entitlement=ent,
        quota_manager=quota_manager,
        feature_label="机会分析",
        on_denied="json",
    )
    if denied:
        return denied
    
    watchlist = _fetch_user_watchlist(user_id, collections)
    
    if not watchlist:
        return {"success": False, "error": "暂无自选股"}
    
    ladder_data = _fetch_ladder_data_for_portfolio()
    if not ladder_data:
        return {"success": False, "error": "无法获取连板天梯数据"}
    
    task_id = _submit_portfolio_task(
        current_user=current_user,
        watchlist=watchlist,
        positions=[],
        ladder_data=ladder_data,
        analysis_type="watchlist",
        collections=collections
    )
    remaining = _consume_quota_after_submit(
        current_user=current_user,
        entitlement=ent,
        quota_manager=quota_manager,
    )
    
    return {
        "success": True,
        "task_id": task_id,
        "status": "pending",
        "message": "自选股分析任务已提交",
        "type": "watchlist",
        "watchlist_count": len(watchlist),
        "quota_remaining": remaining,
    }


@router.post("/analyze/positions-opportunities")
async def analyze_positions_opportunities(current_user: dict = Depends(get_current_active_user)):
    """
    仅分析用户持仓的投资机会（高抛低吸建议，异步任务模式）
    """
    user_id = current_user["id"]
    collections = get_db_collections()
    
    positions = _fetch_user_positions(user_id, collections)
    
    if not positions:
        return {"success": False, "error": "暂无持仓"}
    
    ladder_data = _fetch_ladder_data_for_portfolio()
    if not ladder_data:
        return {"success": False, "error": "无法获取连板天梯数据"}
    
    task_id = _submit_portfolio_task(
        current_user=current_user,
        watchlist=[],
        positions=positions,
        ladder_data=ladder_data,
        analysis_type="positions",
        collections=collections
    )
    
    return {
        "success": True,
        "task_id": task_id,
        "status": "pending",
        "message": "持仓分析任务已提交",
        "type": "positions",
        "positions_count": len(positions)
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
    portfolio_analysis_col = get_analyzer_db()["portfolio_analysis"]
    
    query = {"user_id": user_id}
    if analysis_type:
        query["type"] = analysis_type
    
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


@router.get("/analyze/task/{task_id}")
@router.get("/analyze/portfolio-task/{task_id}")
async def get_analysis_task_status(
    task_id: str,
    current_user: dict = Depends(get_current_active_user)
):
    """
    通用分析任务状态查询（支持组合分析与深度分析）
    """
    collections = get_db_collections()
    try:
        task = collections["analysis_tasks"].find_one({"_id": ObjectId(task_id)})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid task_id")
    
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    
    if not can_access_task(task, current_user):
        logger.warning(
            "[portfolio-task] access denied task_id=%s task_user_id=%s current_user_id=%s current_username=%s",
            task_id,
            task.get("user_id"),
            current_user.get("id"),
            current_user.get("username"),
        )
        raise HTTPException(status_code=403, detail="无权访问此任务")
    
    response = {
        "task_id": task_id,
        "status": _normalize_task_status(task.get("status", "unknown")),
        "type": task.get("params", {}).get("analysis_type", "combined"),
        "analysis_mode": task.get("params", {}).get("analysis_mode", "classic"),
        "created_at": task.get("created_at").isoformat() if task.get("created_at") else None
    }
    
    if response["status"] == "completed":
        result = task.get("result", {})
        if isinstance(result, dict):
            # 缺省为 True：深度分析等流水线往往只存分析正文，不设顶层 success；
            # 使用 False 作为缺省会导致 task 轮询误报“失败/异常”，而 history 仍正常显示。
            response["success"] = result.get("success", True)
        else:
            response["success"] = result is not None
        response["analysis"] = result.get("analysis") if isinstance(result, dict) else None
        response["model"] = result.get("model") if isinstance(result, dict) else None
    elif response["status"] == "completed_with_parse_error":
        result = task.get("result", {})
        response["success"] = False
        response["analysis"] = result.get("analysis")
        response["model"] = result.get("model")
        response["error"] = task.get("error") or result.get("error_detail") or result.get("error") or "结果解析失败"
    elif response["status"] == "failed":
        response["error"] = task.get("error", "分析失败")

    logger.debug(
        "[portfolio-task] task_id=%s status=%s response_success=%s worker_id=%s completed_at=%s failed_at=%s",
        task_id,
        task.get("status"),
        response.get("success"),
        task.get("worker_id"),
        task.get("completed_at"),
        task.get("failed_at"),
    )
    
    return response


def _fetch_user_watchlist(user_id: str, collections) -> List[Dict[str, Any]]:
    """获取用户自选股列表（含行业信息和最新价格）"""
    data_collections = get_data_db()
    watchlist_col = collections["user_watchlists"]
    stock_info_col = data_collections["stock_info"]
    volume_price_col = data_collections["volume_price"]
    
    watchlist_doc = watchlist_col.find_one({"user_id": user_id})
    watchlist_symbols = watchlist_doc.get("symbols", []) if watchlist_doc else []
    
    watchlist = []
    if watchlist_symbols:
        intraday_price_map = _fetch_latest_intraday_prices(watchlist_symbols, data_collections)
        stock_info_docs = {doc["symbol"]: doc for doc in stock_info_col.find({"symbol": {"$in": watchlist_symbols}})}
        for symbol in watchlist_symbols:
            info = stock_info_docs.get(symbol, {})
            name = info.get("name", "")
            industry = info.get("industry", "")
            
            # Fetch latest daily close/pct_chg (fallback path)
            vp_doc = volume_price_col.find_one({"symbol": symbol}, sort=[("trade_date", -1)])
            latest_daily_close = float(vp_doc.get("close", 0) or 0) if vp_doc else 0
            latest_daily_pct = float(vp_doc.get("pct_chg", 0) or 0) if vp_doc else float(info.get("change_pct", 0) or 0)

            # Prefer intraday latest minute close for today's trigger;
            # fallback to latest daily close (usually previous close).
            intraday_price = intraday_price_map.get(symbol)
            if intraday_price is not None:
                current_price = float(intraday_price)
                if latest_daily_close > 0:
                    change_pct = ((current_price - latest_daily_close) / latest_daily_close) * 100
                else:
                    change_pct = latest_daily_pct
            else:
                current_price = latest_daily_close
                change_pct = latest_daily_pct
            
            watchlist.append({
                "symbol": symbol,
                "name": name,
                "industry": industry,
                "current_price": current_price,
                "change_pct": change_pct
            })
    return watchlist


def _fetch_user_positions(user_id: str, collections) -> List[Dict[str, Any]]:
    """获取用户持仓列表（含行业信息和盈亏）"""
    data_collections = get_data_db()
    positions_col = collections["trader_positions"]
    stock_info_col = data_collections["stock_info"]
    volume_price_col = data_collections["volume_price"]
    
    positions_docs = list(positions_col.find({"user_id": user_id}))
    
    positions = []
    if positions_docs:
        position_symbols = [doc.get("symbol") for doc in positions_docs if doc.get("symbol")]
        intraday_price_map = _fetch_latest_intraday_prices(position_symbols, data_collections)
        stock_info_docs = {doc["symbol"]: doc for doc in stock_info_col.find({"symbol": {"$in": position_symbols}})}
        
        for doc in positions_docs:
            symbol = doc.get("symbol")
            info = stock_info_docs.get(symbol, {})
            
            # QMT sometimes sends qty=0 but can_use_volume has the real value
            raw_qty = doc.get("qty", 0) or 0
            can_use = doc.get("can_use_volume", 0) or 0
            quantity = raw_qty if raw_qty > 0 else can_use
            
            cost_price = doc.get("avg_price", 0) or 0
            intraday_price = intraday_price_map.get(symbol)
            if intraday_price is not None:
                current_price = float(intraday_price)
            else:
                current_price = doc.get("last_price", 0) or 0

            # If broker snapshot has no valid last_price, fallback to latest daily close.
            if current_price == 0 and symbol:
                vp_doc = volume_price_col.find_one({"symbol": symbol}, sort=[("trade_date", -1)])
                if vp_doc:
                    current_price = vp_doc.get("close", 0) or 0

            market_value = doc.get("market_value", 0) or 0
            
            # Derive prices from market_value if direct fields are 0
            if cost_price == 0 and market_value > 0 and quantity > 0:
                cost_price = market_value / quantity
            if current_price == 0 and market_value > 0 and quantity > 0:
                current_price = market_value / quantity
            
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
    return positions


def _fetch_latest_intraday_prices(symbols: List[str], data_collections) -> Dict[str, float]:
    """
    Fetch latest intraday price from today's minute_bars.
    Return mapping: symbol -> latest close.
    """
    if not symbols:
        return {}

    minute_bars_col = data_collections.get("minute_bars")
    if minute_bars_col is None:
        return {}

    today = datetime.now().strftime("%Y%m%d")
    pipeline = [
        {
            "$match": {
                "symbol": {"$in": symbols},
                "trade_date": {"$regex": f"^{today}"}
            }
        },
        {"$sort": {"trade_date": 1}},
        {
            "$group": {
                "_id": "$symbol",
                "close": {"$last": "$close"}
            }
        }
    ]

    try:
        docs = list(minute_bars_col.aggregate(pipeline))
    except Exception:
        return {}

    price_map: Dict[str, float] = {}
    for doc in docs:
        symbol = doc.get("_id")
        close = doc.get("close")
        if symbol is None or close is None:
            continue
        try:
            price_map[str(symbol)] = float(close)
        except (TypeError, ValueError):
            continue
    return price_map


def _submit_portfolio_task(
    current_user: Dict[str, Any],
    watchlist: List[Dict[str, Any]],
    positions: List[Dict[str, Any]],
    ladder_data: Dict[str, Any],
    analysis_type: str,
    collections
) -> str:
    """提交组合分析异步任务"""
    task_doc = build_task_doc(
        current_user=current_user,
        task_type="portfolio_analysis",
        priority=1,
        params={
            "watchlist": watchlist,
            "positions": positions,
            "ladder_data": ladder_data,
            "analysis_type": analysis_type,
        },
        source="manual_trigger",
    )
    
    result = collections["analysis_tasks"].insert_one(task_doc)
    return str(result.inserted_id)


def _fetch_ladder_data_for_portfolio() -> Optional[Dict[str, Any]]:
    """获取连板天梯数据（给 portfolio 分析使用）"""
    data_collections = get_data_db()
    ladder_col = data_collections["limit_up_ladder"]
    daily_col = data_collections["daily_limit_up"]
    
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


@router.get("/analysis-history")
def get_analysis_history(symbol: Optional[str] = None, page: int = 1, limit: int = 50, current_user: dict = Depends(get_current_active_user)):
    """
    获取当前用户指定股票的分析历史。
    如果未提供 `symbol`，将返回当前用户的所有历史（按时间倒序），支持分页（page, limit）。
    """
    return build_user_analysis_history_payload(
        current_user.get("id"),
        symbol=symbol,
        page=page,
        limit=limit,
    )


@router.post("/analyze/deep-analysis")
async def create_deep_analysis_task(
    request: AnalysisTaskCreate,
    current_user: dict = Depends(get_current_active_user)
):
    """
    创建深度分析任务 (Producer)。
    - 权益矩阵：可对「自选股 · 深度分析」按等级配置禁止 / 继承默认配额 / 指定每周次数。
    - 继承默认时：VIP 50 次/月，其余 2 次/周（QuotaManager）。
    """
    collections = get_db_collections()
    quota_manager = QuotaManager(db)

    ent = resolve_deep_analysis_entitlement(current_user)
    _enforce_entitlement_and_quota(
        current_user=current_user,
        entitlement=ent,
        quota_manager=quota_manager,
        feature_label="深度分析",
        on_denied="http",
    )

    # 3. 创建任务
    params: Dict[str, Any] = {}
    params["analysis_mode"] = str(request.analysis_mode.value if hasattr(request.analysis_mode, "value") else request.analysis_mode)
    if request.feedback:
        params["feedback"] = request.feedback
    if request.skip_review:
        params["skip_review"] = True

    task_doc = build_task_doc(
        current_user=current_user,
        task_type="deep_analysis",
        priority=request.priority,
        symbol=request.symbol,
        params=params if params else {},
        provider=request.provider,
        model=request.model,
        source="manual_trigger",
        revision_of=request.revision_of,
        feedback=request.feedback,
    )
    
    try:
        result = collections["analysis_tasks"].insert_one(task_doc)
        task_id = str(result.inserted_id)
        
        # 4. 扣除配额
        remaining = _consume_quota_after_submit(
            current_user=current_user,
            entitlement=ent,
            quota_manager=quota_manager,
        )
        
        return {
            "success": True,
            "task_id": task_id,
            "status": "pending",
            "analysis_mode": params.get("analysis_mode", "classic"),
            "message": "深度分析任务已提交",
            "quota_remaining": remaining,
            "position_in_queue": collections["analysis_tasks"].count_documents({"status": "pending"})
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"任务创建失败: {str(e)}")
@router.get("/analyze/unread-count", summary="获取未读分析数量")
async def get_unread_count(current_user: dict = Depends(get_current_active_user)):
    collections = get_db_collections()
    count = get_analyzer_db()["analysis_history"].count_documents({
        "user_id": current_user["id"],
        "is_read": False,
        "source": "async_task" # Only count async tasks? Or all? Let's count all unread.
    })
    return {"count": count}

@router.post("/analyze/mark-read", summary="标记分析为已读")
async def mark_analysis_read(ids: List[str], current_user: dict = Depends(get_current_active_user)):
    collections = get_db_collections()
    if not ids:
        return {"success": True, "modified_count": 0}
        
    # Convert string IDs to ObjectIds
    obj_ids = []
    for i in ids:
        try:
            obj_ids.append(ObjectId(i))
        except:
            pass
            
    result = get_analyzer_db()["analysis_history"].update_many(
        {
            "_id": {"$in": obj_ids},
            "user_id": current_user["id"]
        },
        {"$set": {"is_read": True}}
    )
    return {"success": True, "modified_count": result.modified_count}
