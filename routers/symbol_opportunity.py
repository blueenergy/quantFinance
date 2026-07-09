"""Symbol-level opportunity finding management API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import get_current_user
from db import get_analyzer_db, get_trading_db_collections
from services.portfolio_symbol_risk import get_symbol_opportunity_ledger, get_symbol_risk_ledger

router = APIRouter()

SYMBOL_OPPORTUNITY_LOGS_COL = "symbol_opportunity_operation_logs"


class ManualOpportunityFindingRequest(BaseModel):
    strength: str = Field(default="medium", pattern="^(none|low|medium|high)$")
    summary: str = Field(min_length=1, max_length=500)
    detail: str = Field(default="", max_length=4000)
    category: Optional[str] = Field(default=None, max_length=120)
    subject: Optional[str] = Field(default=None, max_length=200)
    resolution_mode: str = Field(default="event", pattern="^(event|metric|dated)$")


class CloseOpportunityFindingRequest(BaseModel):
    reason: str = Field(default="", max_length=2000)
    plan_id: Optional[str] = None
    spawn_risk: bool = False


def _current_user_id(current_user: Dict[str, Any]) -> str:
    user_id = current_user.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="unauthorized")
    return str(user_id)


def _append_symbol_opportunity_log(
    *,
    symbol: str,
    operation_type: str,
    user_id: str,
    finding_key: str,
    payload: Optional[Dict[str, Any]] = None,
    plan_id: Optional[str] = None,
) -> None:
    collections = get_trading_db_collections()
    collections[SYMBOL_OPPORTUNITY_LOGS_COL].insert_one(
        {
            "symbol": symbol,
            "finding_key": finding_key,
            "operation_type": operation_type,
            "user_id": user_id,
            "plan_id": plan_id,
            "payload": payload or {},
            "created_at": datetime.utcnow(),
        }
    )


@router.post("/symbols/{symbol}/opportunity-findings")
def add_manual_symbol_opportunity(
    symbol: str,
    request: ManualOpportunityFindingRequest,
    current_user: dict = Depends(get_current_user),
):
    user_id = _current_user_id(current_user)
    ledger = get_symbol_opportunity_ledger(get_analyzer_db())
    finding_key = ledger.add_manual_finding(
        symbol,
        strength=request.strength,
        summary=request.summary,
        detail=request.detail,
        created_by=user_id,
        category=request.category,
        subject=request.subject,
        resolution_mode=request.resolution_mode,
    )
    _append_symbol_opportunity_log(
        symbol=symbol,
        operation_type="manual_add",
        user_id=user_id,
        finding_key=finding_key,
        payload={"summary": request.summary, "strength": request.strength},
    )
    return {"success": True, "data": {"symbol": symbol, "finding_key": finding_key}}


@router.post("/symbols/{symbol}/opportunity-findings/{finding_key}/realize")
def realize_symbol_opportunity(
    symbol: str,
    finding_key: str,
    request: CloseOpportunityFindingRequest,
    current_user: dict = Depends(get_current_user),
):
    user_id = _current_user_id(current_user)
    ledger = get_symbol_opportunity_ledger(get_analyzer_db())
    ok = ledger.realize_finding(
        symbol,
        finding_key,
        reason=request.reason or "manual_realize",
        closed_by=user_id,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="active opportunity finding not found")
    _append_symbol_opportunity_log(
        symbol=symbol,
        operation_type="realize",
        user_id=user_id,
        finding_key=finding_key,
        payload={"reason": request.reason},
        plan_id=request.plan_id,
    )
    return {"success": True, "data": {"symbol": symbol, "finding_key": finding_key, "outcome": "realized"}}


@router.post("/symbols/{symbol}/opportunity-findings/{finding_key}/invalidate")
def invalidate_symbol_opportunity(
    symbol: str,
    finding_key: str,
    request: CloseOpportunityFindingRequest,
    current_user: dict = Depends(get_current_user),
):
    user_id = _current_user_id(current_user)
    analyzer_db = get_analyzer_db()
    ledger = get_symbol_opportunity_ledger(analyzer_db)
    risk_ledger = get_symbol_risk_ledger(analyzer_db) if request.spawn_risk else None
    ok = ledger.invalidate_finding(
        symbol,
        finding_key,
        reason=request.reason or "manual_invalidate",
        closed_by=user_id,
        spawn_risk=request.spawn_risk,
        risk_ledger=risk_ledger,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="active opportunity finding not found")
    doc = ledger.symbol_coll.find_one({"symbol": symbol, "finding_key": finding_key}, {"_id": 0}) or {}
    _append_symbol_opportunity_log(
        symbol=symbol,
        operation_type="invalidate",
        user_id=user_id,
        finding_key=finding_key,
        payload={
            "reason": request.reason,
            "spawn_risk": request.spawn_risk,
            "spawned_risk_finding_key": doc.get("spawned_risk_finding_key"),
        },
        plan_id=request.plan_id,
    )
    return {
        "success": True,
        "data": {
            "symbol": symbol,
            "finding_key": finding_key,
            "outcome": "invalidated",
            "spawned_risk_finding_key": doc.get("spawned_risk_finding_key"),
        },
    }


@router.post("/symbols/{symbol}/opportunity-findings/{finding_key}/reopen")
def reopen_symbol_opportunity(
    symbol: str,
    finding_key: str,
    request: CloseOpportunityFindingRequest,
    current_user: dict = Depends(get_current_user),
):
    user_id = _current_user_id(current_user)
    ledger = get_symbol_opportunity_ledger(get_analyzer_db())
    ok = ledger.reopen_finding(
        symbol,
        finding_key,
        reopened_by=user_id,
        reason=request.reason or "",
    )
    if not ok:
        raise HTTPException(status_code=404, detail="closed opportunity finding not found")
    _append_symbol_opportunity_log(
        symbol=symbol,
        operation_type="reopen",
        user_id=user_id,
        finding_key=finding_key,
        payload={"reason": request.reason},
        plan_id=request.plan_id,
    )
    return {"success": True, "data": {"symbol": symbol, "finding_key": finding_key, "reopened": True}}
