"""Symbol-level risk finding management API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import get_current_user
from db import get_analyzer_db, get_trading_db_collections
from services.portfolio_symbol_risk import get_symbol_risk_ledger

router = APIRouter()

SYMBOL_RISK_LOGS_COL = "symbol_risk_operation_logs"


class ManualRiskFindingRequest(BaseModel):
    severity: str = Field(default="medium", pattern="^(none|low|medium|high)$")
    summary: str = Field(min_length=1, max_length=500)
    detail: str = Field(default="", max_length=4000)
    category: Optional[str] = Field(default=None, max_length=120)
    subject: Optional[str] = Field(default=None, max_length=200)
    resolution_mode: str = Field(default="event", pattern="^(event|metric|dated)$")


class ResolveRiskFindingRequest(BaseModel):
    reason: str = Field(default="", max_length=2000)
    plan_id: Optional[str] = None


def _current_user_id(current_user: Dict[str, Any]) -> str:
    user_id = current_user.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="unauthorized")
    return str(user_id)


def _append_symbol_risk_log(
    *,
    symbol: str,
    operation_type: str,
    user_id: str,
    finding_key: str,
    payload: Optional[Dict[str, Any]] = None,
    plan_id: Optional[str] = None,
) -> None:
    collections = get_trading_db_collections()
    collections[SYMBOL_RISK_LOGS_COL].insert_one(
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


@router.post("/symbols/{symbol}/risk-findings")
def add_manual_symbol_risk(
    symbol: str,
    request: ManualRiskFindingRequest,
    current_user: dict = Depends(get_current_user),
):
    user_id = _current_user_id(current_user)
    ledger = get_symbol_risk_ledger(get_analyzer_db())
    finding_key = ledger.add_manual_finding(
        symbol,
        severity=request.severity,
        summary=request.summary,
        detail=request.detail,
        created_by=user_id,
        category=request.category,
        subject=request.subject,
        resolution_mode=request.resolution_mode,
    )
    _append_symbol_risk_log(
        symbol=symbol,
        operation_type="manual_add",
        user_id=user_id,
        finding_key=finding_key,
        payload={"summary": request.summary, "severity": request.severity},
    )
    return {"success": True, "data": {"symbol": symbol, "finding_key": finding_key}}


@router.post("/symbols/{symbol}/risk-findings/{finding_key}/confirm-resolution")
def confirm_symbol_risk_resolution(
    symbol: str,
    finding_key: str,
    request: ResolveRiskFindingRequest,
    current_user: dict = Depends(get_current_user),
):
    user_id = _current_user_id(current_user)
    ledger = get_symbol_risk_ledger(get_analyzer_db())
    ok = ledger.confirm_resolution(symbol, finding_key, confirmed_by=user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="active finding with suggested resolution not found")
    _append_symbol_risk_log(
        symbol=symbol,
        operation_type="confirm_resolution",
        user_id=user_id,
        finding_key=finding_key,
        payload={"reason": request.reason},
        plan_id=request.plan_id,
    )
    return {"success": True, "data": {"symbol": symbol, "finding_key": finding_key, "resolved": True}}


@router.post("/symbols/{symbol}/risk-findings/{finding_key}/resolve")
def resolve_symbol_risk(
    symbol: str,
    finding_key: str,
    request: ResolveRiskFindingRequest,
    current_user: dict = Depends(get_current_user),
):
    user_id = _current_user_id(current_user)
    ledger = get_symbol_risk_ledger(get_analyzer_db())
    ok = ledger.resolve_finding(
        symbol,
        finding_key,
        reason=request.reason or "manual_resolve",
        resolved_by=user_id,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="active finding not found")
    _append_symbol_risk_log(
        symbol=symbol,
        operation_type="manual_resolve",
        user_id=user_id,
        finding_key=finding_key,
        payload={"reason": request.reason},
        plan_id=request.plan_id,
    )
    return {"success": True, "data": {"symbol": symbol, "finding_key": finding_key, "resolved": True}}


@router.post("/symbols/{symbol}/risk-findings/{finding_key}/reopen")
def reopen_symbol_risk(
    symbol: str,
    finding_key: str,
    request: ResolveRiskFindingRequest,
    current_user: dict = Depends(get_current_user),
):
    user_id = _current_user_id(current_user)
    ledger = get_symbol_risk_ledger(get_analyzer_db())
    ok = ledger.reopen_finding(
        symbol,
        finding_key,
        reopened_by=user_id,
        reason=request.reason or "",
    )
    if not ok:
        raise HTTPException(status_code=404, detail="resolved finding not found")
    _append_symbol_risk_log(
        symbol=symbol,
        operation_type="reopen",
        user_id=user_id,
        finding_key=finding_key,
        payload={"reason": request.reason},
        plan_id=request.plan_id,
    )
    return {"success": True, "data": {"symbol": symbol, "finding_key": finding_key, "reopened": True}}
