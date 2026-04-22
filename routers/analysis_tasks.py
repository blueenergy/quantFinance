from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query

from auth import get_current_active_user
from db import get_analyzer_db, get_db_collections
from services.analysis_tasks import (
    can_access_task,
    build_task_doc,
    format_dt,
    infer_task_data_date,
    infer_task_type,
    normalize_task_status,
    summarize_task_result,
)


router = APIRouter(prefix="/analysis-tasks", tags=["分析任务中心"])


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

