"""Read-only Model Registry API (``quant_ml.models``).

Phase 0 backfilled one document per model_id. This router exposes the factory
view: state counts, a filterable catalog, and one model's full lifecycle
evidence. It deliberately has no transition endpoints yet; validate/promote/
retire land in Phase 3.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field
from pymongo import DESCENDING

from auth import get_current_admin_user, get_current_user
from db import get_ml_db_collections, get_trading_db_collections
from services.model_registry_lifecycle import ALLOWED_TRANSITIONS, transition_model

router = APIRouter(prefix="/model-registry", tags=["model-registry"])

STATE_ORDER = ("live", "shadow", "validated", "research", "retired", "rejected")
STATE_RANK = {name: index for index, name in enumerate(STATE_ORDER)}


class ModelTransitionRequest(BaseModel):
    target_state: Literal["research", "validated", "shadow", "live", "retired", "rejected"]
    reason: str = Field(min_length=3, max_length=1000)
    evidence: Optional[Dict[str, Any]] = None
    expected_state: Optional[str] = None
    admin_override: bool = False
    disable_shadow_profiles: bool = False
    cancel_activations: bool = False


def _models():
    return get_ml_db_collections()["models"]


def _find_model_doc(model_id: str) -> Optional[Dict[str, Any]]:
    models = _models()
    doc = models.find_one({"model_id": model_id})
    if not doc:
        doc = models.find_one({"_id": model_id})
    if not doc:
        doc = models.find_one({"legacy.aliases": model_id})
    return doc


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
            "status": health.get("status"),
            "score_drift_z": health.get("score_drift_z"),
            "checked_at": health.get("checked_at"),
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
    latest_health_check = None
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
        checked_at = (doc.get("health") or {}).get("checked_at")
        if checked_at and (latest_health_check is None or checked_at > latest_health_check):
            latest_health_check = checked_at
    return {
        "data": {
            "total": len(docs),
            "accepted": accepted,
            "states": states,
            "active_stale": active_stale,
            "last_backfilled_at": latest_backfill,
            "last_health_check_at": latest_health_check,
            "generated_at": datetime.utcnow(),
        }
    }


@router.get("/models")
def list_registry_models(
    state: Optional[str] = None,
    index_code: Optional[str] = None,
    family_id: Optional[str] = None,
    accepted: Optional[bool] = None,
    factor: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
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
    if factor and str(factor).strip():
        query["feature_factors.factor_id"] = str(factor).strip()
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
    doc = _find_model_doc(model_id)
    if not doc:
        raise HTTPException(status_code=404, detail="model not found")
    payload = dict(doc)
    payload.pop("_id", None)
    return {"data": jsonable_encoder(payload)}


@router.get("/families")
def list_registry_families(
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Group model versions by ``family_id`` for lineage-oriented views."""
    docs = list(
        _models().find(
            {},
            {"model_id": 1, "family_id": 1, "state": 1, "updated_at": 1, "legacy": 1},
        )
    )
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for doc in docs:
        family_id = str(doc.get("family_id") or doc.get("model_id") or doc.get("_id") or "")
        if not family_id:
            continue
        grouped.setdefault(family_id, []).append(
            {
                "model_id": doc.get("model_id") or doc.get("_id"),
                "state": doc.get("state"),
                "updated_at": doc.get("updated_at"),
                "legacy_aliases": (doc.get("legacy") or {}).get("aliases") or [],
            }
        )
    families = []
    for family_id, models in grouped.items():
        state_counts: Dict[str, int] = {}
        for model in models:
            state = str(model.get("state") or "research")
            state_counts[state] = state_counts.get(state, 0) + 1
        families.append(
            {
                "family_id": family_id,
                "model_count": len(models),
                "state_counts": state_counts,
                "models": sorted(models, key=lambda item: str(item.get("model_id") or "")),
            }
        )
    families.sort(key=lambda item: item["family_id"])
    return {"data": families}


@router.get("/models/{model_id}/allowed-transitions")
def get_registry_allowed_transitions(
    model_id: str,
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    doc = _find_model_doc(model_id)
    if not doc:
        raise HTTPException(status_code=404, detail="model not found")
    state = str(doc.get("state") or "research")
    return {
        "data": {
            "state": state,
            "allowed": sorted(ALLOWED_TRANSITIONS.get(state, set())),
            "terminal": state in {"retired", "rejected"},
        }
    }


@router.post("/models/{model_id}/transition")
def transition_registry_model(
    model_id: str,
    request: ModelTransitionRequest,
    current_user: dict = Depends(get_current_admin_user),
) -> Dict[str, Any]:
    """Apply an audited lifecycle transition (admin only)."""
    ml = get_ml_db_collections()
    trading = get_trading_db_collections()
    doc = transition_model(
        ml["models"],
        model_id,
        request.target_state,
        actor=str(current_user.get("username") or current_user.get("id") or "admin"),
        reason=request.reason,
        evidence=request.evidence,
        expected_state=request.expected_state,
        admin_override=request.admin_override,
        shadow_profiles=ml.get("model_shadow_profiles"),
        activations=trading.get("portfolio_strategy_activations"),
        disable_shadow_profiles=request.disable_shadow_profiles,
        cancel_activations=request.cancel_activations,
    )
    payload = dict(doc)
    payload.pop("_id", None)
    return {"success": True, "data": jsonable_encoder(payload)}
