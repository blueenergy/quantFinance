"""
热股分析 API 路由

THS / DC 热股榜单实时分析接口。
手动触发 + 定时轮询，增量检测新数据后触发 LLM 分析。
"""

from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Query, Depends, HTTPException
from pydantic import BaseModel
from auth import get_current_active_user
from db import get_db_collections
from bson import ObjectId

router = APIRouter(prefix="/hot-stock", tags=["热股分析"])


class AnalyzeRequest(BaseModel):
    source: str = "ths"          # "ths" or "dc"
    force: bool = False           # Force re-analyze even if same rank_time


@router.post("/analyze", summary="手动触发热股分析")
async def submit_hot_stock_analysis(
    request: AnalyzeRequest,
    current_user: dict = Depends(get_current_active_user),
):
    """Submit a hot stock analysis task (async)."""
    collections = get_db_collections()
    tasks_col = collections["analysis_tasks"]

    source = request.source.lower()
    if source not in ("ths", "dc"):
        raise HTTPException(400, "source must be 'ths' or 'dc'")

    # Check already running
    existing = tasks_col.find_one({
        "type": "hot_stock_analysis",
        "params.source": source,
        "status": {"$in": ["pending", "processing"]},
    })
    if existing:
        return {
            "success": True,
            "task_id": str(existing["_id"]),
            "message": "已有热股分析任务正在处理中",
            "status": existing["status"],
        }

    # Get latest rank_time from DB (for dedup — skip if unchanged)
    analysis_col = collections["hot_stock_analysis"]
    last_doc = analysis_col.find_one({"source": source}, sort=[("created_at", -1)])
    last_rank_time = last_doc.get("rank_time") if last_doc else None

    task_doc = {
        "user_id": current_user.get("username", "unknown"),
        "type": "hot_stock_analysis",
        "priority": 20,
        "status": "pending",
        "params": {
            "source": source,
            "last_rank_time": None if request.force else last_rank_time,
        },
        "created_at": datetime.now(),
        "updated_at": datetime.now(),
    }
    result = tasks_col.insert_one(task_doc)

    return {
        "success": True,
        "task_id": str(result.inserted_id),
        "message": f"热股分析任务已提交 ({source.upper()})",
        "status": "pending",
    }


@router.get("/task/{task_id}", summary="查询分析任务状态")
async def get_task_status(
    task_id: str,
    current_user: dict = Depends(get_current_active_user),
):
    """Poll task status."""
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
        response["skip"] = result.get("skip", False)
        response["analysis"] = result.get("analysis")
        response["source"] = result.get("source")
        response["source_name"] = result.get("source_name")
        response["rank_time"] = result.get("rank_time")
        response["trade_date"] = result.get("trade_date")
        response["model"] = result.get("model")
        response["stock_count"] = result.get("stock_count")
        response["completed_at"] = (
            task.get("completed_at").isoformat() if task.get("completed_at") else None
        )
    elif task["status"] == "failed":
        response["error"] = task.get("error", "Unknown error")

    return response


@router.get("/latest", summary="获取最新热股分析结果")
async def get_latest_analysis(
    source: str = Query("ths", description="数据源: ths 或 dc"),
    current_user: dict = Depends(get_current_active_user),
):
    """Get the most recent hot stock analysis."""
    collections = get_db_collections()
    analysis_col = collections["hot_stock_analysis"]

    source = source.lower()
    if source not in ("ths", "dc"):
        raise HTTPException(400, "source must be 'ths' or 'dc'")

    doc = analysis_col.find_one({"source": source}, sort=[("created_at", -1)])
    if not doc:
        return {
            "success": False,
            "error": f"No {source.upper()} analysis found. Please trigger an analysis first.",
        }

    return {
        "success": True,
        "source": source,
        "source_name": doc.get("source_name"),
        "rank_time": doc.get("rank_time"),
        "trade_date": doc.get("trade_date"),
        "analysis": doc.get("analysis"),
        "model": doc.get("model"),
        "stock_count": doc.get("stock_count"),
        "created_at": doc.get("created_at").isoformat() if doc.get("created_at") else None,
    }


@router.get("/history", summary="获取当日所有分析记录")
async def get_today_history(
    source: str = Query("ths", description="数据源: ths 或 dc"),
    date: str = Query(None, description="交易日期 YYYYMMDD，不传则为今日"),
    current_user: dict = Depends(get_current_active_user),
):
    """Get all analysis records for a given trade date (for timeline view)."""
    collections = get_db_collections()
    analysis_col = collections["hot_stock_analysis"]

    source = source.lower()
    if source not in ("ths", "dc"):
        raise HTTPException(400, "source must be 'ths' or 'dc'")

    trade_date = date or datetime.now().strftime("%Y%m%d")

    docs = list(
        analysis_col.find(
            {"source": source, "trade_date": trade_date},
            sort=[("created_at", 1)],
            projection={"analysis": 1, "rank_time": 1, "created_at": 1, "stock_count": 1, "source_name": 1},
        )
    )

    records = []
    for doc in docs:
        records.append({
            "id": str(doc["_id"]),
            "rank_time": doc.get("rank_time"),
            "stock_count": doc.get("stock_count"),
            "analysis": doc.get("analysis"),
            "source_name": doc.get("source_name"),
            "created_at": doc.get("created_at").isoformat() if doc.get("created_at") else None,
        })

    return {
        "success": True,
        "source": source,
        "trade_date": trade_date,
        "count": len(records),
        "records": records,
    }
