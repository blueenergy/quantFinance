"""Ops repair queue API.

The API only enqueues. ``quant-ops-launcher`` drains ``finance.ops_repair_jobs``
and starts scratch containers from a kind template. Kind ``score_force_refresh``
is CSI1000 growth+cycle ``--force-refresh``; it must point at a completed stale
score-audit job.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from pymongo import ASCENDING, DESCENDING

from auth import get_current_user
from db import get_db_collections
from routers.score_audit import (
    STALE_VERDICTS,
    _current_user_id,
    _jsonable,
    _normalize_date,
)


router = APIRouter(prefix="/ops-repair", tags=["ops-repair"])

JobKind = Literal["score_force_refresh"]
ALLOWED_KINDS = frozenset({"score_force_refresh"})
# Later kinds (e.g. score_gap_fill) register here; they are not implemented yet.
KIND_REGISTRY: Dict[str, Dict[str, Any]] = {
    "score_force_refresh": {
        "universe_index": "csi1000",
        "dimensions": ["growth", "cycle"],
        "force_refresh": True,
    },
}
MAX_ACTIVE_REPAIR = 1
FORCE_REFRESH_UNIVERSE = "csi1000"

JOB_LIST_PROJECTION = {
    "_id": 0,
    "job_id": 1,
    "user_id": 1,
    "job_kind": 1,
    "source_audit_job_id": 1,
    "start_date": 1,
    "end_date": 1,
    "universe_index": 1,
    "runtime": 1,
    "confirm_years": 1,
    "shards": 1,
    "status": 1,
    "attempts": 1,
    "error_message": 1,
    "created_at": 1,
    "updated_at": 1,
    "started_at": 1,
    "completed_at": 1,
    "heartbeat_at": 1,
    "progress_stage": 1,
    "progress_message": 1,
    "worker_id": 1,
}


class CreateOpsRepairJobRequest(BaseModel):
    source_audit_job_id: str = Field(..., min_length=8, max_length=80)
    job_kind: JobKind = "score_force_refresh"
    confirm_years: bool = False
    start_date: Optional[str] = None
    end_date: Optional[str] = None


def calendar_year_count(start_date: str, end_date: str) -> int:
    return int(end_date[:4]) - int(start_date[:4]) + 1


def calendar_year_shards(start_date: str, end_date: str) -> List[Dict[str, str]]:
    shards: List[Dict[str, str]] = []
    year = int(start_date[:4])
    last_year = int(end_date[:4])
    while year <= last_year:
        shard_start = max(start_date, f"{year}0101")
        shard_end = min(end_date, f"{year}1231")
        shards.append({"start": shard_start, "end": shard_end, "status": "pending"})
        year += 1
    return shards


def _ensure_indexes(collections: Dict[str, Any]) -> None:
    jobs = collections["ops_repair_jobs"]
    jobs.create_index([("job_id", ASCENDING)], unique=True)
    jobs.create_index([("status", ASCENDING), ("created_at", ASCENDING)])
    jobs.create_index([("source_audit_job_id", ASCENDING), ("created_at", DESCENDING)])
    jobs.create_index([("created_at", DESCENDING)])


def _count_active(jobs) -> int:
    return int(jobs.count_documents({"status": {"$in": ["pending", "running", "blocked"]}}) or 0)


def _require_stale_audit(audit: Dict[str, Any]) -> None:
    if not audit:
        raise HTTPException(status_code=400, detail="source audit job not found")
    if audit.get("status") != "completed":
        raise HTTPException(status_code=400, detail="source audit job is not completed")
    verdict = str(audit.get("overall_verdict") or "")
    if verdict not in STALE_VERDICTS:
        raise HTTPException(status_code=400, detail="source audit job is not stale")


def _window_from_audit(audit: Dict[str, Any], request: CreateOpsRepairJobRequest) -> tuple[str, str]:
    start = _normalize_date(request.start_date) if request.start_date else str(audit.get("start_date") or "")
    end = _normalize_date(request.end_date) if request.end_date else str(audit.get("end_date") or "")
    if not start or not end:
        raise HTTPException(status_code=400, detail="audit job is missing start_date/end_date")
    if start > end:
        raise HTTPException(status_code=400, detail="start_date must be <= end_date")
    audit_start = str(audit.get("start_date") or "")
    audit_end = str(audit.get("end_date") or "")
    if start < audit_start or end > audit_end:
        raise HTTPException(status_code=400, detail="repair window must stay inside the audit window")
    return start, end


def _build_job_document(user_id: str, audit: Dict[str, Any], request: CreateOpsRepairJobRequest) -> Dict[str, Any]:
    if request.job_kind not in ALLOWED_KINDS:
        raise HTTPException(status_code=400, detail="unknown job_kind")
    start, end = _window_from_audit(audit, request)
    year_count = calendar_year_count(start, end)
    if year_count > 1 and not request.confirm_years:
        raise HTTPException(status_code=400, detail="confirm_years is required when the window spans more than one calendar year")
    now = datetime.utcnow()
    shards = calendar_year_shards(start, end)
    return {
        "job_id": str(uuid.uuid4()),
        "user_id": user_id,
        "job_kind": request.job_kind,
        "source_audit_job_id": str(audit["job_id"]),
        "start_date": start,
        "end_date": end,
        "universe_index": FORCE_REFRESH_UNIVERSE,
        "runtime": "docker",
        "confirm_years": bool(request.confirm_years),
        "shards": shards,
        "status": "pending",
        "attempts": 0,
        "error_message": None,
        "created_at": now,
        "updated_at": now,
    }


@router.post("/jobs")
async def create_ops_repair_job(
    request: CreateOpsRepairJobRequest,
    current_user: dict = Depends(get_current_user),
):
    collections = get_db_collections()
    _ensure_indexes(collections)
    user_id = _current_user_id(current_user)
    audit = collections["score_audit_jobs"].find_one(
        {"job_id": request.source_audit_job_id},
        {"_id": 0},
    )
    _require_stale_audit(audit)
    if _count_active(collections["ops_repair_jobs"]) >= MAX_ACTIVE_REPAIR:
        raise HTTPException(status_code=409, detail="already have a pending or running ops repair; wait for the launcher")
    job = _build_job_document(user_id, audit, request)
    collections["ops_repair_jobs"].insert_one(job)
    return {"success": True, "data": _jsonable(job)}


@router.get("/jobs")
async def list_ops_repair_jobs(
    source_audit_job_id: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=30, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
):
    _current_user_id(current_user)
    collections = get_db_collections()
    query: Dict[str, Any] = {}
    if source_audit_job_id:
        query["source_audit_job_id"] = source_audit_job_id
    if status:
        query["status"] = status
    jobs = list(
        collections["ops_repair_jobs"]
        .find(query, JOB_LIST_PROJECTION)
        .sort("created_at", DESCENDING)
        .limit(limit)
    )
    return {"success": True, "data": _jsonable(jobs)}


@router.get("/jobs/{job_id}")
async def get_ops_repair_job(job_id: str, current_user: dict = Depends(get_current_user)):
    _current_user_id(current_user)
    collections = get_db_collections()
    job = collections["ops_repair_jobs"].find_one({"job_id": job_id}, {"_id": 0})
    if not job:
        raise HTTPException(status_code=404, detail="ops repair job not found")
    return {"success": True, "data": _jsonable(job)}
