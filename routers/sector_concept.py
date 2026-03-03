"""
概念板块分析 API 路由

提供东方财富(DC)和同花顺(THS)概念板块分析的 REST API 接口。
异步任务模式：提交 → 轮询 → 获取结果。
"""

from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Query, Depends, HTTPException
from pydantic import BaseModel
from auth import get_current_active_user
from db import get_db_collections, get_analyzer_db
from bson import ObjectId

router = APIRouter(prefix="/sector-concept", tags=["概念板块分析"])


class AnalyzeRequest(BaseModel):
    date: Optional[str] = None  # YYYYMMDD or YYYY-MM-DD, defaults to today
    source: str = "dc"          # "dc" or "ths"


def serialize_doc(doc: dict) -> dict:
    """Serialize MongoDB doc."""
    if doc is None:
        return None
    result = dict(doc)
    if "_id" in result:
        result["_id"] = str(result["_id"])
    for key in ("created_at", "completed_at", "failed_at", "started_at"):
        if key in result and isinstance(result[key], datetime):
            result[key] = result[key].isoformat()
    return result


@router.post("/analyze", summary="提交概念板块分析任务")
async def submit_sector_analysis(
    request: AnalyzeRequest,
    current_user: dict = Depends(get_current_active_user),
):
    """
    Submit a sector concept analysis task (async).
    
    - source: "dc" (东方财富) or "ths" (同花顺)
    - date: Trade date (YYYYMMDD), defaults to today
    """
    collections = get_db_collections()
    tasks_col = collections["analysis_tasks"]

    source = request.source.lower()
    if source not in ("dc", "ths"):
        raise HTTPException(400, "source must be 'dc' or 'ths'")

    trade_date = request.date
    if not trade_date:
        trade_date = datetime.now().strftime("%Y%m%d")
    trade_date = trade_date.replace("-", "")

    # Check if a task is already running for same date + source
    existing = tasks_col.find_one({
        "type": "sector_concept_analysis",
        "params.date": trade_date,
        "params.source": source,
        "status": {"$in": ["pending", "processing"]},
    })
    if existing:
        return {
            "success": True,
            "task_id": str(existing["_id"]),
            "message": "已有相同分析任务正在处理中",
            "status": existing["status"],
        }

    # Insert new task
    task_doc = {
        "user_id": current_user.get("username", "unknown"),
        "type": "sector_concept_analysis",
        "priority": 50,
        "status": "pending",
        "params": {
            "date": trade_date,
            "source": source,
        },
        "created_at": datetime.now(),
        "updated_at": datetime.now(),
    }
    result = tasks_col.insert_one(task_doc)

    return {
        "success": True,
        "task_id": str(result.inserted_id),
        "message": f"分析任务已提交 ({source.upper()}, {trade_date})",
        "status": "pending",
    }


@router.get("/task/{task_id}", summary="查询分析任务状态")
async def get_task_status(
    task_id: str,
    current_user: dict = Depends(get_current_active_user),
):
    """Poll task status and get result when completed."""
    collections = get_db_collections()
    tasks_col = collections["analysis_tasks"]

    try:
        task = tasks_col.find_one({"_id": ObjectId(task_id)})
    except Exception:
        raise HTTPException(400, "Invalid task_id")

    if not task:
        raise HTTPException(404, "Task not found")

    response = {
        "success": True,
        "task_id": task_id,
        "status": task.get("status"),
        "created_at": task.get("created_at").isoformat() if task.get("created_at") else None,
    }

    if task["status"] == "completed":
        result = task.get("result", {})
        response["analysis"] = result.get("analysis")
        response["source"] = result.get("source")
        response["source_name"] = result.get("source_name")
        response["trade_date"] = result.get("trade_date")
        response["model"] = result.get("model")
        response["data_summary"] = result.get("data_summary")
        response["completed_at"] = (
            task.get("completed_at").isoformat() if task.get("completed_at") else None
        )
    elif task["status"] == "failed":
        response["error"] = task.get("error", "Unknown error")

    return response


@router.get("/latest", summary="获取最新分析结果")
async def get_latest_analysis(
    source: str = Query("dc", description="数据源: dc 或 ths"),
    date: str = Query(None, description="交易日期 YYYYMMDD，不传则返回最近一条"),
    current_user: dict = Depends(get_current_active_user),
):
    """Get the latest completed analysis for a given source, optionally filtered by date."""
    collections = get_db_collections()
    analysis_col = get_analyzer_db()["sector_concept_analysis"]

    source = source.lower()
    if source not in ("dc", "ths"):
        raise HTTPException(400, "source must be 'dc' or 'ths'")

    query = {"source": source}
    if date:
        trade_date = date.replace("-", "")
        query["trade_date"] = trade_date

    doc = analysis_col.find_one(query, sort=[("created_at", -1)])

    if not doc:
        date_hint = f" for {date}" if date else ""
        return {
            "success": False,
            "error": f"No {source.upper()} analysis found{date_hint}. Please trigger an analysis first.",
        }

    return {
        "success": True,
        "source": source,
        "trade_date": doc.get("trade_date"),
        "analysis": doc.get("analysis"),
        "model": doc.get("model"),
        "data_summary": doc.get("data_summary"),
        "created_at": doc.get("created_at").isoformat() if doc.get("created_at") else None,
    }
