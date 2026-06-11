"""
数据脉搏 (Data Pulse) — 展示系统实时数据状态

功能:
  1. /data-pulse/news: 国际新闻摘要
  2. /data-pulse/status: 各管线 job_status（目录驱动）
  3. /data-pulse/overview: 新闻 + 同步健康度 + 集合新鲜度
  4. /data-pulse/coverage-gaps: 指定集合在报告期上相对 stock_info 的缺数股票列表
"""

from datetime import datetime, timezone
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query

from db import db, get_data_db
from services.data_monitoring import (
    build_data_contract_health,
    build_data_monitoring_overview,
    build_pipeline_dag,
    build_pipelines_status_rows,
    query_coverage_gaps,
)

router = APIRouter(prefix="/data-pulse", tags=["data-pulse"])
_DATA_PULSE_OVERVIEW_CACHE_TTL_SECONDS = 60
_DATA_PULSE_NEWS_CACHE_TTL_SECONDS = 60
_DATA_PULSE_CONTRACTS_CACHE_TTL_SECONDS = 30
_data_pulse_overview_cache: Dict[str, Any] = {"expires_at": 0.0, "payload": None}
_data_pulse_news_cache: Dict[str, Any] = {"key": None, "expires_at": 0.0, "payload": None}
_data_pulse_contracts_cache: Dict[str, Any] = {"expires_at": 0.0, "payload": None}


def _get_international_news_doc():
    """
    Read cache from quant_data first, fallback to finance for compatibility.
    """
    try:
        doc = get_data_db()["international_news_cache"].find_one({"_id": "latest"})
        if doc:
            return doc
    except Exception:
        pass
    return db["international_news_cache"].find_one({"_id": "latest"})


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
            base = s[:8]
        else:
            base = s[:32]
        pct = row.get("coverage_pct")
        num = row.get("coverage_numerator")
        den = row.get("coverage_denominator")
        if num is not None and den is not None and int(den) > 0:
            out[label] = f"{base} · {int(num)}/{int(den)}"
        elif pct is not None and den is not None and int(den) > 0:
            out[label] = f"{base} · 覆盖 {float(pct):.1f}%"
        else:
            out[label] = base
    return out


@router.get("/coverage-gaps")
async def get_coverage_gaps(
    collection: str = Query(..., max_length=80, description="quant_data 集合名，须出现在目录 coverage 配置中"),
    database: str = Query("quant_data", max_length=32),
    period: Optional[str] = Query(
        None,
        max_length=16,
        description="报告期 YYYYMMDD；省略则使用该集合 freshness 字段的全库最大值",
    ),
    skip: int = Query(0, ge=0, le=500000),
    limit: int = Query(200, ge=1, le=2000),
):
    """
    在指定报告期上，相对 ``stock_info`` 宇宙缺少数据行的股票列表（与覆盖率分子/分母同口径）。

    仅允许访问 ``data_pipelines_catalog.yaml`` 中带 ``coverage`` 的 freshness 集合，避免任意集合扫描。
    """
    try:
        payload = query_coverage_gaps(
            database=database,
            collection=collection,
            period=period,
            skip=skip,
            limit=limit,
        )
    except ValueError as e:
        code = str(e)
        if code == "unknown_coverage_target":
            raise HTTPException(
                status_code=404,
                detail="该 database/collection 未配置覆盖率或不在允许列表中",
            ) from e
        if code in ("invalid_or_missing_period", "coverage_unavailable"):
            raise HTTPException(status_code=400, detail="无法解析报告期或无法计算覆盖") from e
        raise HTTPException(status_code=400, detail=code) from e
    return {"success": True, **payload}


@router.get("/news")
async def get_international_news(
    category: Optional[str] = Query(None, description="指定分类"),
):
    cache_key = category or ""
    now = time.time()
    if (
        _data_pulse_news_cache.get("key") == cache_key
        and _data_pulse_news_cache.get("payload") is not None
        and float(_data_pulse_news_cache.get("expires_at") or 0) > now
    ):
        return _data_pulse_news_cache["payload"]

    doc = _get_international_news_doc()

    if not doc:
        payload = {
            "success": False,
            "message": "暂无新闻数据，数据同步引擎可能尚未运行。",
        }
        _data_pulse_news_cache.update({
            "key": cache_key,
            "payload": payload,
            "expires_at": now + _DATA_PULSE_NEWS_CACHE_TTL_SECONDS,
        })
        return payload

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

    payload = {
        "success": True,
        "fetched_at": fetched_at.strftime("%Y-%m-%d %H:%M UTC") if fetched_at else None,
        "age_minutes": age_minutes,
        "is_fresh": is_fresh,
        "total": doc.get("total", 0),
        "categories": categories,
    }
    _data_pulse_news_cache.update({
        "key": cache_key,
        "payload": payload,
        "expires_at": now + _DATA_PULSE_NEWS_CACHE_TTL_SECONDS,
    })
    return payload


@router.get("/status")
async def get_data_sync_status():
    """Catalog-defined pipelines with latest job_status row per pipeline."""
    rows = build_pipelines_status_rows()
    return {"success": True, "jobs": rows}


@router.get("/overview")
async def get_data_pulse_overview():
    now = time.time()
    if (
        _data_pulse_overview_cache.get("payload") is not None
        and float(_data_pulse_overview_cache.get("expires_at") or 0) > now
    ):
        return _data_pulse_overview_cache["payload"]

    dm = build_data_monitoring_overview()
    freshness_rows = dm.get("freshness") or []

    overview: Dict[str, Any] = {
        "news": None,
        "sync_health": dm.get("sync_health"),
        "data_freshness": _freshness_dict_from_rows(freshness_rows),
        "freshness_detail": freshness_rows,
        "pipelines_detail": dm.get("pipelines"),
    }

    news_doc = _get_international_news_doc()
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

    payload = {"success": True, "overview": overview}
    _data_pulse_overview_cache.update({
        "payload": payload,
        "expires_at": now + _DATA_PULSE_OVERVIEW_CACHE_TTL_SECONDS,
    })
    return payload


@router.get("/contracts")
async def get_data_contract_health():
    """Contract health + cold-start bootstrap progress for the data-pulse panel.

    Source of truth is ``finance.data_contract_state`` written by the
    quant-data-engine reconciler (incl. the ``__bootstrap__`` marker).
    """
    now = time.time()
    if (
        _data_pulse_contracts_cache.get("payload") is not None
        and float(_data_pulse_contracts_cache.get("expires_at") or 0) > now
    ):
        return _data_pulse_contracts_cache["payload"]

    health = build_data_contract_health()
    payload = {"success": True, **health}
    _data_pulse_contracts_cache.update({
        "payload": payload,
        "expires_at": now + _DATA_PULSE_CONTRACTS_CACHE_TTL_SECONDS,
    })
    return payload


@router.get("/dag")
async def get_pipeline_dag(
    date: Optional[str] = Query(
        None,
        max_length=8,
        description="查询日期 YYYYMMDD；省略则用今日北京时间",
    ),
):
    """Return pipeline DAG graph: nodes with today's run status + dependency edges.

    Used by the admin dashboard to render a Blue-Ocean-style pipeline flow view.
    """
    dag = build_pipeline_dag(date=date)
    return {"success": True, **dag}
