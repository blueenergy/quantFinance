"""Model training jobs API (quant_ml). Drained by quant-model-trainer worker."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from pymongo import ASCENDING, DESCENDING

from auth import get_current_user
from db import get_ml_db_collections

router = APIRouter(prefix="/model-training", tags=["model-training"])
logger = logging.getLogger(__name__)

UniverseIndex = Literal["hs300", "a500", "csi1000", "csi2000"]
ModelKind = Literal["rule_baseline", "ridge", "elasticnet", "lightgbm"]
DataSource = Literal["synthetic", "production_scores"]


class CreateModelTrainingJobRequest(BaseModel):
    name: str = Field(default="ML训练", max_length=200)
    index_code: UniverseIndex = Field(default="csi1000")
    model_kind: ModelKind = Field(default="ridge")
    data_source: DataSource = Field(default="synthetic")
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    parquet_path: Optional[str] = None
    snapshot_id: Optional[str] = None
    train_days: int = Field(default=60, ge=5, le=500)
    test_days: int = Field(default=20, ge=1, le=120)
    purge_days: int = Field(default=60, ge=0, le=120)


@router.post("/jobs")
def create_model_training_job(
    body: CreateModelTrainingJobRequest,
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    collections = get_ml_db_collections()
    job_id = str(uuid.uuid4())
    now = datetime.utcnow()
    doc = {
        "job_id": job_id,
        "user_id": str(current_user.get("id") or ""),
        "name": body.name,
        "status": "pending",
        "created_at": now,
        "params": body.model_dump(),
    }
    collections["model_training_jobs"].insert_one(doc)
    return {"data": {"job_id": job_id, "status": "pending"}}


@router.get("/jobs")
def list_model_training_jobs(
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    collections = get_ml_db_collections()
    query: Dict[str, Any] = {"user_id": str(current_user.get("id") or "")}
    if status:
        query["status"] = status
    rows = list(
        collections["model_training_jobs"]
        .find(query, {"_id": 0})
        .sort("created_at", DESCENDING)
        .limit(limit)
    )
    return {"data": rows}


@router.get("/jobs/{job_id}")
def get_model_training_job(job_id: str, current_user: dict = Depends(get_current_user)) -> Dict[str, Any]:
    collections = get_ml_db_collections()
    doc = collections["model_training_jobs"].find_one({"job_id": job_id, "user_id": str(current_user.get("id") or "")}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="job not found")
    return {"data": doc}


@router.get("/jobs/{job_id}/results")
def get_model_training_results(job_id: str, current_user: dict = Depends(get_current_user)) -> Dict[str, Any]:
    collections = get_ml_db_collections()
    job = collections["model_training_jobs"].find_one({"job_id": job_id, "user_id": str(current_user.get("id") or "")})
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    result_id = job.get("result_id")
    if not result_id:
        raise HTTPException(status_code=404, detail="results not ready")
    doc = collections["model_training_results"].find_one({"result_id": result_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="results not found")
    return {"data": doc}
