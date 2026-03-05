"""
数据脉搏 (Data Pulse) — 展示系统实时数据状态

功能:
  1. /data-pulse/news: 国际新闻摘要（地缘政治/大宗商品/利率等）
  2. /data-pulse/status: 数据同步任务状态（最近一次各任务执行情况）
  3. /data-pulse/overview: 综合概览（新闻 + 同步状态 + 数据新鲜度）

目的: 让用户感知系统是"活的"，数据在实时更新，增加对分析结果的信心。
"""

from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Query
from db import db

router = APIRouter(prefix="/data-pulse", tags=["data-pulse"])


@router.get("/news")
async def get_international_news(
    category: Optional[str] = Query(None, description="指定分类: 地缘政治/大宗商品/央行利率/科技AI/全球经济"),
):
    """
    获取最新的国际新闻摘要。
    数据来源: quant-data-engine 定时抓取的 BBC/CNBC/Yahoo Finance RSS。
    """
    doc = db["international_news_cache"].find_one({"_id": "latest"})

    if not doc:
        return {
            "success": False,
            "message": "暂无新闻数据，数据同步引擎可能尚未运行。"
        }

    categories = doc.get("categories", {})
    fetched_at = doc.get("fetched_at")

    # 数据新鲜度检查
    age_minutes = None
    is_fresh = False
    if fetched_at:
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - fetched_at
        age_minutes = int(age.total_seconds() / 60)
        is_fresh = age_minutes < 120  # 2小时内视为新鲜

    # 按指定分类过滤
    if category and category in categories:
        categories = {category: categories[category]}

    return {
        "success": True,
        "fetched_at": fetched_at.strftime("%Y-%m-%d %H:%M UTC") if fetched_at else None,
        "age_minutes": age_minutes,
        "is_fresh": is_fresh,
        "total": doc.get("total", 0),
        "categories": categories,
    }


@router.get("/status")
async def get_data_sync_status():
    """
    获取各数据管线最近一次执行状态。
    让用户看到各模块数据的最后更新时间、状态。
    """
    job_types = [
        "stock_daily", "index_daily", "etf_daily",
        "financial_sync", "moneyflow", "ladder_sync",
        "market_sentiment", "international_news",
    ]

    job_labels = {
        "stock_daily": "股票日线",
        "index_daily": "指数日线",
        "etf_daily": "ETF日线",
        "financial_sync": "财务数据",
        "moneyflow": "资金流向",
        "ladder_sync": "连板天梯",
        "market_sentiment": "市场情绪",
        "international_news": "国际新闻",
    }

    results = []
    for jt in job_types:
        doc = db["job_status"].find_one(
            {"job_type": jt},
            sort=[("date", -1)]
        )
        if doc:
            payload = doc.get("payload", {})
            results.append({
                "job_type": jt,
                "label": job_labels.get(jt, jt),
                "date": doc.get("date"),
                "status": payload.get("status", "unknown"),
                "records_changed": payload.get("records_changed"),
                "updated_at": doc.get("updated_at").isoformat() if doc.get("updated_at") else None,
            })
        else:
            results.append({
                "job_type": jt,
                "label": job_labels.get(jt, jt),
                "date": None,
                "status": "no_record",
                "records_changed": None,
                "updated_at": None,
            })

    return {"success": True, "jobs": results}


@router.get("/overview")
async def get_data_pulse_overview():
    """
    综合数据脉搏概览 — 前端仪表盘级别的 "系统心跳"。
    包含: 新闻条数 + 最近同步状态 + 各集合数据新鲜度。
    """
    overview = {
        "news": None,
        "sync_health": None,
        "data_freshness": {},
    }

    # 1. 新闻概要
    news_doc = db["international_news_cache"].find_one({"_id": "latest"})
    if news_doc:
        fetched_at = news_doc.get("fetched_at")
        age_minutes = None
        if fetched_at:
            if fetched_at.tzinfo is None:
                fetched_at = fetched_at.replace(tzinfo=timezone.utc)
            age_minutes = int((datetime.now(timezone.utc) - fetched_at).total_seconds() / 60)

        categories = news_doc.get("categories", {})
        # 取每个分类的第一条作为简报
        headlines = {}
        for cat, items in categories.items():
            if items:
                headlines[cat] = items[0].get("title", "")

        overview["news"] = {
            "total": news_doc.get("total", 0),
            "age_minutes": age_minutes,
            "is_fresh": age_minutes is not None and age_minutes < 120,
            "headlines": headlines,
        }

    # 2. 数据同步健康度（最近的 job_status）
    recent_jobs = list(db["job_status"].aggregate([
        {"$sort": {"date": -1}},
        {"$group": {
            "_id": "$job_type",
            "latest_date": {"$first": "$date"},
            "status": {"$first": "$payload.status"},
            "updated_at": {"$first": "$updated_at"},
        }}
    ]))

    total_jobs = len(recent_jobs)
    healthy_jobs = sum(1 for j in recent_jobs if j.get("status") == "complete")
    overview["sync_health"] = {
        "total_pipelines": total_jobs,
        "healthy": healthy_jobs,
        "health_pct": round(healthy_jobs / total_jobs * 100) if total_jobs > 0 else 0,
        "jobs": [
            {
                "type": j["_id"],
                "latest_date": j.get("latest_date"),
                "status": j.get("status", "unknown"),
            } for j in recent_jobs
        ]
    }

    # 3. 关键集合的数据新鲜度（最新 trade_date）
    freshness_checks = {
        "volume_price": {"key": "trade_date", "label": "股票日线"},
        "etf_daily": {"key": "trade_date", "label": "ETF日线"},
        "moneyflow": {"key": "trade_date", "label": "资金流向"},
    }

    for coll_name, cfg in freshness_checks.items():
        try:
            latest = db[coll_name].find_one(
                {}, sort=[(cfg["key"], -1)], projection={cfg["key"]: 1, "_id": 0}
            )
            overview["data_freshness"][cfg["label"]] = latest.get(cfg["key"]) if latest else None
        except Exception:
            overview["data_freshness"][cfg["label"]] = None

    return {"success": True, "overview": overview}
