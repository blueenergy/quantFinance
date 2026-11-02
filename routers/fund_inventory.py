"""全市场主动公募库存四态看板。"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from auth import get_current_active_user
from db import get_data_db, get_db_collections
from services.analysis_tasks import build_task_doc
from services.fund_inventory import public_snapshot
from services.fund_inventory_comparison import build_period_comparison
from services.fund_inventory_context import build_candidate_contexts
from services.fund_inventory_review import (
    build_evidence_fingerprint,
    build_period_review_evidence_pack,
)

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


def _as_str(value: Any) -> str:
    """Normalize a FastAPI Query default when the function is called directly."""
    return value.strip() if isinstance(value, str) else ""


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


def _pair_query(
    *,
    period_curr: Optional[str] = None,
    period_prev: Optional[str] = None,
    market: Optional[str] = None,
) -> Dict[str, Any]:
    query: Dict[str, Any] = {"universe": _UNIVERSE, "price_adjusted": True}
    if period_curr:
        query["period_curr"] = period_curr
    if period_prev:
        query["period_prev"] = period_prev
    if market in {"A", "HK"}:
        query["market"] = market
    return query


@router.get("/pairs")
def list_fund_inventory_pairs(
    market: Optional[InventoryMarket] = Query(default=None),
    current_user: Dict[str, Any] = Depends(get_current_active_user),
):
    """List available (current, previous) snapshot pairs with their scope labels."""
    coll = _coll()
    if coll is None:
        return {"success": True, "data": {"pairs": [], "periods": []}}
    market_filter = market if isinstance(market, str) and market in {"A", "HK"} else None
    base = _pair_query(market=market_filter)
    try:
        periods = sorted(
            {str(v) for v in coll.distinct("period_curr", base) if v},
            reverse=True,
        )
        pairs = []
        for period_curr in periods:
            prevs = sorted(
                {
                    str(v)
                    for v in coll.distinct(
                        "period_prev",
                        {**base, "period_curr": period_curr},
                    )
                    if v
                },
                reverse=True,
            )
            for period_prev in prevs:
                query = {**base, "period_curr": period_curr, "period_prev": period_prev}
                meta = coll.find_one(
                    query,
                    {
                        "_id": 0,
                        "compare_mode": 1,
                        "comparison_scope": 1,
                        "quality_status": 1,
                        "updated_at": 1,
                    },
                    sort=[("updated_at", -1)],
                ) or {}
                pairs.append(
                    {
                        "period_curr": period_curr,
                        "period_prev": period_prev,
                        "compare_mode": meta.get("compare_mode"),
                        "comparison_scope": meta.get("comparison_scope"),
                        "quality_status": meta.get("quality_status"),
                        "stock_count": coll.count_documents(query),
                        "updated_at": meta.get("updated_at"),
                    }
                )
    except Exception as exc:
        logger.exception("list fund inventory pairs failed")
        raise HTTPException(status_code=503, detail="公募库存数据暂时不可用") from exc
    return {"success": True, "data": {"pairs": pairs, "periods": periods}}


@router.get("/comparison")
def get_fund_inventory_comparison(
    period_curr: Optional[str] = Query(
        default=None,
        pattern=r"^\d{8}$",
        description="本期 end_date YYYYMMDD；不传则取最新一期",
    ),
    period_prev: Optional[str] = Query(
        default=None,
        pattern=r"^\d{8}$",
        description="上期 end_date YYYYMMDD；不传则取该期最近一个上期",
    ),
    market: Optional[InventoryMarket] = Query(default=None),
    top_n: int = Query(default=10, ge=1, le=50),
    current_user: Dict[str, Any] = Depends(get_current_active_user),
):
    """Aggregate one snapshot pair into a quarter-over-quarter view."""
    coll = _coll()
    if coll is None:
        return {"success": True, "data": {"rows": [], "message": "暂无公募库存快照"}}

    base = _pair_query(market=market)
    try:
        curr = _as_str(period_curr)
        if not curr:
            values = [str(v) for v in coll.distinct("period_curr", base) if v]
            curr = max(values) if values else ""
        prev = _as_str(period_prev)
        if curr and not prev:
            values = [
                str(v)
                for v in coll.distinct(
                    "period_prev",
                    {**base, "period_curr": curr},
                )
                if v
            ]
            prev = max(values) if values else ""
        if not curr or not prev:
            return {
                "success": True,
                "data": {
                    "period_curr": curr or None,
                    "period_prev": prev or None,
                    "totals": {},
                    "state_counts": {},
                    "industries": [],
                    "top": {},
                    "caveats": ["该报告期还没有可用快照。"],
                },
            }

        query = {**base, "period_curr": curr, "period_prev": prev}
        meta = coll.find_one(
            query,
            {
                "_id": 0,
                "compare_mode": 1,
                "comparison_scope": 1,
                "quality_status": 1,
                "quality": 1,
            },
            sort=[("updated_at", -1)],
        ) or {}
        rows = list(
            coll.find(
                query,
                {
                    "_id": 0,
                    "symbol": 1,
                    "ts_code": 1,
                    "name": 1,
                    "industry": 1,
                    "market": 1,
                    "period_prev": 1,
                    "period_curr": 1,
                    "compare_mode": 1,
                    "comparison_scope": 1,
                    "quality_status": 1,
                    "price_adjusted": 1,
                    "price_ret": 1,
                    "state": 1,
                    "state_label": 1,
                    "mv_prev": 1,
                    "mv_curr": 1,
                    "surface_mv_chg": 1,
                    "mv_price_effect": 1,
                    "net_ex_price": 1,
                    "new_inflow": 1,
                    "incumbent_delta": 1,
                    "exited_prev_mv": 1,
                    "n_funds_prev": 1,
                    "n_funds_curr": 1,
                    "n_new": 1,
                    "n_exited": 1,
                    "n_incumbent": 1,
                },
            )
        )
    except Exception as exc:
        logger.exception("build fund inventory comparison failed")
        raise HTTPException(status_code=503, detail="公募库存数据暂时不可用") from exc

    scope = meta.get("comparison_scope") or (
        "top10" if meta.get("compare_mode") == "top10_vs_top10" else "full"
    )
    top_n_value = top_n if isinstance(top_n, int) else 10
    data = build_period_comparison(
        rows,
        period_curr=curr,
        period_prev=prev,
        quality=meta.get("quality"),
        quality_status=meta.get("quality_status") or "official",
        comparison_scope=scope,
        compare_mode=meta.get("compare_mode"),
        top_n=top_n_value,
        market=market if isinstance(market, str) else None,
    )
    if not rows:
        data["message"] = "该报告期还没有可用快照。"
    return {"success": True, "data": data}


@router.get("/snapshots")
def list_fund_inventory_snapshots(
    period: Optional[str] = Query(
        default=None,
        pattern=r"^\d{8}$",
        description="本期 end_date YYYYMMDD",
    ),
    period_prev: Optional[str] = Query(
        default=None,
        pattern=r"^\d{8}$",
        description="上期 end_date YYYYMMDD；不传则取该期最近一个上期",
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
    period_curr = _as_str(period)
    if not period_curr:
        latest = coll.find_one(
            query,
            {"_id": 0, "period_curr": 1},
            sort=[("period_curr", -1)],
        )
        period_curr = (latest or {}).get("period_curr") or ""
    if period_curr:
        query["period_curr"] = period_curr
    period_prev_value = _as_str(period_prev)
    if period_curr and not period_prev_value:
        try:
            prevs = [
                str(v)
                for v in coll.distinct(
                    "period_prev",
                    {
                        "universe": _UNIVERSE,
                        "price_adjusted": True,
                        "period_curr": period_curr,
                    },
                )
                if v
            ]
        except Exception as exc:
            logger.exception("resolve fund inventory previous period failed")
            raise HTTPException(status_code=503, detail="公募库存数据暂时不可用") from exc
        period_prev_value = max(prevs) if prevs else ""
    if period_prev_value:
        query["period_prev"] = period_prev_value
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
    if period_prev_value:
        count_query["period_prev"] = period_prev_value
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
            "period_prev": period_prev_value,
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


class FundInventoryReviewRequest(BaseModel):
    period_curr: Optional[str] = Field(
        default=None,
        description="本期 end_date YYYYMMDD；不传则取最新一期",
    )
    period_prev: Optional[str] = Field(
        default=None,
        description="上期 end_date YYYYMMDD；不传则取该期最近一个上期",
    )
    top_n: int = Field(default=10, ge=1, le=50)
    force: bool = False


@router.post("/analysis/trigger")
def trigger_fund_inventory_review(
    request: FundInventoryReviewRequest,
    current_user: Dict[str, Any] = Depends(get_current_active_user),
):
    """Enqueue a grounded period-review task for one snapshot pair."""
    for value in (request.period_curr, request.period_prev):
        if value and (len(value) != 8 or not value.isdigit()):
            raise HTTPException(status_code=400, detail="报告期格式应为 YYYYMMDD")

    comparison_body = get_fund_inventory_comparison(
        period_curr=request.period_curr,
        period_prev=request.period_prev,
        market=None,
        top_n=request.top_n,
        current_user=current_user,
    )
    comparison = (comparison_body or {}).get("data") or {}
    if not comparison.get("period_curr") or not comparison.get("period_prev"):
        raise HTTPException(status_code=400, detail="该报告期还没有可用快照")

    pack = build_period_review_evidence_pack(
        comparison,
        top_n=min(int(request.top_n), 6),
    )
    fingerprint = build_evidence_fingerprint(pack)

    collections = get_db_collections()
    tasks = collections.get("analysis_tasks") if isinstance(collections, dict) else None
    if tasks is None:
        raise HTTPException(status_code=503, detail="analysis task queue unavailable")

    user_id = str(current_user.get("id") or current_user.get("username") or "")
    if not request.force:
        existing = tasks.find_one(
            {
                "type": "fund_inventory_period_review",
                "user_id": user_id,
                "params.evidence_fingerprint": fingerprint,
                "status": {"$in": ["pending", "processing"]},
            },
            sort=[("created_at", -1)],
        )
        if existing:
            return {
                "success": True,
                "data": {
                    "task_id": str(existing.get("_id") or ""),
                    "status": existing.get("status") or "pending",
                    "reused": True,
                },
            }

    run_id = str(uuid.uuid4())
    task = build_task_doc(
        current_user=current_user,
        task_type="fund_inventory_period_review",
        priority=8,
        params={
            "period_curr": comparison["period_curr"],
            "period_prev": comparison["period_prev"],
            "top_n": request.top_n,
            "run_id": run_id,
            "evidence_fingerprint": fingerprint,
            "evidence_pack": pack,
            "options": {"force": request.force},
        },
        source="fund_inventory_board",
        extra_fields={
            "operation_type": "fund_inventory_period_review",
            "idempotency_key": f"fund-inventory-review:{fingerprint}",
        },
    )
    result = tasks.insert_one(task)
    return {
        "success": True,
        "data": {
            "task_id": str(getattr(result, "inserted_id", "") or ""),
            "run_id": run_id,
            "status": "pending",
            "reused": False,
        },
    }
