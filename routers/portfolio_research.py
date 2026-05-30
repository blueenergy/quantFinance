"""Portfolio research jobs and strategy publishing API."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
import os
import uuid

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from pymongo import ASCENDING, DESCENDING

from auth import get_current_user
from db import get_trading_db_collections


router = APIRouter(prefix="/portfolio-research", tags=["portfolio-research"])


class CreateResearchJobRequest(BaseModel):
    name: str = Field(default="组合研究")
    universe_index: str = Field(default="csi1000")
    start_date: str
    end_date: str
    score_column: str = Field(default="composite_growth_cycle_score")
    growth_cycle_weights: List[str] = Field(default_factory=lambda: ["30:70"])
    top_n_values: List[int] = Field(default_factory=lambda: [10, 20, 50])
    horizon: int = Field(default=20, ge=1)
    rebalance_interval_days: Optional[List[int]] = None
    active_caps: List[float] = Field(default_factory=lambda: [0.2, 0.25, 0.3, 0.4])
    transaction_cost: float = Field(default=0.001, ge=0)
    index_benchmark_symbol: str = Field(default="000852.SH")
    cash_buffer: float = Field(default=0.03, ge=0, le=1)
    initial_capital: float = Field(default=1_000_000.0, gt=0)
    force: bool = Field(default=False)


class PublishResearchResultRequest(BaseModel):
    status: str = Field(default="draft", pattern="^(draft|enabled)$")
    strategy_id: Optional[str] = None
    name: Optional[str] = None
    create_new_version: bool = Field(default=False)
    comment: str = Field(default="")


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


def _reviewer(current_user: Dict[str, Any]) -> str:
    return str(current_user.get("username") or current_user.get("email") or current_user.get("id") or "unknown")


def _normalize_date(value: str) -> str:
    raw = str(value or "").replace("-", "").strip()
    if len(raw) >= 8 and raw[:8].isdigit():
        return raw[:8]
    raise HTTPException(status_code=400, detail=f"invalid date: {value}")


def _ensure_research_indexes(collections: Dict[str, Any]) -> None:
    collections["portfolio_research_jobs"].create_index([("job_id", ASCENDING)], unique=True)
    collections["portfolio_research_jobs"].create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])
    collections["portfolio_research_jobs"].create_index([("status", ASCENDING), ("created_at", ASCENDING)])
    collections["portfolio_research_results"].create_index([("result_id", ASCENDING)], unique=True)
    collections["portfolio_research_results"].create_index([("job_id", ASCENDING)])
    collections["portfolio_research_results"].create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])
    collections["portfolio_research_reviews"].create_index([("job_id", ASCENDING), ("created_at", DESCENDING)])
    collections["portfolio_research_reviews"].create_index([("research_result_id", ASCENDING), ("created_at", DESCENDING)])


def _job_summary(job: Dict[str, Any], result: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    row = dict(job)
    if result:
        row["best_row"] = result.get("best_row")
        row["candidate_strategy_config"] = result.get("candidate_strategy_config")
    return row


def _unique_strategy_id(collections: Dict[str, Any], requested: str, create_new_version: bool) -> str:
    strategy_id = requested
    existing = collections["portfolio_strategy_configs"].find_one({"strategy_id": strategy_id})
    if not existing:
        return strategy_id
    if not create_new_version:
        raise HTTPException(status_code=409, detail="strategy_id already exists")
    suffix = datetime.utcnow().strftime("v%Y%m%d%H%M%S")
    candidate = f"{strategy_id}_{suffix}"
    if collections["portfolio_strategy_configs"].find_one({"strategy_id": candidate}):
        candidate = f"{strategy_id}_{suffix}_{uuid.uuid4().hex[:6]}"
    return candidate


@router.post("/jobs")
async def create_portfolio_research_job(request: CreateResearchJobRequest, current_user: dict = Depends(get_current_user)):
    collections = get_trading_db_collections()
    _ensure_research_indexes(collections)
    user_id = _current_user_id(current_user)
    start_date = _normalize_date(request.start_date)
    end_date = _normalize_date(request.end_date)
    if start_date > end_date:
        raise HTTPException(status_code=400, detail="start_date must be <= end_date")

    now = datetime.utcnow()
    params = request.model_dump()
    params["start_date"] = start_date
    params["end_date"] = end_date
    params["rebalance_interval_days"] = request.rebalance_interval_days or [request.horizon]
    job = {
        "job_id": str(uuid.uuid4()),
        "user_id": user_id,
        "name": request.name,
        "strategy_family": "growth_cycle",
        "universe_index": request.universe_index,
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
    collections["portfolio_research_jobs"].insert_one(job)
    return {"success": True, "data": _jsonable(job)}


@router.get("/jobs")
async def list_portfolio_research_jobs(
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    current_user: dict = Depends(get_current_user),
):
    collections = get_trading_db_collections()
    user_id = _current_user_id(current_user)
    query: Dict[str, Any] = {"user_id": user_id}
    if status:
        query["status"] = status
    jobs = list(collections["portfolio_research_jobs"].find(query).sort("created_at", -1).limit(limit))
    result_ids = [job.get("result_id") for job in jobs if job.get("result_id")]
    results = {
        row.get("result_id"): row
        for row in collections["portfolio_research_results"].find({"result_id": {"$in": result_ids}})
    } if result_ids else {}
    return {"success": True, "data": _jsonable([_job_summary(job, results.get(job.get("result_id"))) for job in jobs])}


@router.get("/jobs/{job_id}")
async def get_portfolio_research_job(job_id: str, current_user: dict = Depends(get_current_user)):
    collections = get_trading_db_collections()
    user_id = _current_user_id(current_user)
    job = collections["portfolio_research_jobs"].find_one({"job_id": job_id, "user_id": user_id})
    if not job:
        raise HTTPException(status_code=404, detail="research job not found")
    result = None
    if job.get("result_id"):
        result = collections["portfolio_research_results"].find_one({"result_id": job["result_id"], "user_id": user_id})
    return {"success": True, "data": _jsonable(_job_summary(job, result))}


@router.get("/jobs/{job_id}/results")
async def get_portfolio_research_results(job_id: str, current_user: dict = Depends(get_current_user)):
    collections = get_trading_db_collections()
    user_id = _current_user_id(current_user)
    job = collections["portfolio_research_jobs"].find_one({"job_id": job_id, "user_id": user_id})
    if not job:
        raise HTTPException(status_code=404, detail="research job not found")
    result = collections["portfolio_research_results"].find_one({"job_id": job_id, "user_id": user_id})
    if not result:
        raise HTTPException(status_code=404, detail="research result not found")
    return {"success": True, "data": _jsonable(result)}


@router.get("/jobs/{job_id}/artifact")
async def get_portfolio_research_artifact(job_id: str, current_user: dict = Depends(get_current_user)):
    collections = get_trading_db_collections()
    user_id = _current_user_id(current_user)
    result = collections["portfolio_research_results"].find_one({"job_id": job_id, "user_id": user_id})
    if not result:
        raise HTTPException(status_code=404, detail="research result not found")
    html_path = (result.get("artifact") or {}).get("html_path")
    if not html_path:
        raise HTTPException(status_code=404, detail="artifact not found")
    path = Path(html_path)
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="artifact file not found")
    return FileResponse(str(path), media_type="text/html")


@router.post("/results/{result_id}/publish")
async def publish_portfolio_research_result(
    result_id: str,
    request: PublishResearchResultRequest,
    current_user: dict = Depends(get_current_user),
):
    collections = get_trading_db_collections()
    user_id = _current_user_id(current_user)
    result = collections["portfolio_research_results"].find_one({"result_id": result_id, "user_id": user_id})
    if not result:
        raise HTTPException(status_code=404, detail="research result not found")
    job = collections["portfolio_research_jobs"].find_one({"job_id": result.get("job_id"), "user_id": user_id})
    if not job or job.get("status") != "completed":
        raise HTTPException(status_code=400, detail="research job is not completed")
    candidate = dict(result.get("candidate_strategy_config") or {})
    if not candidate:
        raise HTTPException(status_code=400, detail="candidate strategy config is missing")

    now = datetime.utcnow()
    requested_id = request.strategy_id or candidate.get("strategy_id")
    strategy_id = _unique_strategy_id(collections, str(requested_id), request.create_new_version)
    candidate.update(
        {
            "strategy_id": strategy_id,
            "name": request.name or candidate.get("name") or strategy_id,
            "status": request.status,
            "source": "portfolio_research",
            "research_job_id": job["job_id"],
            "research_result_id": result_id,
            "published_by": user_id,
            "published_at": now,
            "research_metrics_snapshot": result.get("best_row"),
            "data_watermark_snapshot": result.get("data_watermark"),
            "updated_at": now,
        }
    )
    candidate.setdefault("created_at", now)
    collections["portfolio_strategy_configs"].insert_one(candidate)
    review = {
        "review_id": str(uuid.uuid4()),
        "job_id": job["job_id"],
        "research_result_id": result_id,
        "strategy_id": strategy_id,
        "decision": "published",
        "status": request.status,
        "comment": request.comment,
        "reviewer": _reviewer(current_user),
        "user_id": user_id,
        "created_at": now,
    }
    collections["portfolio_research_reviews"].insert_one(review)
    collections["portfolio_research_results"].update_one(
        {"result_id": result_id},
        {"$set": {"published_strategy_id": strategy_id, "published_status": request.status, "published_at": now}},
    )
    return {"success": True, "data": _jsonable({"strategy": candidate, "review": review})}


@router.post("/strategies/{strategy_id}/disable")
async def disable_portfolio_research_strategy(strategy_id: str, current_user: dict = Depends(get_current_user)):
    collections = get_trading_db_collections()
    user_id = _current_user_id(current_user)
    strategy = collections["portfolio_strategy_configs"].find_one({"strategy_id": strategy_id})
    if not strategy:
        raise HTTPException(status_code=404, detail="strategy not found")
    if strategy.get("source") != "portfolio_research":
        raise HTTPException(status_code=400, detail="only portfolio research strategies can be disabled here")
    now = datetime.utcnow()
    collections["portfolio_strategy_configs"].update_one(
        {"strategy_id": strategy_id},
        {"$set": {"status": "disabled", "disabled_by": user_id, "disabled_at": now, "updated_at": now}},
    )
    review = {
        "review_id": str(uuid.uuid4()),
        "job_id": strategy.get("research_job_id"),
        "research_result_id": strategy.get("research_result_id"),
        "strategy_id": strategy_id,
        "decision": "disabled",
        "reviewer": _reviewer(current_user),
        "user_id": user_id,
        "created_at": now,
    }
    collections["portfolio_research_reviews"].insert_one(review)
    return {"success": True, "data": _jsonable(review)}
