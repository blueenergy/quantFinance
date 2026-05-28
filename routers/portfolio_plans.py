"""Portfolio trade plan and paper-trading API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from auth import get_current_user
from db import get_trading_db_collections


router = APIRouter(prefix="/portfolio-plans", tags=["portfolio-plans"])


class ReviewRequest(BaseModel):
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


def _require_plan(collections: Dict[str, Any], plan_id: str) -> Dict[str, Any]:
    plan = collections["portfolio_trade_plans"].find_one({"plan_id": plan_id})
    if not plan:
        raise HTTPException(status_code=404, detail="plan not found")
    return plan


def _reviewer(current_user: Dict[str, Any]) -> str:
    return str(current_user.get("username") or current_user.get("email") or current_user.get("id") or "unknown")


@router.get("/strategies")
async def list_portfolio_strategies(
    status: Optional[str] = Query(default=None),
    current_user: dict = Depends(get_current_user),
):
    collections = get_trading_db_collections()
    query: Dict[str, Any] = {}
    if status:
        query["status"] = status
    rows = list(collections["portfolio_strategy_configs"].find(query).sort("strategy_id", 1))
    return {"success": True, "data": [_jsonable(row) for row in rows]}


@router.get("/plans")
async def list_portfolio_plans(
    strategy_id: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    plan_type: Optional[str] = Query(default=None),
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    current_user: dict = Depends(get_current_user),
):
    collections = get_trading_db_collections()
    query: Dict[str, Any] = {}
    if strategy_id:
        query["strategy_id"] = strategy_id
    if status:
        query["status"] = status
    if plan_type:
        query["plan_type"] = plan_type
    if start_date or end_date:
        date_query: Dict[str, Any] = {}
        if start_date:
            date_query["$gte"] = start_date.replace("-", "")
        if end_date:
            date_query["$lte"] = end_date.replace("-", "")
        query["base_date"] = date_query
    rows = list(collections["portfolio_trade_plans"].find(query).sort("base_date", -1).limit(limit))
    return {"success": True, "data": [_jsonable(row) for row in rows]}


@router.get("/plans/{plan_id}")
async def get_portfolio_plan(plan_id: str, current_user: dict = Depends(get_current_user)):
    collections = get_trading_db_collections()
    plan = _require_plan(collections, plan_id)
    items = list(collections["portfolio_trade_plan_items"].find({"plan_id": plan_id}).sort("rank", 1))
    reviews = list(collections["portfolio_plan_reviews"].find({"plan_id": plan_id}).sort("created_at", -1))
    return {"success": True, "data": {"plan": _jsonable(plan), "items": _jsonable(items), "reviews": _jsonable(reviews)}}


@router.get("/plans/{plan_id}/items")
async def get_portfolio_plan_items(plan_id: str, current_user: dict = Depends(get_current_user)):
    collections = get_trading_db_collections()
    _require_plan(collections, plan_id)
    items = list(collections["portfolio_trade_plan_items"].find({"plan_id": plan_id}).sort("rank", 1))
    return {"success": True, "data": _jsonable(items)}


@router.post("/plans/{plan_id}/approve")
async def approve_portfolio_plan(plan_id: str, request: ReviewRequest, current_user: dict = Depends(get_current_user)):
    return _review_plan(plan_id, "approved", request.comment, current_user)


@router.post("/plans/{plan_id}/reject")
async def reject_portfolio_plan(plan_id: str, request: ReviewRequest, current_user: dict = Depends(get_current_user)):
    return _review_plan(plan_id, "rejected", request.comment, current_user)


def _review_plan(plan_id: str, decision: str, comment: str, current_user: Dict[str, Any]):
    collections = get_trading_db_collections()
    plan = _require_plan(collections, plan_id)
    if plan.get("status") not in {"needs_review", "approved", "rejected"}:
        raise HTTPException(status_code=400, detail=f"plan is not reviewable in status {plan.get('status')}")
    now = datetime.utcnow()
    review = {
        "plan_id": plan_id,
        "strategy_id": plan.get("strategy_id"),
        "decision": decision,
        "reviewer": _reviewer(current_user),
        "comment": comment,
        "created_at": now,
    }
    collections["portfolio_plan_reviews"].insert_one(review)
    collections["portfolio_trade_plans"].update_one(
        {"plan_id": plan_id},
        {"$set": {"status": decision, "reviewer": review["reviewer"], "reviewed_at": now}},
    )
    return {"success": True, "data": _jsonable(review)}


@router.get("/plans/{plan_id}/executions")
async def get_portfolio_plan_executions(plan_id: str, current_user: dict = Depends(get_current_user)):
    collections = get_trading_db_collections()
    _require_plan(collections, plan_id)
    rows = list(collections["portfolio_paper_executions"].find({"plan_id": plan_id}).sort("created_at", 1))
    return {"success": True, "data": _jsonable(rows)}


@router.get("/strategies/{strategy_id}/equity")
async def get_portfolio_strategy_equity(
    strategy_id: str,
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
    current_user: dict = Depends(get_current_user),
):
    collections = get_trading_db_collections()
    query: Dict[str, Any] = {"strategy_id": strategy_id}
    if start_date or end_date:
        date_query: Dict[str, Any] = {}
        if start_date:
            date_query["$gte"] = start_date.replace("-", "")
        if end_date:
            date_query["$lte"] = end_date.replace("-", "")
        query["date"] = date_query
    rows = list(collections["portfolio_paper_equity"].find(query).sort("date", 1))
    return {"success": True, "data": _jsonable(rows)}
