"""Model registry lifecycle transitions with audit.

The registry document in ``quant_ml.models`` is the source of truth for a
model's lifecycle state. This service owns the allowed state machine and
writes every transition into ``state_history``; callers must supply a reason
and, for evidence-bearing transitions, the validation/activation/profile
reference.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable, Optional

from fastapi import HTTPException

STATE_ORDER = ("live", "shadow", "validated", "research", "retired", "rejected")

ALLOWED_TRANSITIONS: Dict[str, set[str]] = {
    "research": {"validated", "rejected", "retired"},
    "validated": {"shadow", "rejected", "retired"},
    "shadow": {"live", "validated", "retired"},
    "live": {"shadow", "validated", "retired"},
    "retired": set(),
    "rejected": set(),
}

TERMINAL_STATES = {"retired", "rejected"}


def _shadow_enabled(doc: Dict[str, Any]) -> bool:
    return any(
        profile.get("enabled") is not False
        for profile in (doc.get("shadow_profiles") or [])
    )


def _find_model(models, model_id: str) -> Dict[str, Any]:
    doc = models.find_one({"model_id": model_id})
    if not doc:
        doc = models.find_one({"_id": model_id})
    if not doc:
        raise HTTPException(status_code=404, detail="model not found")
    return doc


def _allowed_activation(activations, model_id: str, activation_id: str) -> bool:
    if activations is None or not activation_id:
        return False
    doc = activations.find_one({"activation_id": activation_id})
    if not doc:
        return False
    params = doc.get("params_override") or {}
    return (
        str(params.get("model_id") or "") == model_id
        and str(doc.get("execution_venue") or "") == "live"
        and str(doc.get("status") or "") in {"active", "scheduled"}
    )


def _allowed_shadow_profile(shadow_profiles, doc: Dict[str, Any], model_id: str, profile_id: str) -> bool:
    if profile_id:
        for profile in doc.get("shadow_profiles") or []:
            if (
                str(profile.get("profile_id") or "") == profile_id
                and profile.get("enabled") is not False
            ):
                return True
        if shadow_profiles is not None:
            found = shadow_profiles.find_one({"profile_id": profile_id})
            if found and str(found.get("model_id") or (found.get("params") or {}).get("model_id") or "") == model_id and found.get("enabled") is not False:
                return True
        return False
    return _shadow_enabled(doc)


def _validation_evidence(evidence: Optional[Dict[str, Any]], admin_override: bool) -> None:
    if admin_override:
        return
    evidence = evidence or {}
    if evidence.get("demotion") is True:
        return
    if not (evidence.get("result_id") or evidence.get("validation_id") or evidence.get("protocol")):
        raise HTTPException(
            status_code=409,
            detail={
                "message": "validated transition requires evidence.result_id or evidence.validation_id (or admin_override=true)",
                "evidence": evidence,
            },
        )


def transition_model(
    models,
    model_id: str,
    target_state: str,
    *,
    actor: str,
    reason: str,
    evidence: Optional[Dict[str, Any]] = None,
    expected_state: Optional[str] = None,
    admin_override: bool = False,
    shadow_profiles=None,
    activations=None,
) -> Dict[str, Any]:
    """Apply one lifecycle transition and return the updated model document."""
    target = str(target_state or "").strip()
    if target not in STATE_ORDER:
        raise HTTPException(status_code=400, detail=f"unknown target_state: {target_state}")
    if not str(reason or "").strip():
        raise HTTPException(status_code=400, detail="transition requires a non-empty reason")

    doc = _find_model(models, model_id)
    current = str(doc.get("state") or "research")
    if expected_state and current != expected_state:
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"model state changed: expected {expected_state}, found {current}",
                "current_state": current,
            },
        )
    if target == current:
        return doc

    allowed = ALLOWED_TRANSITIONS.get(current, set())
    if current in TERMINAL_STATES and not admin_override:
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"model is {current}; admin_override required to revive it",
                "current_state": current,
                "target_state": target,
            },
        )
    if target not in allowed and not admin_override:
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"transition {current} -> {target} is not allowed",
                "current_state": current,
                "target_state": target,
                "allowed": sorted(allowed),
            },
        )

    evidence = dict(evidence or {})
    if target == "validated":
        _validation_evidence(evidence, admin_override)
    elif target == "shadow":
        profile_id = str(evidence.get("profile_id") or "").strip()
        if not _allowed_shadow_profile(shadow_profiles, doc, model_id, profile_id) and not admin_override:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "shadow transition requires an enabled shadow profile (or admin_override=true)",
                    "model_id": model_id,
                },
            )
    elif target == "live":
        activation_id = str(evidence.get("activation_id") or "").strip()
        if not _allowed_activation(activations, model_id, activation_id) and not admin_override:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "live transition requires an active/scheduled live activation for this model (or admin_override=true)",
                    "model_id": model_id,
                },
            )

    now = datetime.utcnow()
    shadow_enabled = _shadow_enabled(doc)
    if target == "shadow":
        shadow_enabled = True
    elif target == "live":
        shadow_enabled = _shadow_enabled(doc)
    else:
        shadow_enabled = False
    live_active = target == "live"
    reasons = []
    if live_active:
        reasons.append("live")
    if shadow_enabled and target in ("shadow", "live"):
        reasons.append("shadow")

    validated_flag = bool((doc.get("state_flags") or {}).get("validated"))
    if target == "validated":
        validated_flag = True
    elif target in ("research", "rejected"):
        validated_flag = False

    updates: Dict[str, Any] = {
        "state": target,
        "accepted": bool(reasons),
        "accepted_reasons": reasons,
        "state_flags": {
            "live_active": live_active,
            "shadow_enabled": shadow_enabled,
            "shadow_profile_any": bool(doc.get("shadow_profiles")),
            "validated": validated_flag,
        },
        "updated_at": now,
    }
    health = dict(doc.get("health") or {})
    health["active"] = target in ("live", "shadow")
    updates["health"] = health

    history_entry = {
        "from": current,
        "to": target,
        "at": now,
        "actor": actor,
        "reason": str(reason).strip(),
        "evidence": evidence,
        "admin_override": bool(admin_override),
    }
    updates["state_history"] = list(doc.get("state_history") or []) + [history_entry]

    if target == "validated" and evidence.get("demotion") is not True:
        validation_id = str(evidence.get("validation_id") or "").strip()
        if not validation_id:
            validation_id = f"{model_id}:manual:{now.strftime('%Y%m%d%H%M%S')}"
        validation_run = {
            "validation_id": validation_id,
            "protocol": evidence.get("protocol") or "manual",
            "scope": evidence.get("scope") or "primary",
            "job_id": evidence.get("job_id"),
            "result_id": evidence.get("result_id"),
            "anchor_model_id": evidence.get("anchor_model_id"),
            "window": evidence.get("window") or {},
            "metrics": evidence.get("metrics") or {},
            "verdict": evidence.get("verdict") or "pass",
            "caveats": evidence.get("caveats") or [],
            "created_at": now,
            "actor": actor,
            "reason": str(reason).strip(),
        }
        updates["validation_runs"] = list(doc.get("validation_runs") or []) + [validation_run]
        updates["latest_validation"] = {
            "protocol": validation_run["protocol"],
            "verdict": validation_run["verdict"],
            "metrics": validation_run["metrics"],
            "result_id": validation_run["result_id"],
            "created_at": now,
        }

    models.update_one({"_id": doc["_id"]}, {"$set": updates})
    doc.update(updates)
    return doc
