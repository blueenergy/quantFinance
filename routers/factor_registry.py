"""Read-only Factor Registry API.

``quant_trading.factor_registry`` is one document per factor (definition +
status + coverage + latest verdict); ``quant_trading.factor_tests`` is the
append-only evidence ledger. ``quant_ml.models`` links models back to the
factors they use. This router only reads those three collections.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user
from db import get_ml_db_collections, get_trading_db_collections

router = APIRouter(prefix="/factor-registry", tags=["factor-registry"])

STATUS_ORDER = (
    "candidate", "confirmed", "in_use", "inconclusive",
    "screened", "no_increment", "rejected", "untested",
)
STATUS_RANK = {name: index for index, name in enumerate(STATUS_ORDER)}
STATE_ORDER = ("live", "shadow", "validated", "research", "retired", "rejected")
STATE_RANK = {name: index for index, name in enumerate(STATE_ORDER)}


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


def _cols() -> Dict[str, Any]:
    return get_trading_db_collections()


def _split(value: Optional[str]) -> Optional[List[str]]:
    if not value:
        return None
    if str(value).strip().lower() in {"all", "*"}:
        return None
    parts = [part.strip() for part in str(value).split(",") if part.strip()]
    return parts or None


def _test_counts(registry_coll, tests_coll) -> Dict[str, int]:
    return {
        row["_id"]: int(row["n"])
        for row in tests_coll.aggregate([{"$group": {"_id": "$factor_id", "n": {"$sum": 1}}}])
    }


@router.get("/summary")
def factor_registry_summary(current_user: dict = Depends(get_current_user)) -> Dict[str, Any]:
    """Counts for the registry header: status board, sources, tested share."""
    cols = _cols()
    registry = cols["factor_registry"]
    statuses = {name: 0 for name in STATUS_ORDER}
    for row in registry.aggregate([{"$group": {"_id": "$status", "n": {"$sum": 1}}}]):
        key = row["_id"] or "untested"
        statuses[key] = statuses.get(key, 0) + int(row["n"])
    sources = {row["_id"] or "unknown": int(row["n"])
               for row in registry.aggregate([{"$group": {"_id": "$source", "n": {"$sum": 1}}}])}
    total = sum(statuses.values())
    tested = len(cols["factor_tests"].distinct("factor_id"))
    return {
        "total": total,
        "tested": tested,
        # ``untested`` is the registry status; ``no_tests`` is the raw
        # "has zero test rows" count. They differ because a factor can be
        # deliberately in_use (the raw5 baseline) without an individual test.
        "untested": statuses.get("untested", 0),
        "no_tests": max(total - tested, 0),
        "statuses": statuses,
        "sources": sources,
    }


@router.get("/factors")
def list_factors(
    status: Optional[str] = None,
    source: Optional[str] = None,
    family: Optional[str] = None,
    q: Optional[str] = None,
    tested: Optional[bool] = None,
    limit: int = 200,
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Filterable registry list with test counts and the latest verdict."""
    cols = _cols()
    registry = cols["factor_registry"]
    query: Dict[str, Any] = {}
    statuses = _split(status)
    if statuses:
        query["status"] = {"$in": statuses}
    if source:
        query["source"] = source
    if family:
        query["family"] = family
    if q:
        pattern = re.escape(str(q))
        query["$or"] = [{"_id": {"$regex": pattern, "$options": "i"}},
                        {"name": {"$regex": pattern, "$options": "i"}}]

    counts = _test_counts(registry, cols["factor_tests"])
    docs = list(registry.find(query, {
        "_id": 1, "name": 1, "source": 1, "family": 1, "status": 1,
        "coverage": 1, "latest_verdict": 1, "used_by_models": 1,
    }))
    if tested is True:
        docs = [doc for doc in docs if counts.get(doc["_id"], 0) > 0]
    elif tested is False:
        docs = [doc for doc in docs if counts.get(doc["_id"], 0) == 0]
    docs.sort(key=lambda doc: (STATUS_RANK.get(doc.get("status"), 99), doc["_id"]))
    rows = []
    for doc in docs[:limit]:
        latest = doc.get("latest_verdict") or {}
        coverage = doc.get("coverage") or {}
        rows.append({
            "factor_id": doc["_id"],
            "name": doc.get("name"),
            "source": doc.get("source"),
            "family": doc.get("family"),
            "status": doc.get("status"),
            "coverage": coverage.get("pct"),
            "coverage_by_year": coverage.get("by_year"),
            "tests": counts.get(doc["_id"], 0),
            "latest": latest.get("verdict"),
            "latest_type": latest.get("test_type"),
            "as_of": latest.get("as_of"),
            "used_by_models": len(doc.get("used_by_models") or []),
        })
    return _jsonable({"total": len(docs), "rows": rows})


@router.get("/factors/{factor_id}")
def factor_detail(factor_id: str, current_user: dict = Depends(get_current_user)) -> Dict[str, Any]:
    """One factor's definition, coverage, full evidence and model usage."""
    cols = _cols()
    doc = cols["factor_registry"].find_one({"_id": factor_id})
    if not doc:
        raise HTTPException(status_code=404, detail=f"factor not found: {factor_id}")
    tests = list(cols["factor_tests"].find({"factor_id": factor_id}).sort("as_of", -1).limit(200))
    # Deep-link evidence rows back to the factor-backtest report that produced
    # them. Rows written from vault/manual notes carry no result_id (and some
    # results may have been deleted), so ``job_id`` stays absent there.
    result_ids = sorted({test["result_id"] for test in tests if test.get("result_id")})
    job_by_result: Dict[str, str] = {}
    if result_ids:
        for row in cols["factor_backtest_results"].find(
            {"result_id": {"$in": result_ids}}, {"result_id": 1, "job_id": 1},
        ):
            if row.get("job_id"):
                job_by_result[row["result_id"]] = str(row["job_id"])
    for test in tests:
        job_id = job_by_result.get(str(test.get("result_id") or ""))
        if job_id:
            test["job_id"] = job_id
    models = list(get_ml_db_collections()["models"].find(
        {"feature_factors.factor_id": factor_id},
        {"model_id": 1, "display_name": 1, "state": 1, "model_type": 1},
    ))
    return _jsonable({"registry": doc, "tests": tests, "used_by_models": models})


@router.get("/model-candidates")
def list_model_candidates(
    state: Optional[str] = None,
    factor: Optional[str] = None,
    model_type: Optional[str] = None,
    limit: int = 100,
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Model candidates with their per-factor status, newest state first."""
    query: Dict[str, Any] = {}
    states = _split(state)
    if states:
        query["state"] = {"$in": states}
    if model_type:
        query["model_type"] = model_type
    if factor:
        query["feature_factors.factor_id"] = factor
    docs = list(get_ml_db_collections()["models"].find(query, {
        "model_id": 1, "display_name": 1, "family_id": 1, "state": 1,
        "accepted": 1, "model_type": 1, "feature_factors": 1,
        "feature_status": 1, "feature_source": 1, "latest_validation": 1,
        "updated_at": 1,
    }))
    docs.sort(key=lambda doc: (STATE_RANK.get(doc.get("state"), 99),
                               str(doc.get("model_id") or doc["_id"])))
    rows = []
    for doc in docs[:limit]:
        latest = doc.get("latest_validation") or {}
        rows.append({
            "model_id": doc.get("model_id") or doc["_id"],
            "display_name": doc.get("display_name"),
            "family_id": doc.get("family_id"),
            "state": doc.get("state"),
            "accepted": bool(doc.get("accepted")),
            "model_type": doc.get("model_type"),
            "feature_source": doc.get("feature_source"),
            "features": [
                {"factor_id": item.get("factor_id"), "name": item.get("name"),
                 "kind": item.get("kind")}
                for item in (doc.get("feature_factors") or [])
            ],
            "factor_status": doc.get("feature_status") or {},
            "validation": ({"protocol": latest.get("protocol"),
                            "verdict": latest.get("verdict")} if latest else None),
        })
    return _jsonable({"total": len(docs), "rows": rows})
