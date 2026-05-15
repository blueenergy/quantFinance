from __future__ import annotations

from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel

from auth import get_current_active_user, require_hermes_quant_internal_key
from db import get_db_collections
from services.market_insight_engine import (
    EVENT_TYPES,
    generate_market_insight_events,
    get_event_categories,
    get_event_type_metadata,
    get_market_insight_focus,
    get_market_insight_overview,
    get_sector_strength,
    get_sector_strength_series,
    list_market_insight_events,
)
from utils.stock_utils import serialize_doc


router = APIRouter(prefix="/market-insights", tags=["火眼金睛"])


class GenerateInsightPayload(BaseModel):
    trade_date: Optional[str] = None
    snapshot_time: Optional[str] = None
    source: str = "auto"
    sync_status: Optional[str] = None


@router.get("/overview")
def overview(
    trade_date: Optional[str] = Query(None, description="YYYYMMDD or YYYY-MM-DD"),
    current_user: dict = Depends(get_current_active_user),  # noqa: ARG001
):
    return get_market_insight_overview(trade_date)


@router.get("/event-types")
def event_types(
    current_user: dict = Depends(get_current_active_user),  # noqa: ARG001
):
    metadata = get_event_type_metadata()
    return {"success": True, "count": len(metadata), "data": metadata}


@router.get("/focus")
def focus(
    trade_date: Optional[str] = Query(None, description="YYYYMMDD or YYYY-MM-DD"),
    limit: int = Query(5, ge=1, le=10),
    current_user: dict = Depends(get_current_active_user),
):
    return get_market_insight_focus(trade_date=trade_date, limit=limit, user_id=str(current_user["id"]))


@router.get("/events")
def events(
    trade_date: Optional[str] = Query(None, description="YYYYMMDD or YYYY-MM-DD"),
    event_type: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    severity: Optional[str] = Query(None, pattern="^(low|medium|high)$"),
    min_confidence: Optional[float] = Query(None, ge=0, le=100),
    level: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    current_user: dict = Depends(get_current_active_user),  # noqa: ARG001
):
    if event_type and event_type not in EVENT_TYPES:
        raise HTTPException(status_code=400, detail="unsupported event_type")
    if category and category not in get_event_categories():
        raise HTTPException(status_code=400, detail="unsupported category")
    rows = list_market_insight_events(
        trade_date=trade_date,
        event_type=event_type,
        category=category,
        severity=severity,
        min_confidence=min_confidence,
        level=level,
        limit=limit,
    )
    return {"success": True, "count": len(rows), "data": rows}


@router.get("/events/{event_id}")
def event_detail(
    event_id: str,
    current_user: dict = Depends(get_current_active_user),  # noqa: ARG001
):
    try:
        oid = ObjectId(event_id)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid event_id")
    doc = get_db_collections()["market_insight_events"].find_one({"_id": oid})
    if not doc:
        raise HTTPException(status_code=404, detail="event not found")
    return {"success": True, "data": serialize_doc(doc)}


@router.get("/sector-strength")
def sector_strength(
    trade_date: Optional[str] = Query(None, description="YYYYMMDD or YYYY-MM-DD"),
    level: str = Query("L2", pattern="^L[123]$"),
    window: int = Query(15, ge=5, le=60),
    limit: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(get_current_active_user),  # noqa: ARG001
):
    return get_sector_strength(level=level, trade_date=trade_date, window=window, limit=limit)


@router.get("/sector-strength/series")
def sector_strength_series(
    sector_code: str = Query(..., min_length=1),
    trade_date: Optional[str] = Query(None, description="YYYYMMDD or YYYY-MM-DD"),
    level: str = Query("L2", pattern="^L[123]$"),
    limit: int = Query(120, ge=1, le=500),
    simulate_if_sparse: bool = Query(False),
    current_user: dict = Depends(get_current_active_user),  # noqa: ARG001
):
    return get_sector_strength_series(
        level=level,
        sector_code=sector_code,
        trade_date=trade_date,
        limit=limit,
        simulate_if_sparse=simulate_if_sparse,
    )


@router.post("/generate")
def generate(
    trade_date: Optional[str] = Query(None, description="YYYYMMDD or YYYY-MM-DD"),
    current_user: dict = Depends(get_current_active_user),  # noqa: ARG001
):
    return generate_market_insight_events(trade_date)


@router.post("/generate-internal")
def generate_internal(
    payload: Optional[GenerateInsightPayload] = Body(None),
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """Internal endpoint called after realtime daily K sync materializes snapshots."""
    payload = payload or GenerateInsightPayload()
    return generate_market_insight_events(payload.trade_date)
