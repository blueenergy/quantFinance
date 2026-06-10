"""Portfolio research jobs and strategy publishing API."""

from __future__ import annotations

import hashlib
import json
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
from db import get_db_collections, get_trading_db_collections


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
    buy_commission_rate: Optional[float] = Field(default=None, ge=0)
    sell_commission_rate: Optional[float] = Field(default=None, ge=0)
    min_commission: Optional[float] = Field(default=None, ge=0)
    stamp_tax_rate: Optional[float] = Field(default=None, ge=0)
    transfer_fee_rate: Optional[float] = Field(default=None, ge=0)
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
    collections["portfolio_strategy_param_presets"].create_index(
        [("user_id", ASCENDING), ("strategy_template_id", ASCENDING), ("params_hash", ASCENDING), ("status", ASCENDING)]
    )


def _as_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item]
    if value:
        return [str(value)]
    return []


def _latest_stock_score_date(stock_scores: Any, job: Dict[str, Any]) -> Optional[str]:
    params = job.get("params") or {}
    end_date = str(params.get("end_date") or job.get("end_date") or "").replace("-", "")
    if not end_date:
        return None
    query: Dict[str, Any] = {"score_date": {"$lte": end_date}}
    universes = _as_list(params.get("universe_index") or job.get("universe_index"))
    if universes:
        query["index_codes"] = {"$in": universes}
    row = stock_scores.find_one(query, {"_id": 0, "score_date": 1}, sort=[("score_date", DESCENDING)])
    return str(row["score_date"]) if row and row.get("score_date") else None


def _normalized_watermark(job: Dict[str, Any], result: Optional[Dict[str, Any]], stock_scores: Any = None) -> Dict[str, Any]:
    watermark = dict((result or {}).get("data_watermark") or job.get("data_watermark") or {})
    if "backtest_score_max_date" not in watermark and watermark.get("stock_scores_max_date"):
        # Older research results used stock_scores_max_date for the backtestable dataset max date.
        watermark["backtest_score_max_date"] = watermark.get("stock_scores_max_date")
        latest_score = _latest_stock_score_date(stock_scores, job) if stock_scores is not None else None
        if latest_score:
            watermark["stock_scores_max_date"] = latest_score
    return watermark


def _job_summary(job: Dict[str, Any], result: Optional[Dict[str, Any]] = None, stock_scores: Any = None) -> Dict[str, Any]:
    row = dict(job)
    row["data_watermark"] = _normalized_watermark(job, result, stock_scores)
    if result:
        row["best_row"] = result.get("best_row")
        row["candidate_strategy_config"] = result.get("candidate_strategy_config")
        row["published_preset_id"] = result.get("published_preset_id")
        row["published_status"] = result.get("published_status")
        row["published_at"] = result.get("published_at")
        row["publish_action"] = result.get("publish_action")
    return row


RESEARCH_JOB_LIST_PROJECTION = {
    "_id": 0,
    "job_id": 1,
    "user_id": 1,
    "name": 1,
    "strategy_family": 1,
    "universe_index": 1,
    "start_date": 1,
    "end_date": 1,
    "params": 1,
    "status": 1,
    "attempts": 1,
    "result_id": 1,
    "error_message": 1,
    "created_at": 1,
    "updated_at": 1,
    "started_at": 1,
    "completed_at": 1,
    "data_watermark": 1,
}


RESEARCH_RESULT_SUMMARY_PROJECTION = {
    "_id": 0,
    "result_id": 1,
    "job_id": 1,
    "best_row": 1,
    "candidate_strategy_config": 1,
    "published_preset_id": 1,
    "published_status": 1,
    "published_at": 1,
    "publish_action": 1,
}


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


PRESET_PARAM_KEYS = {
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
    "buy_commission_rate",
    "sell_commission_rate",
    "min_commission",
    "stamp_tax_rate",
    "transfer_fee_rate",
    "execution_price",
    "cash_buffer",
    "initial_capital",
    "candidate_pool_enabled",
}


def _preset_params_from_candidate(candidate: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in candidate.items() if key in PRESET_PARAM_KEYS}


def _params_hash(params: Dict[str, Any]) -> str:
    payload = json.dumps(params, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _research_evidence(job: Dict[str, Any], result: Dict[str, Any], status: str, comment: str) -> Dict[str, Any]:
    return {
        "research_job_id": job["job_id"],
        "research_result_id": result["result_id"],
        "status": status,
        "comment": comment,
        "best_row": result.get("best_row"),
        "data_window": {"start_date": job.get("start_date"), "end_date": job.get("end_date")},
        "data_watermark_snapshot": result.get("data_watermark"),
        "attached_at": datetime.utcnow(),
    }


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
    jobs = list(collections["portfolio_research_jobs"].find(query, RESEARCH_JOB_LIST_PROJECTION).sort("created_at", -1).limit(limit))
    result_ids = [job.get("result_id") for job in jobs if job.get("result_id")]
    results = {
        row.get("result_id"): row
        for row in collections["portfolio_research_results"].find({"result_id": {"$in": result_ids}}, RESEARCH_RESULT_SUMMARY_PROJECTION)
    } if result_ids else {}
    stock_scores = get_db_collections()["stock_scores"]
    return {"success": True, "data": _jsonable([_job_summary(job, results.get(job.get("result_id")), stock_scores) for job in jobs])}


@router.get("/jobs/{job_id}")
async def get_portfolio_research_job(job_id: str, current_user: dict = Depends(get_current_user)):
    collections = get_trading_db_collections()
    user_id = _current_user_id(current_user)
    job = collections["portfolio_research_jobs"].find_one({"job_id": job_id, "user_id": user_id}, RESEARCH_JOB_LIST_PROJECTION)
    if not job:
        raise HTTPException(status_code=404, detail="research job not found")
    result = None
    if job.get("result_id"):
        result = collections["portfolio_research_results"].find_one({"result_id": job["result_id"], "user_id": user_id}, RESEARCH_RESULT_SUMMARY_PROJECTION)
    stock_scores = get_db_collections()["stock_scores"]
    return {"success": True, "data": _jsonable(_job_summary(job, result, stock_scores))}


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


def _resolve_combo_file(combos_dir: Optional[str], file_rel: Optional[str]) -> Optional[Path]:
    """Resolve a per-combo detail JSON, mirroring the artifact path fallbacks.

    ``file_rel`` is the manifest-relative path (``<base>_combos/<key>.json``); we also
    try the recorded absolute ``combos_dir`` and the report-dir fallbacks so the file is
    found whether the API runs on the host or inside the container mount.
    """
    candidates: List[Path] = []
    if combos_dir and file_rel:
        candidates.append(Path(combos_dir) / Path(file_rel).name)
    for base_dir in (
        os.getenv("PORTFOLIO_RESEARCH_REPORT_DIR"),
        "/app/reports/portfolio-research",
        "reports/portfolio-research",
    ):
        if base_dir and file_rel:
            candidates.append(Path(base_dir) / file_rel)

    seen = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


@router.get("/jobs/{job_id}/combos")
async def list_portfolio_research_combos(job_id: str, current_user: dict = Depends(get_current_user)):
    collections = get_trading_db_collections()
    user_id = _current_user_id(current_user)
    result = collections["portfolio_research_results"].find_one({"job_id": job_id, "user_id": user_id})
    if not result:
        raise HTTPException(status_code=404, detail="research result not found")
    combos = (result.get("artifact") or {}).get("combos") or []
    return {"success": True, "data": _jsonable(combos)}


@router.get("/jobs/{job_id}/combos/{combo_key}")
async def get_portfolio_research_combo_detail(job_id: str, combo_key: str, current_user: dict = Depends(get_current_user)):
    collections = get_trading_db_collections()
    user_id = _current_user_id(current_user)
    result = collections["portfolio_research_results"].find_one({"job_id": job_id, "user_id": user_id})
    if not result:
        raise HTTPException(status_code=404, detail="research result not found")
    artifact = result.get("artifact") or {}
    combos = artifact.get("combos") or []
    entry = next((c for c in combos if c.get("combo_key") == combo_key), None)
    if not entry:
        raise HTTPException(status_code=404, detail="combo detail not found")
    path = _resolve_combo_file(artifact.get("combos_dir"), entry.get("file"))
    if path is None:
        raise HTTPException(status_code=404, detail="combo detail file not found")
    return FileResponse(str(path), media_type="application/json")


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
    if result.get("published_preset_id"):
        raise HTTPException(status_code=409, detail="research result has already been published")
    job = collections["portfolio_research_jobs"].find_one({"job_id": result.get("job_id"), "user_id": user_id})
    if not job or job.get("status") != "completed":
        raise HTTPException(status_code=400, detail="research job is not completed")
    candidate = dict(result.get("candidate_strategy_config") or {})
    if not candidate:
        raise HTTPException(status_code=400, detail="candidate strategy config is missing")

    now = datetime.utcnow()
    strategy_template_id = str(candidate.get("strategy_template_id") or candidate.get("template_id") or "growth_cycle_index_enhanced")
    preset_params = _preset_params_from_candidate(candidate)
    params_hash = _params_hash(preset_params)
    existing_preset = collections["portfolio_strategy_param_presets"].find_one(
        {
            "user_id": user_id,
            "strategy_template_id": strategy_template_id,
            "status": {"$in": ["draft", "enabled", "recommended"]},
            "$or": [{"params_hash": params_hash}, {"params": preset_params}],
        }
    )
    if existing_preset:
        preset_id = existing_preset["preset_id"]
        evidence = _research_evidence(job, result, existing_preset.get("status") or request.status, request.comment)
        collections["portfolio_strategy_param_presets"].update_one(
            {"preset_id": preset_id, "user_id": user_id},
            {
                "$set": {
                    "params_hash": params_hash,
                    "updated_at": now,
                    "latest_evidence": evidence,
                    "latest_research_job_id": job["job_id"],
                    "latest_research_result_id": result_id,
                },
                "$push": {"evidence": evidence},
            },
        )
        review = {
            "review_id": str(uuid.uuid4()),
            "job_id": job["job_id"],
            "research_result_id": result_id,
            "preset_id": preset_id,
            "strategy_template_id": strategy_template_id,
            "decision": "evidence_attached",
            "status": existing_preset.get("status") or request.status,
            "comment": request.comment,
            "reviewer": _reviewer(current_user),
            "user_id": user_id,
            "created_at": now,
        }
        collections["portfolio_research_reviews"].insert_one(review)
        collections["portfolio_research_results"].update_one(
            {"result_id": result_id},
            {
                "$set": {
                    "published_preset_id": preset_id,
                    "published_status": existing_preset.get("status") or request.status,
                    "published_at": now,
                    "publish_action": "evidence_attached",
                }
            },
        )
        return {
            "success": True,
            "data": _jsonable({"preset": existing_preset, "review": review, "attached_to_existing": True}),
        }

    requested_id = request.preset_id or candidate.get("preset_id") or f"research_{result_id[:8]}"
    preset_id = _unique_preset_id(collections, str(requested_id), request.create_new_version)
    evidence = _research_evidence(job, result, request.status, request.comment)
    preset = {
        "preset_id": preset_id,
        "strategy_template_id": strategy_template_id,
        "user_id": user_id,
        "name": request.name or candidate.get("name") or preset_id,
        "description": request.comment,
        "params": preset_params,
        "params_hash": params_hash,
        "status": request.status,
        "is_default": False,
        "priority": 100,
        "source": "portfolio_research",
        "research_job_id": job["job_id"],
        "research_result_id": result_id,
        "backtest_summary": result.get("best_row"),
        "data_window": {"start_date": job.get("start_date"), "end_date": job.get("end_date")},
        "data_watermark_snapshot": result.get("data_watermark"),
        "evidence": [evidence],
        "latest_evidence": evidence,
        "latest_research_job_id": job["job_id"],
        "latest_research_result_id": result_id,
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
        {
            "$set": {
                "published_preset_id": preset_id,
                "published_status": request.status,
                "published_at": now,
                "publish_action": "preset_published",
            }
        },
    )
    return {"success": True, "data": _jsonable({"preset": preset, "review": review, "attached_to_existing": False})}


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
