"""Walk-forward promotion: single index pool vs multi-pool group (recipe-matched jobs)."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional, Sequence

PROMOTION_MIN_WALK_FORWARD_FOLDS = 3
PROMOTION_MIN_UNIVERSES = 2
PROMOTION_PERMUTATION_P = 0.05


def recipe_fingerprint(params: Dict[str, Any], *, code_version: Optional[str] = None) -> str:
    """Stable key for jobs that share training recipe (not per-pool artifact_hash)."""
    payload = {
        "model_kind": params.get("model_kind"),
        "train_days": params.get("train_days"),
        "test_days": params.get("test_days"),
        "purge_days": params.get("purge_days"),
        "code_version": code_version or params.get("code_version"),
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _model_mean_ic_from_folds(fold_reports: Sequence[Dict[str, Any]]) -> Optional[float]:
    vals: List[float] = []
    for fold in fold_reports or []:
        model = fold.get("model") if isinstance(fold, dict) else None
        if not isinstance(model, dict):
            continue
        ic = model.get("mean_ic")
        if ic is not None:
            try:
                vals.append(float(ic))
            except (TypeError, ValueError):
                continue
    if not vals:
        return None
    return sum(vals) / len(vals)


def _baseline_mean_ic(result: Dict[str, Any]) -> Optional[float]:
    promo = result.get("promotion") or {}
    if promo.get("baseline_mean_ic") is not None:
        return float(promo["baseline_mean_ic"])
    baseline = result.get("baseline_ic") or {}
    if baseline.get("mean_ic") is not None:
        return float(baseline["mean_ic"])
    if promo.get("baseline") is not None:
        return float(promo["baseline"])
    return None


def evaluate_single_pool_promotion(result: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Per-index gates (walk-forward, IC vs baseline, permutation); ignores universe_count."""
    if not result:
        return {"passed": False, "reason": "no_result"}
    fold_reports = result.get("fold_reports") or []
    model_folds = [f.get("model") for f in fold_reports if isinstance(f, dict) and f.get("model")]
    if len(model_folds) < PROMOTION_MIN_WALK_FORWARD_FOLDS:
        return {"passed": False, "reason": "insufficient_walk_forward_folds", "fold_count": len(model_folds)}
    model_mean = _model_mean_ic_from_folds(fold_reports)
    if model_mean is None:
        return {"passed": False, "reason": "no_ic"}
    baseline_mean = _baseline_mean_ic(result)
    if baseline_mean is None:
        baseline_mean = 0.0
    if model_mean <= 0 or model_mean < baseline_mean:
        return {
            "passed": False,
            "reason": "ic_below_baseline",
            "model_mean_ic": model_mean,
            "baseline_mean_ic": baseline_mean,
        }
    perm_p = result.get("permutation_p")
    try:
        perm_val = float(perm_p) if perm_p is not None else 1.0
    except (TypeError, ValueError):
        perm_val = 1.0
    if perm_val > PROMOTION_PERMUTATION_P:
        return {
            "passed": False,
            "reason": "permutation_failed",
            "p_value": perm_val,
            "model_mean_ic": model_mean,
            "baseline_mean_ic": baseline_mean,
        }
    return {
        "passed": True,
        "model_mean_ic": model_mean,
        "baseline_mean_ic": baseline_mean,
        "permutation_p": perm_val,
        "fold_count": len(model_folds),
    }


def _member_summary(job: Dict[str, Any], result: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    params = job.get("params") or {}
    single = evaluate_single_pool_promotion(result)
    return {
        "job_id": job.get("job_id"),
        "index_code": params.get("index_code"),
        "name": job.get("name"),
        "status": job.get("status"),
        "single_pool": single,
        "model_kind": params.get("model_kind") or (result or {}).get("model_kind"),
        "snapshot_id": params.get("snapshot_id") or (result or {}).get("snapshot_id"),
    }


def build_promotion_context(
    job: Dict[str, Any],
    result: Optional[Dict[str, Any]],
    sibling_jobs: Sequence[Dict[str, Any]],
    sibling_results: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    params = job.get("params") or {}
    code_version = (result or {}).get("code_version")
    fingerprint = recipe_fingerprint(params, code_version=code_version)
    single_pool = evaluate_single_pool_promotion(result)

    members: List[Dict[str, Any]] = []
    seen_indices: set[str] = set()
    for member_job in sibling_jobs:
        jid = member_job.get("job_id")
        if not jid:
            continue
        member_result = sibling_results.get(jid)
        summary = _member_summary(member_job, member_result)
        idx = summary.get("index_code")
        if idx in seen_indices:
            continue
        seen_indices.add(idx)
        members.append(summary)

    members.sort(key=lambda m: (str(m.get("index_code") or ""), str(m.get("job_id") or "")))

    passed_members = [m for m in members if m.get("single_pool", {}).get("passed")]
    passed_index_codes = sorted({str(m.get("index_code")) for m in passed_members if m.get("index_code")})
    passed_pool_count = len(passed_index_codes)
    group_passed = passed_pool_count >= PROMOTION_MIN_UNIVERSES

    return {
        "single_pool": single_pool,
        "group": {
            "passed": group_passed,
            "required_pools": PROMOTION_MIN_UNIVERSES,
            "passed_pool_count": passed_pool_count,
            "passed_index_codes": passed_index_codes,
            "recipe_fingerprint": fingerprint,
            "members": members,
        },
    }


def find_recipe_sibling_jobs(
    collections: Dict[str, Any],
    *,
    user_id: str,
    fingerprint: str,
    code_version: Optional[str],
    limit: int = 80,
) -> List[Dict[str, Any]]:
    """Completed jobs for user with same recipe fingerprint (may include multiple index_code)."""
    jobs_coll = collections["model_training_jobs"]
    results_coll = collections["model_training_results"]
    cursor = (
        jobs_coll.find(
            {"user_id": user_id, "status": "completed", "result_id": {"$exists": True, "$ne": None}},
            {"_id": 0},
        )
        .sort("created_at", -1)
        .limit(limit)
    )
    matched: List[Dict[str, Any]] = []
    for doc in cursor:
        params = doc.get("params") or {}
        result_id = doc.get("result_id")
        result = results_coll.find_one({"result_id": result_id}, {"_id": 0}) if result_id else None
        cv = (result or {}).get("code_version")
        fp = recipe_fingerprint(params, code_version=cv)
        if fp == fingerprint:
            matched.append(doc)
    return matched
