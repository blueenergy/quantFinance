from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query

from auth import get_current_active_user
from db import get_analyzer_db, get_db_collections
from services.analysis_tasks import (
    can_access_task,
    build_task_doc,
    compute_queue_ahead,
    estimate_task_duration_seconds,
    estimate_wait_seconds,
    format_dt,
    infer_task_data_date,
    infer_task_type,
    normalize_task_status,
    queue_wait_seconds_to_text,
    summarize_task_result,
)


router = APIRouter(prefix="/analysis-tasks", tags=["分析任务中心"])
ANALYSIS_WORKER_PARALLELISM = max(1, int(os.getenv("ANALYSIS_WORKER_PARALLELISM", "1")))
PROCESSING_STALE_MINUTES = max(5, int(os.getenv("ANALYSIS_TASK_STALE_MINUTES", "60")))


RENDER_TYPE_MAP = {
    "deep_analysis": "deep_analysis",
    "portfolio_analysis": "portfolio_analysis",
    "hot_stock_analysis": "hot_stock_analysis",
    "sector_concept_analysis": "sector_concept_analysis",
    "etf_analysis": "etf_analysis",
    "global_analysis": "global_analysis",
    "market_analysis": "market_analysis",
    "limit_up_feedback": "ladder_feedback",
    "broken_limit_up_feedback": "ladder_feedback",
    "earnings_hunter": "earnings_hunter",
    "risk_report_analysis": "risk_report_analysis",
    "x_influencer_post_analysis": "x_influencer_post_analysis",
}


PARTIAL_TYPES = {
    "portfolio_analysis",
    "hot_stock_analysis",
    "sector_concept_analysis",
    "etf_analysis",
    "global_analysis",
    "market_analysis",
    "limit_up_feedback",
    "broken_limit_up_feedback",
    "risk_report_analysis",
    "x_influencer_post_analysis",
}


def _serialize_doc(doc: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not doc:
        return None
    out = dict(doc)
    if "_id" in out:
        out["_id"] = str(out["_id"])
    for key, value in list(out.items()):
        if isinstance(value, datetime):
            out[key] = value.isoformat()
    return out


def _build_user_filter(current_user: Dict[str, Any]) -> Dict[str, Any]:
    if current_user.get("is_admin"):
        return {}
    user_id = str(current_user.get("id") or "")
    username = str(current_user.get("username") or "")
    or_filters = []
    if user_id:
        or_filters.extend(
            [
                {"created_by_user_id": user_id},
                {"user_id": user_id},
            ]
        )
    if username:
        or_filters.extend(
            [
                {"created_by_username": username},
                {"user_id": username},
            ]
        )
    if not or_filters:
        return {"_id": None}
    return {"$or": or_filters}


def _get_render_type(task_type: str) -> str:
    return RENDER_TYPE_MAP.get(task_type, "fallback")


def _fetch_extra_result(task: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    analyzer = get_analyzer_db()
    task_type = infer_task_type(task)
    params = task.get("params", {}) if isinstance(task.get("params"), dict) else {}
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    symbol = task.get("symbol")
    user_id = task.get("created_by_user_id") or task.get("user_id")
    data_date = infer_task_data_date(task)

    if task_type == "hot_stock_analysis":
        query: Dict[str, Any] = {"source": params.get("source", "ths")}
        if result.get("rank_time"):
            query["rank_time"] = result.get("rank_time")
        elif data_date:
            query["trade_date"] = data_date.replace("-", "")
        elif params.get("date"):
            query["trade_date"] = str(params.get("date")).replace("-", "")
        return _serialize_doc(analyzer["hot_stock_analysis"].find_one(query, sort=[("created_at", -1)]))
    if task_type == "sector_concept_analysis":
        query = {"source": params.get("source", "dc")}
        if data_date:
            query["trade_date"] = data_date.replace("-", "")
        elif params.get("date"):
            query["trade_date"] = str(params.get("date")).replace("-", "")
        return _serialize_doc(analyzer["sector_concept_analysis"].find_one(query, sort=[("created_at", -1)]))
    if task_type == "etf_analysis":
        sym = symbol or params.get("symbol")
        q_etf: Dict[str, Any] = {"ts_code": sym} if sym else {}
        if data_date:
            q_etf["trade_date"] = data_date.replace("-", "")
        if not q_etf:
            return None
        return _serialize_doc(analyzer["etf_analysis"].find_one(q_etf, sort=[("created_at", -1)]))
    if task_type == "portfolio_analysis":
        query = {"user_id": user_id}
        if params.get("analysis_type"):
            query["type"] = params["analysis_type"]
        ladder = params.get("ladder_data") if isinstance(params.get("ladder_data"), dict) else {}
        ld = ladder.get("date")
        if ld:
            query["ladder_date"] = ld
        elif data_date:
            query["ladder_date"] = data_date.replace("-", "")
        return _serialize_doc(analyzer["portfolio_analysis"].find_one(query, sort=[("created_at", -1)]))
    if task_type == "global_analysis":
        return _serialize_doc(analyzer["global_analysis"].find_one(sort=[("created_at", -1), ("_id", -1)]))
    if task_type == "market_analysis":
        analysis_date = data_date or params.get("effective_analysis_date") or params.get("date")
        if not analysis_date:
            return None
        return _serialize_doc(
            analyzer["market_analysis_cache"].find_one({"analysis_date": analysis_date}, sort=[("created_at", -1)])
        )
    if task_type in {"limit_up_feedback", "broken_limit_up_feedback"}:
        if not symbol:
            return None
        query = {"symbol": symbol}
        if data_date:
            query["date"] = data_date.replace("-", "")
        elif params.get("date"):
            query["date"] = str(params.get("date")).replace("-", "")
        return _serialize_doc(analyzer["limit_up_analysis"].find_one(query, sort=[("updated_at", -1)]))
    if task_type == "deep_analysis":
        if not symbol:
            return None
        query = {"symbol": symbol}
        if user_id:
            query["user_id"] = user_id
        return _serialize_doc(analyzer["analysis_history"].find_one(query, sort=[("timestamp", -1), ("_id", -1)]))
    return None


@router.get("", summary="获取当前用户分析任务列表")
async def list_analysis_tasks(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=200),
    status: Optional[str] = Query(None),
    task_type: Optional[str] = Query(None, alias="type"),
    symbol: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None, description="YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="YYYY-MM-DD"),
    include_market_global: bool = Query(
        False,
        description="为 true 时列表包含 global_analysis / market_analysis；默认 false 隐藏二者",
    ),
    current_user: dict = Depends(get_current_active_user),
):
    collections = get_db_collections()
    tasks_col = collections["analysis_tasks"]

    query: Dict[str, Any] = _build_user_filter(current_user)
    if status:
        query["status"] = normalize_task_status(status)
    if task_type:
        query.setdefault("$and", [])
        query["$and"].append({"$or": [{"task_type": task_type}, {"type": task_type}]})
    elif not include_market_global:
        query.setdefault("$and", [])
        query["$and"].append(
            {
                "$nor": [
                    {"$or": [{"task_type": "global_analysis"}, {"type": "global_analysis"}]},
                    {"$or": [{"task_type": "market_analysis"}, {"type": "market_analysis"}]},
                ]
            }
        )
    if symbol:
        query["symbol"] = symbol.upper()
    if start_date or end_date:
        created_range: Dict[str, Any] = {}
        try:
            if start_date:
                created_range["$gte"] = datetime.fromisoformat(start_date)
            if end_date:
                created_range["$lte"] = datetime.fromisoformat(end_date)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"日期格式无效，应为 YYYY-MM-DD: {exc}")
        query["created_at"] = created_range

    skip = (page - 1) * limit
    total = tasks_col.count_documents(query)
    docs = list(tasks_col.find(query).sort("created_at", -1).skip(skip).limit(limit))

    items = []
    for task in docs:
        task_id = str(task.get("_id"))
        ttype = infer_task_type(task)
        items.append(
            {
                "task_id": task_id,
                "task_type": ttype,
                "status": normalize_task_status(task.get("status")),
                "symbol": task.get("symbol"),
                "stock_name": task.get("stock_name"),
                "created_at": format_dt(task.get("created_at")),
                "updated_at": format_dt(task.get("updated_at")),
                "completed_at": format_dt(task.get("completed_at")),
                "data_date": infer_task_data_date(task),
                "error": task.get("error"),
                "result_summary": summarize_task_result(task),
                "render_type": _get_render_type(ttype),
                "revision_of": task.get("revision_of"),
            }
        )

    return {
        "success": True,
        "items": items,
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
        },
    }


def _queue_status_hint(status: str, estimated_wait_seconds: Optional[int]) -> str:
    if status == "pending":
        return queue_wait_seconds_to_text(estimated_wait_seconds)
    if status == "processing":
        return "正在分析，LLM 响应时间可能有波动"
    if status == "completed":
        return "已完成"
    if status == "completed_with_parse_error":
        return "已完成，但结果解析存在问题"
    if status == "failed":
        return "任务失败"
    return "状态未知"


def _processing_age_minutes(task: Dict[str, Any], now: datetime) -> Optional[int]:
    started_at = task.get("started_at") or task.get("updated_at")
    if not isinstance(started_at, datetime):
        return None
    return max(0, int((now - started_at).total_seconds() // 60))


def _serialize_processing_task(task: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    age = _processing_age_minutes(task, now)
    task_type = infer_task_type(task)
    return {
        "task_id": str(task.get("_id")),
        "task_type": task_type,
        "status": normalize_task_status(task.get("status")),
        "symbol": task.get("symbol"),
        "stock_name": task.get("stock_name"),
        "source": task.get("source"),
        "priority": int(task.get("priority") or 0),
        "created_by_user_id": task.get("created_by_user_id") or task.get("user_id"),
        "created_by_username": task.get("created_by_username"),
        "worker_id": task.get("worker_id"),
        "started_at": format_dt(task.get("started_at")),
        "updated_at": format_dt(task.get("updated_at")),
        "processing_age_minutes": age,
        "stale": bool(age is not None and age >= PROCESSING_STALE_MINUTES),
    }


def _fetch_queue_docs(tasks_col, query: Dict[str, Any], limit: int, status: Optional[str]) -> List[Dict[str, Any]]:
    if status:
        return list(tasks_col.find(query).sort("created_at", -1).limit(limit))

    running_query = dict(query)
    running_query["status"] = {"$in": ["pending", "processing"]}
    terminal_query = dict(query)
    terminal_query["status"] = {"$in": ["completed", "completed_with_parse_error", "failed"]}

    running_docs = list(tasks_col.find(running_query).sort([("status", -1), ("priority", -1), ("created_at", 1)]).limit(limit))
    remaining = max(0, limit - len(running_docs))
    if remaining <= 0:
        return running_docs
    seen_ids = {doc.get("_id") for doc in running_docs}
    terminal_docs = [
        doc
        for doc in tasks_col.find(terminal_query).sort("created_at", -1).limit(remaining)
        if doc.get("_id") not in seen_ids
    ]
    return running_docs + terminal_docs


@router.get("/queue", summary="获取分析任务队列状态")
async def get_analysis_task_queue(
    limit: int = Query(50, ge=1, le=200),
    status: Optional[str] = Query(None, description="可选状态过滤：pending/processing/completed/failed"),
    current_user: dict = Depends(get_current_active_user),
):
    collections = get_db_collections()
    tasks_col = collections["analysis_tasks"]
    now = datetime.now()

    pending_count = tasks_col.count_documents({"status": "pending"})
    processing_count = tasks_col.count_documents({"status": "processing"})
    completed_count = tasks_col.count_documents({"status": {"$in": ["completed", "completed_with_parse_error"]}})
    failed_count = tasks_col.count_documents({"status": "failed"})
    estimated_parallelism = ANALYSIS_WORKER_PARALLELISM
    processing_docs = list(tasks_col.find({"status": "processing"}).sort([("started_at", -1), ("updated_at", -1)]))
    stale_processing_count = sum(1 for task in processing_docs if (_processing_age_minutes(task, now) or 0) >= PROCESSING_STALE_MINUTES)

    query: Dict[str, Any] = _build_user_filter(current_user)
    if status:
        query["status"] = normalize_task_status(status)
    else:
        query["status"] = {"$in": ["pending", "processing", "completed", "completed_with_parse_error", "failed"]}

    docs = _fetch_queue_docs(tasks_col, query, limit, status)
    duration_cache: Dict[str, int] = {}
    items = []
    for task in docs:
        task_id = str(task.get("_id"))
        task_type = infer_task_type(task)
        normalized_status = normalize_task_status(task.get("status"))
        average_duration = duration_cache.get(task_type)
        if average_duration is None:
            average_duration = estimate_task_duration_seconds(tasks_col, task_type)
            duration_cache[task_type] = average_duration

        queue_ahead = compute_queue_ahead(tasks_col, task) if normalized_status == "pending" else 0
        estimated_wait = None
        if normalized_status == "pending":
            estimated_wait = estimate_wait_seconds(
                queue_ahead=queue_ahead,
                processing_count=processing_count,
                average_duration_seconds=average_duration,
                estimated_parallelism=estimated_parallelism,
            )

        items.append(
            {
                "task_id": task_id,
                "task_type": task_type,
                "status": normalized_status,
                "priority": int(task.get("priority") or 0),
                "symbol": task.get("symbol"),
                "stock_name": task.get("stock_name"),
                "source": task.get("source"),
                "created_at": format_dt(task.get("created_at")),
                "updated_at": format_dt(task.get("updated_at")),
                "started_at": format_dt(task.get("started_at")),
                "completed_at": format_dt(task.get("completed_at")),
                "failed_at": format_dt(task.get("failed_at")),
                "worker_id": task.get("worker_id"),
                "data_date": infer_task_data_date(task),
                "queue_ahead": queue_ahead,
                "estimated_wait_seconds": estimated_wait,
                "wait_hint": _queue_status_hint(normalized_status, estimated_wait),
                "average_duration_seconds": average_duration,
                "error": task.get("error"),
                "result_summary": summarize_task_result(task),
                "render_type": _get_render_type(task_type),
                "revision_of": task.get("revision_of"),
                "created_by_user_id": task.get("created_by_user_id") if current_user.get("is_admin") else None,
                "created_by_username": task.get("created_by_username") if current_user.get("is_admin") else None,
            }
        )

    return {
        "success": True,
        "global": {
            "pending_count": pending_count,
            "processing_count": processing_count,
            "stale_processing_count": stale_processing_count,
            "completed_count": completed_count,
            "failed_count": failed_count,
            "estimated_parallelism": estimated_parallelism,
            "parallelism_source": "ANALYSIS_WORKER_PARALLELISM",
            "processing_stale_minutes": PROCESSING_STALE_MINUTES,
            "default_average_duration_seconds": estimate_task_duration_seconds(tasks_col, "deep_analysis"),
            "updated_at": datetime.now().isoformat(),
        },
        "processing_tasks": [
            _serialize_processing_task(task, now)
            for task in processing_docs
        ] if current_user.get("is_admin") else [],
        "items": items,
    }


@router.get("/{task_id}", summary="获取分析任务详情")
async def get_analysis_task_detail(
    task_id: str,
    current_user: dict = Depends(get_current_active_user),
):
    collections = get_db_collections()
    try:
        task = collections["analysis_tasks"].find_one({"_id": ObjectId(task_id)})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid task_id")

    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not can_access_task(task, current_user):
        raise HTTPException(status_code=403, detail="无权访问此任务")

    revisions_col = collections.get("analysis_task_revisions")
    latest_revision = None
    if revisions_col is not None:
        latest_revision = revisions_col.find_one({"task_id": task_id}, sort=[("revision_no", -1)])
        latest_revision = _serialize_doc(latest_revision)

    task_type = infer_task_type(task)
    normalized_status = normalize_task_status(task.get("status"))
    result_completeness = (
        "partial"
        if task_type in PARTIAL_TYPES or normalized_status == "completed_with_parse_error"
        else "full"
    )
    return {
        "success": True,
        "task_meta": {
            "task_id": task_id,
            "task_type": task_type,
            "status": normalize_task_status(task.get("status")),
            "symbol": task.get("symbol"),
            "stock_name": task.get("stock_name"),
            "created_at": format_dt(task.get("created_at")),
            "updated_at": format_dt(task.get("updated_at")),
            "completed_at": format_dt(task.get("completed_at")),
            "data_date": infer_task_data_date(task),
            "error": task.get("error"),
            "revision_of": task.get("revision_of"),
        },
        "base_result": task.get("result"),
        "result_completeness": result_completeness,
        "render_type": _get_render_type(task_type),
        "latest_revision": latest_revision,
    }


@router.get("/{task_id}/resolved", summary="获取任务聚合后的完整结果")
async def get_resolved_task_result(
    task_id: str,
    current_user: dict = Depends(get_current_active_user),
):
    collections = get_db_collections()
    try:
        task = collections["analysis_tasks"].find_one({"_id": ObjectId(task_id)})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid task_id")

    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not can_access_task(task, current_user):
        raise HTTPException(status_code=403, detail="无权访问此任务")

    return {
        "success": True,
        "task_id": task_id,
        "task_type": infer_task_type(task),
        "data_date": infer_task_data_date(task),
        "base_result": task.get("result"),
        "extra_result": _fetch_extra_result(task),
        "resolved_at": datetime.now().isoformat(),
    }


@router.get("/{task_id}/revisions", summary="获取任务历史版本")
async def get_task_revisions(
    task_id: str,
    current_user: dict = Depends(get_current_active_user),
):
    collections = get_db_collections()
    tasks_col = collections["analysis_tasks"]
    revisions_col = collections["analysis_task_revisions"]
    try:
        task = tasks_col.find_one({"_id": ObjectId(task_id)})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid task_id")

    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not can_access_task(task, current_user):
        raise HTTPException(status_code=403, detail="无权访问此任务")

    root_task_id = str(task.get("revision_of") or task_id)
    related_ids = {root_task_id}
    id_filters = [{"revision_of": root_task_id}]
    try:
        id_filters.append({"_id": ObjectId(root_task_id)})
    except Exception:
        pass
    cursor = tasks_col.find({"$or": id_filters}, {"_id": 1})
    for row in cursor:
        related_ids.add(str(row.get("_id")))

    docs = list(revisions_col.find({"task_id": {"$in": list(related_ids)}}).sort([("revision_no", 1), ("created_at", 1)]))
    revisions = []
    for doc in docs:
        revisions.append(
            {
                "id": str(doc.get("_id")),
                "task_id": doc.get("task_id"),
                "revision_no": doc.get("revision_no"),
                "status": normalize_task_status(doc.get("status")),
                "feedback": doc.get("feedback"),
                "summary": doc.get("summary"),
                "created_at": format_dt(doc.get("created_at")),
                "completed_at": format_dt(doc.get("completed_at")),
            }
        )

    return {"success": True, "task_id": task_id, "root_task_id": root_task_id, "revisions": revisions}


@router.post("/{task_id}/feedback", summary="提交反馈并创建新版本任务")
async def submit_task_feedback(
    task_id: str,
    body: Dict[str, Any],
    current_user: dict = Depends(get_current_active_user),
):
    feedback_text = str(body.get("feedback") or "").strip()
    if not feedback_text:
        raise HTTPException(status_code=400, detail="feedback 不能为空")

    collections = get_db_collections()
    tasks_col = collections["analysis_tasks"]
    try:
        task = tasks_col.find_one({"_id": ObjectId(task_id)})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid task_id")

    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not can_access_task(task, current_user):
        raise HTTPException(status_code=403, detail="无权访问此任务")

    normalized_status = normalize_task_status(task.get("status"))
    if normalized_status not in {"completed", "failed", "completed_with_parse_error"}:
        raise HTTPException(status_code=409, detail="仅终态任务支持反馈重跑")

    task_type = infer_task_type(task)
    source_params = dict(task.get("params", {}) or {})
    source_params["feedback"] = feedback_text
    source_params["parent_task_id"] = task_id

    revision_of = str(task.get("revision_of") or task_id)
    new_task = build_task_doc(
        current_user=current_user,
        task_type=task_type,
        priority=int(task.get("priority", 1)) + 1,
        symbol=task.get("symbol"),
        stock_name=task.get("stock_name"),
        params=source_params,
        source="feedback_rerun",
        provider=task.get("provider"),
        model=task.get("model"),
        revision_of=revision_of,
        feedback=feedback_text,
    )
    inserted = tasks_col.insert_one(new_task)
    new_task_id = str(inserted.inserted_id)

    # count 包含 root task 本身，减 1 得到实际 revision 序号
    rev_count = tasks_col.count_documents({"$or": [{"_id": ObjectId(revision_of)}, {"revision_of": revision_of}]})

    return {
        "success": True,
        "new_task_id": new_task_id,
        "revision_no": rev_count - 1,
        "status": "pending",
        "message": "反馈已提交并创建新分析任务",
    }

