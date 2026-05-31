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
    preset_id: Optional[str] = None
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
    collections["portfolio_strategy_param_presets"].create_index([("preset_id", ASCENDING)], unique=True)
    collections["portfolio_strategy_param_presets"].create_index([("strategy_template_id", ASCENDING), ("status", ASCENDING)])


def _job_summary(job: Dict[str, Any], result: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    row = dict(job)
    if result:
        row["best_row"] = result.get("best_row")
        row["candidate_strategy_config"] = result.get("candidate_strategy_config")
    return row


def _unique_preset_id(collections: Dict[str, Any], requested: str, create_new_version: bool) -> str:
    preset_id = requested
    existing = collections["portfolio_strategy_param_presets"].find_one({"preset_id": preset_id})
    if not existing:
        return preset_id
    if not create_new_version:
        raise HTTPException(status_code=409, detail="preset_id already exists")
    suffix = datetime.utcnow().strftime("v%Y%m%d%H%M%S")
    candidate = f"{preset_id}_{suffix}"
    if collections["portfolio_strategy_param_presets"].find_one({"preset_id": candidate}):
        candidate = f"{preset_id}_{suffix}_{uuid.uuid4().hex[:6]}"
    return candidate


def _resolve_artifact_path(html_path: str) -> Optional[Path]:
    candidates = [Path(html_path)]
    filename = Path(html_path).name
    for base_dir in (
        os.getenv("PORTFOLIO_RESEARCH_REPORT_DIR"),
        "/app/reports/portfolio-research",
        "reports/portfolio-research",
    ):
        if base_dir:
            candidates.append(Path(base_dir) / filename)

    seen = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


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
    path = _resolve_artifact_path(str(html_path))
    if path is None:
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
    requested_id = request.preset_id or candidate.get("preset_id") or f"research_{result_id[:8]}"
    preset_id = _unique_preset_id(collections, str(requested_id), request.create_new_version)
    strategy_template_id = str(candidate.get("strategy_template_id") or candidate.get("template_id") or "growth_cycle")
    preset = {
        "preset_id": preset_id,
        "strategy_template_id": strategy_template_id,
        "user_id": user_id,
        "name": request.name or candidate.get("name") or preset_id,
        "description": request.comment,
        "params": {
            key: value
            for key, value in candidate.items()
            if key
            in {
                "universe_index",
                "index_benchmark_symbol",
                "score_type",
                "score_column",
                "growth_weight",
                "cycle_weight",
                "top_n",
                "rebalance_days",
                "construction_mode",
                "max_industry_weight",
                "lot_size",
                "transaction_cost",
                "execution_price",
                "cash_buffer",
                "initial_capital",
                "candidate_pool_enabled",
            }
        },
        "status": request.status,
        "source": "portfolio_research",
        "research_job_id": job["job_id"],
        "research_result_id": result_id,
        "backtest_summary": result.get("best_row"),
        "data_window": {"start_date": job.get("start_date"), "end_date": job.get("end_date")},
        "data_watermark_snapshot": result.get("data_watermark"),
        "created_by": user_id,
        "created_at": now,
        "updated_at": now,
    }
    collections["portfolio_strategy_param_presets"].insert_one(preset)
    review = {
        "review_id": str(uuid.uuid4()),
        "job_id": job["job_id"],
        "research_result_id": result_id,
        "preset_id": preset_id,
        "strategy_template_id": strategy_template_id,
        "decision": "preset_published",
        "status": request.status,
        "comment": request.comment,
        "reviewer": _reviewer(current_user),
        "user_id": user_id,
        "created_at": now,
    }
    collections["portfolio_research_reviews"].insert_one(review)
    collections["portfolio_research_results"].update_one(
        {"result_id": result_id},
        {"$set": {"published_preset_id": preset_id, "published_status": request.status, "published_at": now}},
    )
    return {"success": True, "data": _jsonable({"preset": preset, "review": review})}


@router.post("/parameter-presets/{preset_id}/disable")
async def disable_portfolio_research_preset(preset_id: str, current_user: dict = Depends(get_current_user)):
    collections = get_trading_db_collections()
    user_id = _current_user_id(current_user)
    preset = collections["portfolio_strategy_param_presets"].find_one({"preset_id": preset_id, "user_id": user_id})
    if not preset:
        raise HTTPException(status_code=404, detail="parameter preset not found")
    if preset.get("source") != "portfolio_research":
        raise HTTPException(status_code=400, detail="only portfolio research presets can be disabled here")
    now = datetime.utcnow()
    collections["portfolio_strategy_param_presets"].update_one(
        {"preset_id": preset_id, "user_id": user_id},
        {"$set": {"status": "disabled", "disabled_by": user_id, "disabled_at": now, "updated_at": now}},
    )
    review = {
        "review_id": str(uuid.uuid4()),
        "job_id": preset.get("research_job_id"),
        "research_result_id": preset.get("research_result_id"),
        "preset_id": preset_id,
        "strategy_template_id": preset.get("strategy_template_id"),
        "decision": "disabled",
        "reviewer": _reviewer(current_user),
        "user_id": user_id,
        "created_at": now,
    }
    collections["portfolio_research_reviews"].insert_one(review)
    return {"success": True, "data": _jsonable(review)}
