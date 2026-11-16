"""Read-only Model Registry API (``quant_ml.models``).

Phase 0 backfilled one document per model_id. This router exposes the factory
view: state counts, a filterable catalog, and one model's full lifecycle
evidence. It deliberately has no transition endpoints yet; validate/promote/
retire land in Phase 3.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from pymongo import DESCENDING

from auth import get_current_user
from db import get_ml_db_collections

router = APIRouter(prefix="/model-registry", tags=["model-registry"])

STATE_ORDER = ("live", "shadow", "validated", "research", "retired", "rejected")
STATE_RANK = {name: index for index, name in enumerate(STATE_ORDER)}


def _models():
    return get_ml_db_collections()["models"]


def _parse_states(value: Optional[str]) -> Optional[List[str]]:
    if not value:
        return None
    text = str(value).strip()
    if text.lower() in {"all", "*"}:
        return None
    values = [part.strip() for part in text.split(",") if part.strip()]
    return values or None


def _list_row(doc: Dict[str, Any]) -> Dict[str, Any]:
    latest = doc.get("latest_validation") or {}
    gate = doc.get("operational_gate") or {}
    health = doc.get("health") or {}
    return {
        "model_id": doc.get("model_id") or doc.get("_id"),
        "display_name": doc.get("display_name"),
        "family_id": doc.get("family_id"),
        "index_code": doc.get("index_code"),
        "state": doc.get("state"),
        "accepted": bool(doc.get("accepted")),
        "accepted_reasons": list(doc.get("accepted_reasons") or []),
        "state_flags": doc.get("state_flags") or {},
        "validation": (
            {"protocol": latest.get("protocol"), "verdict": latest.get("verdict")}
            if latest
            else None
        ),
        "operational_gate": (
            {"passed": gate.get("passed"), "reason": gate.get("reason")}
            if gate
            else None
        ),
        "health": {
            "last_prediction_date": health.get("last_prediction_date"),
            "date_count": int(health.get("date_count") or 0),
            "prediction_count": int(health.get("prediction_count") or 0),
            "stale_days": health.get("stale_days"),
            "is_current": bool(health.get("is_current")),
            "active": bool(health.get("active")),
        },
        "artifact_count": len(doc.get("artifacts") or []),
        "job_count": len(doc.get("training_jobs") or []),
        "shadow_profile_count": len(doc.get("shadow_profiles") or []),
        "activation_count": len(doc.get("activations") or []),
        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
    }


@router.get("/summary")
def model_registry_summary(
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """State board counts for the model factory page."""
    docs = list(
        _models().find(
            {},
            {"state": 1, "accepted": 1, "health": 1, "backfilled_at": 1},
        )
    )
    states = {name: 0 for name in STATE_ORDER}
    accepted = 0
    active_stale = 0
    latest_backfill = None
    for doc in docs:
        state = str(doc.get("state") or "research")
        states[state] = states.get(state, 0) + 1
        if doc.get("accepted"):
            accepted += 1
        health = doc.get("health") or {}
        if health.get("active") and not health.get("is_current"):
            active_stale += 1
        backfilled_at = doc.get("backfilled_at")
        if backfilled_at and (latest_backfill is None or backfilled_at > latest_backfill):
            latest_backfill = backfilled_at
    return {
        "data": {
            "total": len(docs),
            "accepted": accepted,
            "states": states,
            "active_stale": active_stale,
            "last_backfilled_at": latest_backfill,
            "generated_at": datetime.utcnow(),
        }
    }


@router.get("/models")
def list_registry_models(
    state: Optional[str] = Query(default=None, description="Comma-separated state filter, or all"),
    index_code: Optional[str] = Query(default=None),
    family_id: Optional[str] = Query(default=None),
    accepted: Optional[bool] = Query(default=None),
    q: Optional[str] = Query(default=None, description="model_id / family / display name search"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    query: Dict[str, Any] = {}
    states = _parse_states(state)
    if states:
        query["state"] = {"$in": states}
    if index_code:
        query["index_code"] = str(index_code)
    if family_id:
        query["family_id"] = str(family_id)
    if accepted is not None:
        query["accepted"] = bool(accepted)
    if q and str(q).strip():
        pattern = re.escape(str(q).strip())
        query["$or"] = [
            {"model_id": {"$regex": pattern, "$options": "i"}},
            {"display_name": {"$regex": pattern, "$options": "i"}},
            {"family_id": {"$regex": pattern, "$options": "i"}},
        ]

    docs = list(_models().find(query).sort("updated_at", DESCENDING))
    docs.sort(
        key=lambda doc: (
            STATE_RANK.get(str(doc.get("state") or ""), len(STATE_ORDER)),
            str(doc.get("family_id") or ""),
            str(doc.get("model_id") or doc.get("_id") or ""),
        )
    )
    total = len(docs)
    page = docs[offset : offset + limit]
    return {"data": [_list_row(doc) for doc in page], "total": total}


@router.get("/models/{model_id}")
def get_registry_model(
    model_id: str,
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    doc = _models().find_one({"model_id": model_id})
    if not doc:
        doc = _models().find_one({"_id": model_id})
    if not doc:
        raise HTTPException(status_code=404, detail="model not found")
    payload = dict(doc)
    payload.pop("_id", None)
    return {"data": jsonable_encoder(payload)}
