"""
Market events calendar API — read scheduled macro/corporate events from quant_data.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from zoneinfo import ZoneInfo

from auth import get_current_user
from db import db, get_data_db
from services.analysis_tasks import build_task_doc

router = APIRouter(prefix="/market", tags=["market-events"])

_SH_TZ = ZoneInfo("Asia/Shanghai")


class EnqueueBriefsRequest(BaseModel):
    min_importance: int = Field(4, ge=1, le=4)
    limit: int = Field(20, ge=1, le=100)


def _serialize_event(doc: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(doc)
    out["id"] = str(doc.get("_id", ""))
    out.pop("_id", None)
    et = doc.get("event_time")
    if hasattr(et, "isoformat"):
        out["event_time"] = et.isoformat()
    for src in out.get("sources") or []:
        seen = src.get("seen_at")
        if hasattr(seen, "isoformat"):
            src["seen_at"] = seen.isoformat()
    for field in ("fetched_at", "updated_at"):
        val = out.get(field)
        if hasattr(val, "isoformat"):
            out[field] = val.isoformat()
    return out


def _default_from_to() -> tuple[str, str]:
    now = datetime.now(_SH_TZ)
    start = now.strftime("%Y%m%d")
    end = (now + timedelta(days=14)).strftime("%Y%m%d")
    return start, end


def _parse_yyyymmdd(value: str, end_of_day: bool = False) -> datetime:
    s = "".join(c for c in str(value) if c.isdigit())[:8]
    dt = datetime.strptime(s, "%Y%m%d").replace(tzinfo=_SH_TZ)
    if end_of_day:
        dt = dt.replace(hour=23, minute=59, second=59)
    return dt.astimezone(timezone.utc)


@router.get("/events")
async def list_market_events(
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    region: Optional[str] = Query(None),
    min_importance: int = Query(3, ge=1, le=4),
    category: Optional[str] = Query(None),
    symbol: Optional[str] = Query(None),
    limit: int = Query(500, ge=1, le=2000),
) -> Dict[str, Any]:
    return query_market_events(
        from_date=from_date,
        to_date=to_date,
        region=region,
        min_importance=min_importance,
        category=category,
        symbol=symbol,
        limit=limit,
    )


def query_market_events(
    *,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    region: Optional[str] = None,
    min_importance: int = 3,
    category: Optional[str] = None,
    symbol: Optional[str] = None,
    limit: int = 500,
) -> Dict[str, Any]:
    default_from, default_to = _default_from_to()
    from_s = from_date or default_from
    to_s = to_date or default_to

    query: Dict[str, Any] = {
        "event_time": {
            "$gte": _parse_yyyymmdd(from_s),
            "$lte": _parse_yyyymmdd(to_s, end_of_day=True),
        },
        "importance": {"$gte": min_importance},
    }
    if region:
        query["region"] = region.upper()
    if category:
        query["category"] = category
    if symbol:
        query["symbol"] = symbol.upper()

    col = get_data_db()["market_events"]
    cursor = col.find(query).sort("event_time", 1).limit(limit)
    events = [_serialize_event(doc) for doc in cursor]
    return {
        "from": from_s,
        "to": to_s,
        "count": len(events),
        "events": events,
    }


@router.get("/events/status")
async def market_events_status() -> Dict[str, Any]:
    latest = db["job_status"].find_one(
        {"job_type": "market_events"},
        sort=[("updated_at", -1)],
    )
    col = get_data_db()["market_events"]
    upcoming = col.count_documents(
        {
            "event_time": {"$gte": datetime.now(timezone.utc)},
            "importance": {"$gte": 3},
        }
    )
    newest = col.find_one({}, sort=[("updated_at", -1)])
    return {
        "job_status": {
            "status": (latest or {}).get("status"),
            "updated_at": (latest or {}).get("updated_at"),
            "records_changed": (latest or {}).get("records_changed"),
            "error": (latest or {}).get("error"),
        },
        "upcoming_high_importance": upcoming,
        "latest_event_update": (newest or {}).get("updated_at"),
    }


@router.post("/events/enqueue-briefs")
async def enqueue_market_event_briefs(
    body: EnqueueBriefsRequest,
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Enqueue a batch LLM brief job for high-importance upcoming events."""
    analysis_tasks = db["analysis_tasks"]
    task_doc = build_task_doc(
        current_user=current_user,
        task_type="market_events_brief",
        priority=10,
        params={
            "min_importance": body.min_importance,
            "limit": body.limit,
        },
        source="market_events_calendar",
    )
    task_doc["idempotency_key"] = f"market_events_brief:{datetime.now(timezone.utc).strftime('%Y%m%d')}"
    existing = analysis_tasks.find_one(
        {"idempotency_key": task_doc["idempotency_key"], "status": {"$in": ["pending", "processing"]}}
    )
    if existing:
        return {"task_id": str(existing["_id"]), "status": existing.get("status"), "reused": True}
    result = analysis_tasks.insert_one(task_doc)
    return {"task_id": str(result.inserted_id), "status": "pending", "reused": False}
