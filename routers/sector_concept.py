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
from services.analysis_tasks import build_task_doc, can_access_task

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
    task_doc = build_task_doc(
        current_user=current_user,
        task_type="sector_concept_analysis",
        priority=20,
        params={
            "date": trade_date,
            "source": source,
        },
        source="manual_trigger",
    )
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
    if not can_access_task(task, current_user):
        raise HTTPException(403, "无权访问此任务")

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
    elif task.get("status") == "completed_with_parse_error":
        result = task.get("result", {})
        response["analysis"] = result.get("analysis")
        response["source"] = result.get("source")
        response["source_name"] = result.get("source_name")
        response["trade_date"] = result.get("trade_date")
        response["model"] = result.get("model")
        response["data_summary"] = result.get("data_summary")
        response["error"] = (
            task.get("error") or result.get("error_detail") or result.get("error") or "结果解析失败"
        )
        response["completed_at"] = (
            task.get("completed_at").isoformat() if task.get("completed_at") else None
        )
    elif task.get("status") == "failed":
        response["error"] = task.get("error", "Unknown error")

    return response


@router.get("/latest", summary="获取最新分析结果")
async def get_latest_analysis(
    source: str = Query("dc", description="数据源: dc 或 ths"),
    date: str = Query(None, description="交易日期 YYYYMMDD，不传则返回最近一条"),
    current_user: dict = Depends(get_current_active_user),
):
    """Get the latest completed analysis for a given source, optionally filtered by date."""
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


@router.get("/tree", summary="获取热点题材树")
async def get_hot_topic_tree(
    mode: str = Query("auto", description="口径: auto / intraday / eod"),
    date: str = Query(None, description="交易日期 YYYYMMDD，不传则取最近交易日"),
    current_user: dict = Depends(get_current_active_user),
):
    """热点题材树：父层(申万一级) -> 叶子(题材) -> 催化事件。

    口径：
    - ``intraday``：盘中实时，来自 quant_data 的涨停天梯快照，申万行业聚类；
    - ``eod``：收盘定稿，来自 Tushare 东财概念板块（定时任务预生成，本接口只读）；
    - ``auto``：优先 eod，没有就降级 intraday，并在响应里标明降级原因。

    盘中与收盘两份都保留，可通过 ``GET /sector-concept/tree/snapshots`` 比对。
    """
    from services.hot_topic_tree import (
        build_intraday_tree,
        list_snapshots,
        load_latest_available,
        load_tree,
    )

    mode = (mode or "auto").lower()
    if mode not in ("auto", "intraday", "eod"):
        raise HTTPException(400, "mode must be 'auto', 'intraday' or 'eod'")

    trade_date = (date or "").replace("-", "")

    # 未指定日期时，取最近的交易日（有快照的那个）
    if not trade_date:
        today = datetime.now().strftime("%Y%m%d")
        from db import get_data_db

        cal = get_data_db()["trade_calendar"]
        try:
            from utils.latest_sse_open_day import latest_sse_open_day

            trade_date = latest_sse_open_day(cal, today)
        except Exception:
            trade_date = today

    fallback_reason = ""
    tree = None
    if mode in ("auto", "eod"):
        tree = load_tree("eod", trade_date)
        if tree is None and mode == "eod":
            return {
                "success": False,
                "error": f"{trade_date} 暂无收盘口径题材树（定时任务 16:20 生成）",
                "trade_date": trade_date,
                "mode": "eod",
            }
        if tree is None:
            fallback_reason = "收盘口径尚未生成，先用盘中口径"

    if tree is None:
        tree = build_intraday_tree(trade_date)
        if tree is None:
            # 当天没有（可能选到非交易日，或盘中快照还没落库）→ 回退到最近一份
            tree = load_latest_available("intraday", trade_date)
            if tree is None:
                return {
                    "success": False,
                    "error": f"{trade_date} 暂无题材树数据",
                    "trade_date": trade_date,
                    "mode": mode,
                }
            fallback_reason = f"{trade_date} 无数据，显示最近一份（{tree.get('trade_date')}）"
            tree = {**tree, "meta": {**(tree.get("meta") or {}), "trade_date": tree.get("trade_date")}}
            trade_date = tree.get("trade_date") or trade_date
        else:
            tree = {**tree, "meta": {**(tree.get("meta") or {}), "trade_date": trade_date}}

    meta = tree.get("meta") or {}
    return {
        "success": True,
        "mode": meta.get("mode"),
        "trade_date": meta.get("trade_date") or trade_date,
        "branches": tree.get("branches") or [],
        "stats": tree.get("stats") or {},
        "meta": meta,
        "generated_at": tree.get("generated_at"),
        "fallback_reason": fallback_reason,
        "snapshots": list_snapshots(trade_date),
    }


@router.get("/tree/snapshots", summary="列出当日题材树快照")
async def get_hot_topic_tree_snapshots(
    date: str = Query(None, description="交易日期 YYYYMMDD，不传则取今天"),
    current_user: dict = Depends(get_current_active_user),
):
    """列出某日已有的题材树快照（盘中各时点 + 收盘定稿），用于切换与比对。"""
    from services.hot_topic_tree import list_snapshots

    trade_date = (date or datetime.now().strftime("%Y%m%d")).replace("-", "")
    return {
        "success": True,
        "trade_date": trade_date,
        "snapshots": list_snapshots(trade_date),
    }
