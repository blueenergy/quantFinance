"""Model training jobs API (quant_ml). Drained by quant-model-trainer worker."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from pymongo import DESCENDING

from auth import get_current_user
from db import get_ml_db_collections, get_trading_db_collections
from services.model_scores_daily import accepted_model_reasons
from services.model_training_promotion import (
    build_promotion_context,
    find_recipe_sibling_jobs,
    recipe_fingerprint,
)

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


def _snapshot_parquet_path(doc: Dict[str, Any]) -> Optional[str]:
    path = doc.get("parquet_path") or doc.get("parquet_uri")
    if not path:
        return None
    text = str(path).strip()
    return text or None


def _serialize_snapshot_row(doc: Dict[str, Any]) -> Dict[str, Any]:
    created = doc.get("created_at")
    if isinstance(created, datetime):
        created_at = created.isoformat() + "Z"
    else:
        created_at = created
    parquet_path = _snapshot_parquet_path(doc)
    return {
        "snapshot_id": doc.get("snapshot_id"),
        "index_code": doc.get("index_code"),
        "start_date": doc.get("start_date"),
        "end_date": doc.get("end_date"),
        "row_count": doc.get("row_count"),
        "parquet_path": parquet_path,
        "created_at": created_at,
    }


@router.get("/snapshots")
def list_dataset_snapshots(
    index_code: Optional[UniverseIndex] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Registered Parquet manifests from quant_ml.dataset_manifests (materialize job)."""
    collections = get_ml_db_collections()
    query: Dict[str, Any] = {"snapshot_id": {"$exists": True, "$ne": None}}
    if index_code:
        query["index_code"] = index_code
    cursor = (
        collections["dataset_manifests"]
        .find(query, {"_id": 0})
        .sort("created_at", DESCENDING)
        .limit(limit)
    )
    rows: List[Dict[str, Any]] = []
    for doc in cursor:
        row = _serialize_snapshot_row(doc)
        if row.get("snapshot_id") and row.get("parquet_path"):
            rows.append(row)
    return {"data": rows}


def _serialize_prediction_model_row(
    doc: Dict[str, Any],
    accepted_reasons: Optional[List[str]] = None,
    registry_doc: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    reasons = list(accepted_reasons or [])
    state = (registry_doc or {}).get("state")
    validated = state == "validated"
    validation = (registry_doc or {}).get("latest_validation") or {}
    return {
        "model_id": doc.get("_id"),
        "index_code": doc.get("index_code"),
        "first_trade_date": doc.get("first_trade_date"),
        "last_trade_date": doc.get("last_trade_date"),
        "date_count": int(doc.get("date_count") or 0),
        "prediction_count": int(doc.get("prediction_count") or 0),
        "research": bool(doc.get("research")),
        "shadow": bool(doc.get("shadow")),
        "source": doc.get("source"),
        "window": doc.get("window"),
        "accepted": bool(reasons),
        "accepted_reasons": reasons,
        "state": state,
        "validated": validated,
        "validation": (
            {"protocol": validation.get("protocol"), "verdict": validation.get("verdict")}
            if validation
            else None
        ),
    }


@router.get("/prediction-models")
def list_prediction_models(
    index_code: Optional[UniverseIndex] = Query(default=None),
    kind: Literal["all", "research", "shadow"] = Query(default="all"),
    accepted_only: bool = False,
    include_validated: bool = False,
    limit: int = Query(default=100, ge=1, le=500),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Model ids available in ``quant_ml.model_predictions`` with date coverage.

    Portfolio research selects a model by ``model_id``; listing training jobs is
    not enough because only materialised / shadowed models have prediction rows,
    and the coverage decides whether a requested backtest window is usable.

    ``accepted_only`` keeps models the platform currently runs: an active
    strategy activation (live) or an enabled shadow profile. ``include_validated``
    additionally keeps models whose registry state is ``validated`` (research
    OOS evidence, not live/shadow). The registry in ``quant_ml.models`` is the
    source of truth; the older profile/activation derivation remains as a
    fallback for deployments that have not run the Phase 0 backfill.
    """
    collections = get_ml_db_collections()
    trading_collections = get_trading_db_collections()
    fallback_reasons = accepted_model_reasons(
        collections.get("model_shadow_profiles"),
        trading_collections.get("portfolio_strategy_activations"),
    )
    registry_docs = {}
    models_coll = collections.get("models")
    if models_coll is not None:
        registry_docs = {
            str(doc.get("model_id") or doc.get("_id")): doc
            for doc in models_coll.find(
                {},
                {
                    "model_id": 1,
                    "state": 1,
                    "accepted": 1,
                    "accepted_reasons": 1,
                    "latest_validation": 1,
                },
            )
            if doc.get("model_id") or doc.get("_id")
        }

    def reasons_for(model_id: str) -> List[str]:
        if model_id in registry_docs:
            return list(registry_docs[model_id].get("accepted_reasons") or [])
        return list(fallback_reasons.get(model_id, []))

    accepted_ids = {
        model_id
        for model_id in set(registry_docs) | set(fallback_reasons)
        if reasons_for(model_id)
    }
    validated_ids = {
        model_id
        for model_id, doc in registry_docs.items()
        if doc.get("state") == "validated"
    }
    allowed_ids = None
    if accepted_only:
        allowed_ids = set(accepted_ids)
        if include_validated:
            allowed_ids |= validated_ids
        if not allowed_ids:
            return {"data": []}

    match: Dict[str, Any] = {"model_id": {"$exists": True, "$nin": [None, ""]}}
    if allowed_ids is not None:
        match["model_id"] = {"$in": sorted(allowed_ids)}
    if index_code:
        match["index_code"] = index_code
    if kind == "research":
        match["research"] = True
    elif kind == "shadow":
        match["shadow"] = True

    pipeline = [
        {"$match": match},
        # First collapse to (model, date) so date_count is exact without
        # materialising a large set of dates in memory.
        {
            "$group": {
                "_id": {"model_id": "$model_id", "trade_date": "$trade_date"},
                "prediction_count": {"$sum": 1},
                "index_code": {"$max": "$index_code"},
                "research": {"$max": "$research"},
                "shadow": {"$max": "$shadow"},
                "source": {"$max": "$source"},
                "window": {"$max": "$window"},
            }
        },
        {
            "$group": {
                "_id": "$_id.model_id",
                "first_trade_date": {"$min": "$_id.trade_date"},
                "last_trade_date": {"$max": "$_id.trade_date"},
                "date_count": {"$sum": 1},
                "prediction_count": {"$sum": "$prediction_count"},
                "index_code": {"$max": "$index_code"},
                "research": {"$max": "$research"},
                "shadow": {"$max": "$shadow"},
                "source": {"$max": "$source"},
                "window": {"$max": "$window"},
            }
        },
        {"$sort": {"last_trade_date": DESCENDING, "_id": 1}},
        {"$limit": limit},
    ]
    rows = [
        _serialize_prediction_model_row(
            doc,
            reasons_for(str(doc.get("_id"))),
            registry_docs.get(str(doc.get("_id"))),
        )
        for doc in collections["model_predictions"].aggregate(pipeline)
    ]
    if allowed_ids is not None:
        # ``$match`` already filters in production; the Python guard keeps the
        # contract true for test doubles and any pipeline that ignores ``$in``.
        rows = [row for row in rows if row["model_id"] in allowed_ids]
    return {"data": rows}


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


@router.get("/jobs/{job_id}/promotion-context")
def get_model_training_promotion_context(
    job_id: str,
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Single-pool vs recipe-matched multi-pool promotion (cross-job, same user)."""
    collections = get_ml_db_collections()
    user_id = str(current_user.get("id") or "")
    job = collections["model_training_jobs"].find_one(
        {"job_id": job_id, "user_id": user_id},
        {"_id": 0},
    )
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    result: Optional[Dict[str, Any]] = None
    result_id = job.get("result_id")
    if result_id:
        result = collections["model_training_results"].find_one({"result_id": result_id}, {"_id": 0})
    params = job.get("params") or {}
    fingerprint = recipe_fingerprint(params, code_version=(result or {}).get("code_version"))
    siblings = find_recipe_sibling_jobs(
        collections,
        user_id=user_id,
        fingerprint=fingerprint,
        code_version=(result or {}).get("code_version"),
    )
    if not any(j.get("job_id") == job_id for j in siblings):
        siblings = [job] + [j for j in siblings if j.get("job_id") != job_id]
    sibling_results: Dict[str, Dict[str, Any]] = {}
    for sib in siblings:
        rid = sib.get("result_id")
        if not rid:
            continue
        doc = collections["model_training_results"].find_one({"result_id": rid}, {"_id": 0})
        if doc and sib.get("job_id"):
            sibling_results[str(sib["job_id"])] = doc
    ctx = build_promotion_context(job, result, siblings, sibling_results)
    return {"data": ctx}


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
