"""Alpha factor backtest jobs API.

Jobs are enqueued here and drained by the ``quant-factor-researcher`` worker in
stock-scoring-system, which shares nothing with this service but the Mongo
collections. That worker owns validation: it rebuilds the params through
``FactorBacktestParams`` and marks anything it refuses as
``error_kind=invalid_request``. The checks below exist to fail fast on the
obvious mistakes, not to mirror its rules — a request that gets past them can
still be rejected with a precise reason on the job document.

The same split explains the factor catalog: factor definitions live in the
worker's repo, so it publishes their metadata into ``factor_catalog`` and this
service only reads it.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from pymongo import ASCENDING, DESCENDING

from auth import get_current_user
from db import get_trading_db_collections


router = APIRouter(prefix="/factor-backtest", tags=["factor-backtest"])
logger = logging.getLogger(__name__)

# Restricted to the universes whose size the worker knows, because that is what
# keeps its memory estimate — and therefore the guard against OOMing the pod on
# a 158-factor run — switched on. An unrecognised code silently skips it.
UniverseIndex = Literal["hs300", "a500", "csi1000", "csi2000"]
FactorSet = Literal["alpha8", "alpha158"]


class CreateFactorBacktestJobRequest(BaseModel):
    """A factor backtest request over the Mongo price history.

    Three of the worker's parameters are deliberately absent. ``source`` and
    ``panel`` would let a caller name a CSV path inside the worker's pod, and
    the pipeline is only useful here against Mongo anyway. ``memory_budget_gb``
    would override the budget the worker was started with — it fills the value
    in only when the job leaves it unset — so a caller could raise its own
    ceiling and OOM the pod. ``workers`` is a property of the machine, not of
    the request.
    """

    name: str = Field(default="因子回测", max_length=200)
    index_code: UniverseIndex = Field(default="csi1000")
    start_date: str
    end_date: str
    factor_set: FactorSet = Field(default="alpha8")
    factors: Optional[List[str]] = Field(default=None, max_length=200)
    warmup_days: int = Field(default=60, ge=0, le=400)
    horizons: List[int] = Field(default_factory=lambda: [1, 5, 10, 20], min_length=1, max_length=10)
    quantiles: int = Field(default=5, ge=2, le=20)
    min_names: int = Field(default=30, ge=2)
    top_k: List[int] = Field(default_factory=lambda: [20, 50], min_length=1, max_length=10)
    screen_top: int = Field(default=20, ge=1)
    screen_horizon: Optional[int] = Field(default=None, ge=1)
    slippage_bps: float = Field(default=10.0, ge=0, le=1000)
    turnover: float = Field(default=1.0, gt=0, le=1)

    @model_validator(mode="after")
    def _validate_request(self) -> "CreateFactorBacktestJobRequest":
        if any(h < 1 for h in self.horizons):
            raise ValueError("horizons must be >= 1")
        if any(k < 1 for k in self.top_k):
            raise ValueError("top_k values must be >= 1")
        if self.min_names < self.quantiles:
            raise ValueError(f"min_names {self.min_names} cannot fill {self.quantiles} quantile buckets")
        if self.screen_horizon is not None and self.screen_horizon not in self.horizons:
            raise ValueError(f"screen_horizon {self.screen_horizon} is not among horizons {self.horizons}")
        if self.factors is not None and not self.factors:
            raise ValueError("factors cannot be an empty list; omit it to use the whole set")
        return self


FACTOR_JOB_LIST_PROJECTION = {
    "_id": 0,
    "job_id": 1,
    "user_id": 1,
    "name": 1,
    "index_code": 1,
    "factor_set": 1,
    "start_date": 1,
    "end_date": 1,
    "params": 1,
    "status": 1,
    "attempts": 1,
    "result_id": 1,
    "error_message": 1,
    "error_kind": 1,
    # Written by the worker on completion so this view can summarise a report
    # without loading the couple of hundred kilobytes it occupies.
    "artifact": 1,
    "created_at": 1,
    "updated_at": 1,
    "started_at": 1,
    "completed_at": 1,
    "heartbeat_at": 1,
    "progress_stage": 1,
    "progress_message": 1,
    "worker_id": 1,
}


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


def _ensure_factor_indexes(collections: Dict[str, Any]) -> None:
    collections["factor_backtest_jobs"].create_index([("job_id", ASCENDING)], unique=True)
    collections["factor_backtest_jobs"].create_index([("status", ASCENDING), ("created_at", ASCENDING)])
    collections["factor_backtest_jobs"].create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])
    collections["factor_backtest_results"].create_index([("result_id", ASCENDING)], unique=True)
    collections["factor_backtest_results"].create_index([("job_id", ASCENDING)])


def _known_factor_names(collections: Dict[str, Any], factor_set: str) -> Optional[set]:
    """Names the worker published for one set, or None when it never has.

    A missing catalog must not block job creation: the worker validates names
    anyway, and its rejection message is the authoritative one.
    """
    doc = collections["factor_catalog"].find_one({"factor_set": factor_set}, {"_id": 0, "factors": 1})
    factors = (doc or {}).get("factors") or []
    if not factors:
        return None
    return {str(entry.get("name")) for entry in factors}


def _build_factor_job_document(
    user_id: str,
    request: CreateFactorBacktestJobRequest,
    collections: Dict[str, Any],
) -> Dict[str, Any]:
    start_date = _normalize_date(request.start_date)
    end_date = _normalize_date(request.end_date)
    if start_date > end_date:
        raise HTTPException(status_code=400, detail="start_date must be <= end_date")

    if request.factors:
        known = _known_factor_names(collections, request.factor_set)
        if known is not None:
            unknown = sorted(set(request.factors) - known)
            if unknown:
                raise HTTPException(
                    status_code=400,
                    detail=f"unknown {request.factor_set} factors: {', '.join(unknown)}",
                )

    params = request.model_dump(exclude={"name"})
    params["start_date"] = start_date
    params["end_date"] = end_date
    # The worker refuses a params key it does not recognise, and reads the
    # panel source only from here.
    params["source"] = "mongo"
    params["horizons"] = sorted(set(request.horizons))
    params["top_k"] = sorted(set(request.top_k))

    now = datetime.utcnow()
    return {
        "job_id": str(uuid.uuid4()),
        "user_id": user_id,
        "name": request.name,
        "index_code": request.index_code,
        "factor_set": request.factor_set,
        "start_date": start_date,
        "end_date": end_date,
        "params": params,
        "status": "pending",
        "attempts": 0,
        "result_id": None,
        "error_message": None,
        "created_at": now,
        "updated_at": now,
    }


@router.get("/factors")
async def list_factor_catalog(
    factor_set: Optional[FactorSet] = Query(default=None),
    current_user: dict = Depends(get_current_user),
):
    """Factor metadata published by the worker: expression, family, window, blurb."""
    _current_user_id(current_user)
    collections = get_trading_db_collections()
    query: Dict[str, Any] = {"factor_set": factor_set} if factor_set else {}
    docs = list(collections["factor_catalog"].find(query, {"_id": 0}))
    if not docs:
        raise HTTPException(
            status_code=503,
            detail="factor catalog is empty; the factor backtest worker publishes it on startup",
        )
    return {"success": True, "data": _jsonable(sorted(docs, key=lambda doc: str(doc.get("factor_set"))))}


@router.post("/jobs")
async def create_factor_backtest_job(
    request: CreateFactorBacktestJobRequest,
    current_user: dict = Depends(get_current_user),
):
    collections = get_trading_db_collections()
    _ensure_factor_indexes(collections)
    user_id = _current_user_id(current_user)
    job = _build_factor_job_document(user_id, request, collections)
    collections["factor_backtest_jobs"].insert_one(job)
    return {"success": True, "data": _jsonable(job)}


@router.get("/jobs")
async def list_factor_backtest_jobs(
    status: Optional[str] = Query(default=None),
    factor_set: Optional[FactorSet] = Query(default=None),
    index_code: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    current_user: dict = Depends(get_current_user),
):
    collections = get_trading_db_collections()
    user_id = _current_user_id(current_user)
    query: Dict[str, Any] = {"user_id": user_id}
    if status:
        query["status"] = status
    if factor_set:
        query["factor_set"] = factor_set
    if index_code:
        query["index_code"] = index_code
    jobs = list(
        collections["factor_backtest_jobs"]
        .find(query, FACTOR_JOB_LIST_PROJECTION)
        .sort("created_at", -1)
        .limit(limit)
    )
    return {"success": True, "data": _jsonable(jobs)}


@router.get("/jobs/{job_id}")
async def get_factor_backtest_job(job_id: str, current_user: dict = Depends(get_current_user)):
    collections = get_trading_db_collections()
    user_id = _current_user_id(current_user)
    job = collections["factor_backtest_jobs"].find_one(
        {"job_id": job_id, "user_id": user_id}, FACTOR_JOB_LIST_PROJECTION
    )
    if not job:
        raise HTTPException(status_code=404, detail="factor backtest job not found")
    return {"success": True, "data": _jsonable(job)}


@router.get("/jobs/{job_id}/results")
async def get_factor_backtest_results(job_id: str, current_user: dict = Depends(get_current_user)):
    """The full report. Stored inline in Mongo, so this is a few hundred kilobytes."""
    collections = get_trading_db_collections()
    user_id = _current_user_id(current_user)
    job = collections["factor_backtest_jobs"].find_one({"job_id": job_id, "user_id": user_id}, {"_id": 0, "job_id": 1})
    if not job:
        raise HTTPException(status_code=404, detail="factor backtest job not found")
    result = collections["factor_backtest_results"].find_one({"job_id": job_id, "user_id": user_id}, {"_id": 0})
    if not result:
        raise HTTPException(status_code=404, detail="factor backtest result not found")
    return {"success": True, "data": _jsonable(result)}


@router.delete("/jobs/{job_id}")
async def delete_factor_backtest_job(job_id: str, current_user: dict = Depends(get_current_user)):
    """Delete a finished job and its report.

    A pending or running job stays: the worker may still be holding it and
    would write its result back over the deletion.
    """
    collections = get_trading_db_collections()
    user_id = _current_user_id(current_user)
    job = collections["factor_backtest_jobs"].find_one({"job_id": job_id, "user_id": user_id})
    if not job:
        raise HTTPException(status_code=404, detail="factor backtest job not found")

    status = str(job.get("status") or "")
    if status in {"pending", "running"}:
        raise HTTPException(status_code=409, detail=f"cannot delete factor backtest job while status is {status}")

    deleted_results = collections["factor_backtest_results"].delete_many({"job_id": job_id, "user_id": user_id})
    deleted_jobs = collections["factor_backtest_jobs"].delete_one({"job_id": job_id, "user_id": user_id})
    return {
        "success": True,
        "data": {
            "job_id": job_id,
            "deleted_jobs": getattr(deleted_jobs, "deleted_count", 0),
            "deleted_results": getattr(deleted_results, "deleted_count", 0),
        },
    }
