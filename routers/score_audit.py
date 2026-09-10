"""Score self-audit jobs API.

The API only enqueues. ``quant-scorer`` drains ``finance.score_audit_jobs`` and
writes stored-vs-live verdicts onto the same document. Each environment audits
its own Mongo; this is not a cross-cluster compare.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from pymongo import ASCENDING, DESCENDING

from auth import get_current_user
from db import get_db_collections


router = APIRouter(prefix="/score-audit", tags=["score-audit"])

UniverseIndex = Literal["hs300", "a500", "csi500", "csi1000", "star50"]
MAX_QUEUE_ACTIVE = 3

JOB_LIST_PROJECTION = {
    "_id": 0,
    "job_id": 1,
    "user_id": 1,
    "source": 1,
    "name": 1,
    "start_date": 1,
    "end_date": 1,
    "universe_index": 1,
    "params": 1,
    "status": 1,
    "attempts": 1,
    "error_message": 1,
    "error_kind": 1,
    "artifact": 1,
    "overall_verdict": 1,
    "created_at": 1,
    "updated_at": 1,
    "started_at": 1,
    "completed_at": 1,
    "heartbeat_at": 1,
    "progress_stage": 1,
    "progress_message": 1,
    "worker_id": 1,
}


class CreateScoreAuditJobRequest(BaseModel):
    start_date: str
    end_date: str
    universe_index: UniverseIndex = Field(default="csi1000")
    step: int = Field(default=2, ge=1, le=40)
    sample: int = Field(default=12, ge=0, le=50)
    max_dates: int = Field(default=40, ge=1, le=80)
    symbols: Optional[List[str]] = Field(default=None, max_length=80)
    name: str = Field(default="", max_length=200)


def _jsonable(value: Any) -> Any:
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


def _current_user_id(current_user: Dict[str, Any]) -> str:
    user_id = current_user.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="current user id is missing")
    return str(user_id)


def _normalize_date(value: str) -> str:
    raw = str(value or "").replace("-", "").strip()
    if len(raw) >= 8 and raw[:8].isdigit():
        return raw[:8]
    raise HTTPException(status_code=400, detail=f"invalid date: {value}")


def _ensure_indexes(collections: Dict[str, Any]) -> None:
    jobs = collections["score_audit_jobs"]
    jobs.create_index([("job_id", ASCENDING)], unique=True)
    jobs.create_index([("status", ASCENDING), ("created_at", ASCENDING)])
    jobs.create_index([("created_at", DESCENDING)])


def _count_active(jobs) -> int:
    return int(jobs.count_documents({"status": {"$in": ["pending", "running"]}}) or 0)


def _build_job_document(user_id: str, request: CreateScoreAuditJobRequest) -> Dict[str, Any]:
    start_date = _normalize_date(request.start_date)
    end_date = _normalize_date(request.end_date)
    if start_date > end_date:
        raise HTTPException(status_code=400, detail="start_date must be <= end_date")
    symbols = [str(sym).strip() for sym in (request.symbols or []) if str(sym).strip()]
    params = {
        "start_date": start_date,
        "end_date": end_date,
        "universe_index": request.universe_index,
        "step": request.step,
        "sample": request.sample,
        "max_dates": request.max_dates,
        "seed": 42,
        "symbols": symbols,
    }
    now = datetime.utcnow()
    name = str(request.name or "").strip() or f"score audit {start_date}-{end_date} {request.universe_index}"
    return {
        "job_id": str(uuid.uuid4()),
        "user_id": user_id,
        "source": "ui",
        "name": name,
        "start_date": start_date,
        "end_date": end_date,
        "universe_index": request.universe_index,
        "params": params,
        "status": "pending",
        "attempts": 0,
        "result_id": None,
        "error_message": None,
        "artifact": None,
        "report": None,
        "created_at": now,
        "updated_at": now,
    }


@router.post("/jobs")
async def create_score_audit_job(
    request: CreateScoreAuditJobRequest,
    current_user: dict = Depends(get_current_user),
):
    collections = get_db_collections()
    _ensure_indexes(collections)
    user_id = _current_user_id(current_user)
    if _count_active(collections["score_audit_jobs"]) >= MAX_QUEUE_ACTIVE:
        raise HTTPException(status_code=409, detail="already have pending or running score audits; wait for the scorer")
    job = _build_job_document(user_id, request)
    collections["score_audit_jobs"].insert_one(job)
    return {"success": True, "data": _jsonable(job)}


@router.get("/jobs")
async def list_score_audit_jobs(
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=30, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
):
    _current_user_id(current_user)
    collections = get_db_collections()
    query: Dict[str, Any] = {}
    if status:
        query["status"] = status
    jobs = list(
        collections["score_audit_jobs"]
        .find(query, JOB_LIST_PROJECTION)
        .sort("created_at", DESCENDING)
        .limit(limit)
    )
    return {"success": True, "data": _jsonable(jobs)}


@router.get("/jobs/{job_id}")
async def get_score_audit_job(job_id: str, current_user: dict = Depends(get_current_user)):
    _current_user_id(current_user)
    collections = get_db_collections()
    job = collections["score_audit_jobs"].find_one({"job_id": job_id}, {"_id": 0})
    if not job:
        raise HTTPException(status_code=404, detail="score audit job not found")
    return {"success": True, "data": _jsonable(job)}
