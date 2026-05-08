"""
连板天梯 API 路由

提供连板天梯数据的 REST API 接口
"""

from typing import Optional, List
from fastapi import APIRouter, Query, Depends, HTTPException
from pydantic import BaseModel
from datetime import datetime, date, timedelta
from auth import get_current_active_user
from db import get_db_collections, get_analyzer_db, get_data_db
from bson import ObjectId
from services.ladder_daily import build_daily_ladder_payload
from services.analysis_tasks import build_task_doc

router = APIRouter(prefix="/ladder", tags=["Limit-Up Ladder"])

class FeedbackRequest(BaseModel):
    date: str
    feedback: str
    is_broken: bool = False



def serialize_doc(doc: dict) -> dict:
    """序列化 MongoDB 文档"""
    if doc is None:
        return None
    result = dict(doc)
    if "_id" in result:
        result["_id"] = str(result["_id"])
    if "created_at" in result and isinstance(result["created_at"], datetime):
        result["created_at"] = result["created_at"].isoformat()
    return result


@router.get("/daily", summary="获取每日连板天梯")
async def get_daily_ladder(
    date: str = Query(None, description="日期 YYYYMMDD，默认今天"),
    current_user: dict = Depends(get_current_active_user)
):
    """获取指定日期的连板天梯数据
    
    返回按梯队分类的涨停股票，以及情绪指标
    如果指定日期无数据，自动回退到最近有数据的交易日
    """
    return build_daily_ladder_payload(date)


@router.get("/compare", summary="历史对比")
async def compare_ladder(
    from_date: str = Query(..., alias="from", description="起始日期 YYYYMMDD"),
    to_date: str = Query(..., alias="to", description="结束日期 YYYYMMDD"),
    current_user: dict = Depends(get_current_active_user)
):
    """对比两天的连板天梯变化
    
    追踪晋级、断板和新进首板
    """
    data_collections = get_data_db()
    daily_col = data_collections["daily_limit_up"]
    
    # 获取两天的数据
    from_stocks = {s["symbol"]: s for s in daily_col.find({"date": from_date})}
    to_stocks = {s["symbol"]: s for s in daily_col.find({"date": to_date})}
    
    if not from_stocks:
        raise HTTPException(400, f"{from_date} 无数据")
    if not to_stocks:
        raise HTTPException(400, f"{to_date} 无数据")
    
    promotions = []  # 晋级
    busts = []  # 断板
    new_entries = []  # 新进首板
    
    # 分析变化
    for symbol, to_stock in to_stocks.items():
        from_stock = from_stocks.get(symbol)
        
        if from_stock:
            # 存在于两天
            if to_stock["streak"] > from_stock["streak"]:
                promotions.append({
                    "symbol": symbol,
                    "name": to_stock["name"],
                    "from_streak": from_stock["streak"],
                    "to_streak": to_stock["streak"],
                    "sector": to_stock.get("primary_sector", "")
                })
        else:
            # 新进
            if to_stock["streak"] == 1:
                new_entries.append({
                    "symbol": symbol,
                    "name": to_stock["name"],
                    "sector": to_stock.get("primary_sector", "")
                })
    
    # 断板：昨天有，今天没有
    for symbol, from_stock in from_stocks.items():
        if symbol not in to_stocks:
            busts.append({
                "symbol": symbol,
                "name": from_stock["name"],
                "from_streak": from_stock["streak"],
                "sector": from_stock.get("primary_sector", "")
            })
    
    # 板块变化统计
    from_sectors = {}
    to_sectors = {}
    
    for s in from_stocks.values():
        sector = s.get("primary_sector", "其他")
        from_sectors[sector] = from_sectors.get(sector, 0) + 1
    
    for s in to_stocks.values():
        sector = s.get("primary_sector", "其他")
        to_sectors[sector] = to_sectors.get(sector, 0) + 1
    
    all_sectors = set(from_sectors.keys()) | set(to_sectors.keys())
    sector_changes = {}
    for sector in all_sectors:
        yesterday = from_sectors.get(sector, 0)
        today = to_sectors.get(sector, 0)
        change = today - yesterday
        sector_changes[sector] = {
            "yesterday": yesterday,
            "today": today,
            "change": f"+{change}" if change > 0 else str(change),
            "trend": "up" if change > 0 else "down" if change < 0 else "stable"
        }
    
    return {
        "success": True,
        "from_date": from_date,
        "to_date": to_date,
        "promotions": promotions,
        "busts": busts,
        "new_entries": new_entries[:20],  # 限制数量
        "sector_changes": sector_changes,
        "summary": {
            "promotion_count": len(promotions),
            "bust_count": len(busts),
            "new_entry_count": len(new_entries)
        }
    }


@router.get("/sectors", summary="板块排名")
async def get_sector_ranking(
    date: str = Query(None, description="日期 YYYYMMDD，默认今天"),
    current_user: dict = Depends(get_current_active_user)
):
    """获取板块强度排名
    
    返回主线板块和板块详细统计
    """
    data_collections = get_data_db()
    sector_col = data_collections["sector_flow"]
    ladder_col = data_collections["limit_up_ladder"]
    
    if not date:
        date = datetime.now().strftime("%Y%m%d")
    
    # Find actual_date with data
    requested_date = date
    actual_date = requested_date

    # 获取板块数据
    sectors = list(sector_col.find({"date": actual_date}).sort("limit_up_count", -1))
    
    # 自动回退逻辑：如果指定日期无数据，找最近的一天
    if not sectors:
        latest_ladder = ladder_col.find_one(
            {"date": {"$lte": requested_date}},
            sort=[("date", -1)]
        )
        if latest_ladder:
            actual_date = latest_ladder["date"]
            sectors = list(sector_col.find({"date": actual_date}).sort("limit_up_count", -1))
            
    if not sectors:
        return {
            "success": False,
            "error": f"{requested_date} 暂无板块数据"
        }
    
    # 获取天梯数据中的排名
    ladder = ladder_col.find_one({"date": actual_date})
    sector_ranking = ladder.get("sector_ranking", []) if ladder else []
    
    # 识别主线
    mainlines = [s["sector"] for s in sectors if s.get("is_mainline")]
    
    sector_list = []
    for s in sectors:
        sector_list.append({
            "name": s["sector"],
            "is_mainline": s.get("is_mainline", False),
            "limit_up_count": s["limit_up_count"],
            "streak_distribution": s.get("streak_distribution", {}),
            "promotion_rate": s.get("promotion_rate", 0),
            "promotions": s.get("promotions", 0)
        })
    
    return {
        "success": True,
        "date": actual_date,
        "requested_date": requested_date,
        "mainlines": mainlines,
        "sectors": sector_list,
        "ranking": sector_ranking
    }


@router.get("/indicators", summary="情绪指标")
async def get_indicators(
    date: str = Query(None, description="日期 YYYYMMDD，默认今天"),
    days: int = Query(5, description="获取最近N天的数据"),
    current_user: dict = Depends(get_current_active_user)
):
    """获取市场情绪指标
    
    返回情绪得分、晋级率、炸板率等
    """
    data_collections = get_data_db()
    ladder_col = data_collections["limit_up_ladder"]
    
    if not date:
        date = datetime.now().strftime("%Y%m%d")
    
    # 获取最近N天数据
    end_dt = datetime.strptime(date, "%Y%m%d")
    start_dt = end_dt - timedelta(days=days * 2)  # 多取一些天数以覆盖非交易日
    
    ladders = list(ladder_col.find({
        "date": {"$gte": start_dt.strftime("%Y%m%d"), "$lte": date}
    }).sort("date", -1).limit(days))
    
    if not ladders:
        return {
            "success": False,
            "error": f"无法获取 {date} 附近的数据"
        }
    
    # 构建趋势数据
    trend_data = []
    for ladder in reversed(ladders):
        indicators = ladder.get("indicators", {})
        trend_data.append({
            "date": ladder["date"],
            "total_limit_up": indicators.get("total_limit_up", 0),
            "sentiment_score": indicators.get("sentiment_score", 50),
            "promotion_rate": indicators.get("promotion_rate_1to2", 0),
            "high_tier_count": indicators.get("high_tier_count", 0),
            "max_streak": indicators.get("max_streak", 0)
        })
    
    # 最新一天的指标
    latest = ladders[0].get("indicators", {})
    
    # 额外获取跌停数以计算比例
    limit_down_col = data_collections["daily_limit_down"]
    total_limit_down = limit_down_col.count_documents({"date": ladders[0]["date"]})
    
    return {
        "success": True,
        "date": date,
        "latest": {
            "sentiment_score": latest.get("sentiment_score", 50),
            "sentiment_level": _get_sentiment_level(latest.get("sentiment_score", 50)),
            "total_limit_up": latest.get("total_limit_up", 0),
            "total_limit_down": total_limit_down,
            "promotion_rate": latest.get("promotion_rate_1to2", 0),
            "high_tier_count": latest.get("high_tier_count", 0),
            "max_streak": latest.get("max_streak", 0)
        },
        "trend": trend_data
    }


def _get_sentiment_level(score: int) -> str:
    """获取情绪等级描述"""
    if score >= 80:
        return "极度亢奋"
    elif score >= 65:
        return "乐观"
    elif score >= 50:
        return "中性"
    elif score >= 35:
        return "谨慎"
    else:
        return "恐慌"


@router.get("/history", summary="历史天梯")
async def get_history(
    start_date: str = Query(..., description="开始日期 YYYYMMDD"),
    end_date: str = Query(..., description="结束日期 YYYYMMDD"),
    current_user: dict = Depends(get_current_active_user)
):
    """获取历史连板天梯数据
    
    用于分析趋势
    """
    data_collections = get_data_db()
    ladder_col = data_collections["limit_up_ladder"]
    
    ladders = list(ladder_col.find({
        "date": {"$gte": start_date, "$lte": end_date}
    }).sort("date", 1))
    
    return {
        "success": True,
        "start_date": start_date,
        "end_date": end_date,
        "count": len(ladders),
        "data": [serialize_doc(l) for l in ladders]
    }


@router.get("/reasoning/{symbol}", summary="获取个股归因详情")
async def get_reasoning_detail(
    symbol: str, 
    date: str = Query(None, description="日期 YYYYMMDD"),
    current_user: dict = Depends(get_current_active_user)
):
    """获取指定股票的 AI 归因详情和新闻上下文"""
    if not date:
        date = datetime.now().strftime("%Y%m%d")
        
    # limit_up_analysis lives in quant_analyzer DB (written by quantAnalyzer)
    analysis_col = get_analyzer_db()["limit_up_analysis"]
    
    log = analysis_col.find_one({"symbol": symbol, "date": date})
    
    if not log:
        return {
            "success": False,
            "error": f"{symbol} on {date} has no detailed analysis"
        }
        
    return {
        "success": True,
        "data": serialize_doc(log)
    }

@router.post("/reasoning/{symbol}/feedback", summary="提交归因反馈并重新分析")
async def submit_reasoning_feedback(
    symbol: str, 
    request: FeedbackRequest,
    current_user: dict = Depends(get_current_active_user)
):
    """提取用户的反馈，创建高优先级的分析任务"""
    collections = get_db_collections()
    tasks_col = collections["analysis_tasks"]
    
    # 优先使用 request.date，如果没有提供则默认为今天
    target_date = request.date
    if not target_date:
        target_date = datetime.now().strftime("%Y%m%d")
        
    task_type = "broken_limit_up_feedback" if request.is_broken else "limit_up_feedback"
    
    task_doc = build_task_doc(
        current_user=current_user,
        task_type=task_type,
        priority=25,
        symbol=symbol,
        params={
            "date": target_date,
            "feedback": request.feedback,
        },
        source="ladder_feedback",
        feedback=request.feedback,
    )
    
    result = tasks_col.insert_one(task_doc)
    
    return {
        "success": True,
        "message": "反馈已提交，正在重新分析",
        "task_id": str(result.inserted_id)
    }


@router.get("/narrative", summary="涨停全景市场动向总结")
async def get_limit_up_narrative(
    date: str = Query(None, description="日期 YYYYMMDD，默认最新有数据的交易日"),
    current_user: dict = Depends(get_current_active_user),
):
    """获取由 LLM 生成的涨停全景市场动向总结。

    包含今日涨停全貌（含炸板）、主线格局、天梯健康度、与昨日的关键变化，以及市场方向解读。
    数据来源：``quant_analyzer.limit_up_daily_narrative``，由
    ``run_limit_up_narrative`` 任务入队、quantAnalyzer 消费后写入。
    """
    _analyzer_cols = get_analyzer_db()
    data_db = get_data_db()
    narrative_col = _analyzer_cols["limit_up_daily_narrative"]

    # Resolve date
    if not date:
        latest_doc = narrative_col.find_one(sort=[("trade_date", -1)])
        if latest_doc:
            date = latest_doc["trade_date"]
        else:
            # Fall back to latest daily_limit_up date
            latest_stock = data_db["daily_limit_up"].find_one(sort=[("date", -1)])
            date = (latest_stock or {}).get("date") or datetime.now().strftime("%Y%m%d")

    doc = narrative_col.find_one({"trade_date": date})

    if not doc:
        # Check if a pending task exists
        task_col = get_db_collections()["analysis_tasks"]
        pending = task_col.find_one(
            {
                "type": "limit_up_daily_narrative",
                "params.trade_date": date,
                "status": {"$in": ["pending", "processing"]},
            }
        )
        if pending:
            return {
                "success": False,
                "trade_date": date,
                "status": "pending",
                "message": "分析正在生成中，请稍后刷新",
            }
        return {
            "success": False,
            "trade_date": date,
            "status": "not_found",
            "message": f"{date} 暂无涨停全景总结，请先运行 run_limit_up_narrative 任务",
        }

    return {
        "success": True,
        "trade_date": doc["trade_date"],
        "headline": doc.get("headline", ""),
        "sentiment_signal": doc.get("sentiment_signal", ""),
        "narrative_markdown": doc.get("narrative_markdown", ""),
        "model": doc.get("model"),
        "created_at": doc["created_at"].isoformat() if isinstance(doc.get("created_at"), datetime) else None,
    }
