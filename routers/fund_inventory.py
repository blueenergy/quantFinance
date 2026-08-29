"""全市场主动公募库存四态看板。"""
from __future__ import annotations

import logging
from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from auth import get_current_active_user
from db import get_data_db, get_db_collections
from services.fund_inventory import public_snapshot
from services.fund_inventory_context import build_candidate_contexts

router = APIRouter(prefix="/fund-inventory", tags=["公募库存"])
logger = logging.getLogger(__name__)

_UNIVERSE = "active_equity"
InventoryState = Literal[
    "incumbent_strengthen",
    "new_inventory",
    "inventory_turnover",
    "inventory_exit",
]
InventorySort = Literal["net_ex_price", "net", "net_asc"]
InventoryMarket = Literal["A", "HK"]


def _coll():
    data = get_data_db()
    if isinstance(data, dict):
        return data.get("fund_inventory_snapshots")
    return data["fund_inventory_snapshots"]


@router.get("/periods")
def list_fund_inventory_periods(
    current_user: Dict[str, Any] = Depends(get_current_active_user),
):
    coll = _coll()
    if coll is None:
        return {"success": True, "data": {"periods": []}}
    try:
        values = coll.distinct("period_curr", {"universe": _UNIVERSE})
    except Exception as exc:
        logger.exception("list fund inventory periods failed")
        raise HTTPException(status_code=503, detail="公募库存数据暂时不可用") from exc
    periods = sorted({str(v) for v in values if v}, reverse=True)
    return {"success": True, "data": {"periods": periods}}


@router.get("/snapshots")
def list_fund_inventory_snapshots(
    period: Optional[str] = Query(
        default=None,
        pattern=r"^\d{8}$",
        description="本期 end_date YYYYMMDD",
    ),
    state: Optional[InventoryState] = Query(default=None),
    market: Optional[InventoryMarket] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    sort: InventorySort = Query(default="net_ex_price"),
    current_user: Dict[str, Any] = Depends(get_current_active_user),
):
    coll = _coll()
    if coll is None:
        return {"success": True, "data": {"period": period, "rows": [], "counts": {}}}

    query: Dict[str, Any] = {
        "universe": _UNIVERSE,
        "price_adjusted": True,
    }
    market_filter = market if isinstance(market, str) and market in {"A", "HK"} else None
    period_curr = (period or "").strip()
    if not period_curr:
        latest = coll.find_one(
            query,
            {"_id": 0, "period_curr": 1},
            sort=[("period_curr", -1)],
        )
        period_curr = (latest or {}).get("period_curr") or ""
    if period_curr:
        query["period_curr"] = period_curr
    if state:
        query["state"] = state
    if market_filter:
        query["market"] = market_filter

    sort_field = "net_ex_price"
    descending = True
    if sort == "net_asc":
        sort_field = "net_ex_price"
        descending = False

    try:
        rows = list(
            coll.find(
                query,
                {"_id": 0},
                sort=[(sort_field, -1 if descending else 1)],
                limit=limit,
            )
        )
    except Exception as exc:
        logger.exception("list fund inventory snapshots failed")
        raise HTTPException(status_code=503, detail="公募库存数据暂时不可用") from exc
    counts: Dict[str, int] = {}
    count_query = {"universe": _UNIVERSE, "price_adjusted": True}
    if period_curr:
        count_query["period_curr"] = period_curr
    if market_filter:
        count_query["market"] = market_filter
    try:
        quality_doc = coll.find_one(
            count_query,
            {
                "_id": 0,
                "quality_status": 1,
                "comparison_scope": 1,
                "quality": 1,
            },
        )
        for st in (
            "incumbent_strengthen",
            "new_inventory",
            "inventory_turnover",
            "inventory_exit",
        ):
            counts[st] = int(coll.count_documents({**count_query, "state": st}))
    except Exception as exc:
        logger.exception("count fund inventory states failed")
        raise HTTPException(status_code=503, detail="公募库存数据暂时不可用") from exc

    candidate_contexts: Dict[str, Dict[str, Any]] = {}
    try:
        candidate_contexts = build_candidate_contexts(
            rows,
            finance_collections=get_db_collections(),
            data_collections=get_data_db(),
        )
    except Exception:
        # Candidate classification is an enrichment. Inventory accounting stays
        # available and explicitly reports insufficient context if it fails.
        logger.exception("load fund inventory candidate context failed")

    return {
        "success": True,
        "data": {
            "period": period_curr,
            "state": state,
            "market": market_filter,
            "rows": [
                public_snapshot(
                    row,
                    candidate_contexts.get(str(row.get("ts_code") or "")),
                )
                for row in rows
            ],
            "counts": counts,
            "quality_status": (quality_doc or {}).get("quality_status"),
            "comparison_scope": (quality_doc or {}).get("comparison_scope"),
            "quality": (quality_doc or {}).get("quality") or {},
        },
    }
