"""
数据脉搏 (Data Pulse) — 展示系统实时数据状态

功能:
  1. /data-pulse/news: 国际新闻摘要
  2. /data-pulse/status: 各管线 job_status（目录驱动）
  3. /data-pulse/overview: 新闻 + 同步健康度 + 集合新鲜度
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Query

from db import db
from services.data_monitoring import (
    build_data_monitoring_overview,
    build_pipelines_status_rows,
)

router = APIRouter(prefix="/data-pulse", tags=["data-pulse"])


def _freshness_dict_from_rows(freshness_rows: list) -> Dict[str, Any]:
    """Flatten freshness list to label -> display string (backward compatible)."""
    out: Dict[str, Any] = {}
    for row in freshness_rows:
        label = row.get("label") or row.get("pipeline_id")
        lv = row.get("latest_value")
        if lv is None:
            out[label] = None
            continue
        s = str(lv)
        # YYYYMMDD embedded in ISO or plain
        if len(s) >= 8 and s[:8].isdigit():
            out[label] = s[:8]
        else:
            out[label] = s[:32]
    return out


@router.get("/news")
async def get_international_news(
    category: Optional[str] = Query(None, description="指定分类"),
):
    doc = db["international_news_cache"].find_one({"_id": "latest"})

    if not doc:
        return {
            "success": False,
            "message": "暂无新闻数据，数据同步引擎可能尚未运行。",
        }

    categories = doc.get("categories", {})
    fetched_at = doc.get("fetched_at")

    age_minutes = None
    is_fresh = False
    if fetched_at:
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - fetched_at
        age_minutes = int(age.total_seconds() / 60)
        is_fresh = age_minutes < 120

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
    """Catalog-defined pipelines with latest job_status row per pipeline."""
    rows = build_pipelines_status_rows()
    return {"success": True, "jobs": rows}


@router.get("/overview")
async def get_data_pulse_overview():
    dm = build_data_monitoring_overview()
    freshness_rows = dm.get("freshness") or []

    overview: Dict[str, Any] = {
        "news": None,
        "sync_health": dm.get("sync_health"),
        "data_freshness": _freshness_dict_from_rows(freshness_rows),
        "freshness_detail": freshness_rows,
        "pipelines_detail": dm.get("pipelines"),
    }

    news_doc = db["international_news_cache"].find_one({"_id": "latest"})
    if news_doc:
        fetched_at = news_doc.get("fetched_at")
        age_minutes = None
        if fetched_at:
            if fetched_at.tzinfo is None:
                fetched_at = fetched_at.replace(tzinfo=timezone.utc)
            age_minutes = int((datetime.now(timezone.utc) - fetched_at).total_seconds() / 60)

        categories = news_doc.get("categories", {})
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

    return {"success": True, "overview": overview}
