"""A-share market regime report API."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from auth import get_current_active_user
from db import get_data_db

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
    return [_serialize(doc) for doc in docs if doc]


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
    limit: int = Query(30, ge=1, le=180, description="返回最近 N 条，最多 180"),
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