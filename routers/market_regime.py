"""A-share market regime report API."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from auth import get_current_active_user
from db import get_analyzer_db, get_data_db
import db as _app_db
from services.analysis_tasks import build_task_doc

router = APIRouter(prefix="/market-regime", tags=["A股广度估值扫描"])


def _serialize(doc: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not doc:
        return None
    out = dict(doc)
    out.pop("_id", None)
    for key, value in list(out.items()):
        if isinstance(value, datetime):
            out[key] = value.isoformat()
    return out


def _serialize_list(docs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for doc in docs:
        item = _serialize(doc)
        if item is not None:
            items.append(item)
    return items


@router.get("/analysis/latest", summary="最新A股广度估值 AI 解读")
async def get_market_regime_analysis_latest(
    current_user: dict = Depends(get_current_active_user),
):
    coll = get_analyzer_db()["market_regime_analysis"]
    doc = coll.find_one({}, {"_id": 0}, sort=[("analysis_date", -1)])
    if not doc:
        raise HTTPException(404, "暂无 A 股广度估值 AI 解读，请先触发分析")
    return _serialize(doc)


@router.get("/analysis/history", summary="A股广度估值 AI 解读历史")
async def get_market_regime_analysis_history(
    limit: int = Query(10, ge=1, le=60, description="返回最近 N 条，最多 60"),
    current_user: dict = Depends(get_current_active_user),
):
    coll = get_analyzer_db()["market_regime_analysis"]
    docs = list(coll.find({}, {"_id": 0}).sort("analysis_date", -1).limit(limit))
    return {"data": _serialize_list(docs)}


@router.post("/analysis/trigger", summary="触发A股广度估值 AI 解读")
async def trigger_market_regime_analysis(
    date: Optional[str] = Query(None, description="扫描日期 YYYYMMDD，默认最新日报"),
    force: bool = Query(False, description="强制重新分析（即使当日已有任务）"),
    current_user: dict = Depends(get_current_active_user),
):
    data_coll = get_data_db()["market_regime_reports"]
    report = data_coll.find_one({"scan_date": date}, {"_id": 0}) if date else data_coll.find_one({}, {"_id": 0}, sort=[("scan_date", -1)])
    if not report:
        raise HTTPException(404, "暂无 A 股广度估值扫描数据，无法触发 AI 解读")

    analysis_date = str(report.get("scan_date") or date)
    if not force:
        existing_task = _app_db.db["analysis_tasks"].find_one({
            "type": "market_regime_analysis",
            "params.date": analysis_date,
            "status": {"$in": ["pending", "processing"]},
        })
        if existing_task:
            return {
                "task_id": str(existing_task["_id"]),
                "status": existing_task.get("status", "pending"),
                "analysis_date": analysis_date,
                "message": "当日广度估值 AI 解读任务已在队列中",
            }

    task_doc = build_task_doc(
        current_user=current_user,
        task_type="market_regime_analysis",
        priority=5,
        params={"date": analysis_date},
        source="manual_trigger",
    )
    result = _app_db.db["analysis_tasks"].insert_one(task_doc)
    return {
        "task_id": str(result.inserted_id),
        "status": "pending",
        "analysis_date": analysis_date,
    }


@router.get("/latest", summary="最新A股广度估值扫描日报")
async def get_latest_market_regime(
    current_user: dict = Depends(get_current_active_user),
):
    coll = get_data_db()["market_regime_reports"]
    doc = coll.find_one({}, {"_id": 0}, sort=[("scan_date", -1)])
    if not doc:
        raise HTTPException(404, "暂无 A 股广度估值扫描数据")
    return _serialize(doc)


@router.get("/detail", summary="指定日期A股广度估值扫描日报")
async def get_market_regime_detail(
    date: str = Query(..., description="日期 YYYYMMDD"),
    current_user: dict = Depends(get_current_active_user),
):
    coll = get_data_db()["market_regime_reports"]
    doc = coll.find_one({"scan_date": date}, {"_id": 0})
    if not doc:
        raise HTTPException(404, f"{date} 暂无 A 股广度估值扫描数据")
    return _serialize(doc)


@router.get("/history", summary="A股广度估值扫描历史列表")
async def get_market_regime_history(
    limit: int = Query(30, ge=1, le=500, description="返回最近 N 条，最多 500"),
    start: Optional[str] = Query(None, description="起始日期 YYYYMMDD；指定后按区间查询"),
    end: Optional[str] = Query(None, description="结束日期 YYYYMMDD"),
    current_user: dict = Depends(get_current_active_user),
):
    coll = get_data_db()["market_regime_reports"]
    projection = {"_id": 0}
    if start:
        end_date = end or datetime.today().strftime("%Y%m%d")
        docs = list(coll.find({"scan_date": {"$gte": start, "$lte": end_date}}, projection).sort("scan_date", 1))
    else:
        docs = list(coll.find({}, projection).sort("scan_date", -1).limit(limit))
        docs.reverse()
    return {"data": _serialize_list(docs)}


@router.get("/breadth", summary="A股广度指标历史")
async def get_market_breadth_history(
    limit: int = Query(90, ge=1, le=500, description="返回最近 N 条，最多 500"),
    start: Optional[str] = Query(None, description="起始日期 YYYYMMDD；指定后按区间查询"),
    end: Optional[str] = Query(None, description="结束日期 YYYYMMDD"),
    current_user: dict = Depends(get_current_active_user),
):
    coll = get_data_db()["market_breadth_daily"]
    projection = {"_id": 0}
    if start:
        end_date = end or datetime.today().strftime("%Y%m%d")
        docs = list(coll.find({"trade_date": {"$gte": start, "$lte": end_date}}, projection).sort("trade_date", 1))
    else:
        docs = list(coll.find({}, projection).sort("trade_date", -1).limit(limit))
        docs.reverse()
    return {"data": _serialize_list(docs)}


@router.get("/cape", summary="指数CAPE历史")
async def get_index_cape_history(
    index_code: str = Query("000300.SH", description="指数代码，如 000300.SH"),
    limit: int = Query(180, ge=1, le=1000, description="返回最近 N 条，最多 1000"),
    start: Optional[str] = Query(None, description="起始日期 YYYYMMDD；指定后按区间查询"),
    end: Optional[str] = Query(None, description="结束日期 YYYYMMDD"),
    current_user: dict = Depends(get_current_active_user),
):
    coll = get_data_db()["index_cape_daily"]
    projection = {"_id": 0}
    query: Dict[str, Any] = {"index_code": index_code}
    if start:
        end_date = end or datetime.today().strftime("%Y%m%d")
        query["trade_date"] = {"$gte": start, "$lte": end_date}
        docs = list(coll.find(query, projection).sort("trade_date", 1))
    else:
        docs = list(coll.find(query, projection).sort("trade_date", -1).limit(limit))
        docs.reverse()
    return {"index_code": index_code, "data": _serialize_list(docs)}


__all__ = ["router"]