"""公募抄作业看板：观察名单速览、大家在买什么、按代码查。"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional
from urllib.parse import unquote

from fastapi import APIRouter, Depends, HTTPException, Query

from auth import get_current_active_user
from db import get_data_db
from services.fund_copywork import public_meta, public_product, public_stock

router = APIRouter(prefix="/fund-copywork", tags=["公募抄作业"])
logger = logging.getLogger(__name__)

_UNIVERSE = "active_equity"


def _coll(name: str):
    """None when the collection is not registered in db.py."""
    return get_data_db().get(name)


def _latest_meta():
    coll = _coll("fund_copywork_meta")
    if coll is None:
        return None
    return coll.find_one({"universe": _UNIVERSE}, {"_id": 0}, sort=[("period_curr", -1)])


def _meta_for(period: Optional[str]):
    coll = _coll("fund_copywork_meta")
    if coll is None:
        return None
    query: Dict[str, Any] = {"universe": _UNIVERSE}
    if period:
        query["period_curr"] = period
        return coll.find_one(query, {"_id": 0}, sort=[("period_prev", -1)])
    return _latest_meta()


def _holding_names(symbols, *, period_prev, period_curr) -> Dict[str, str]:
    coll = _coll("fund_copywork_stocks")
    keys = [str(symbol) for symbol in symbols if symbol]
    if coll is None or not keys:
        return {}
    names: Dict[str, str] = {}
    try:
        docs = coll.find(
            {
                "universe": _UNIVERSE,
                "period_curr": period_curr,
                "period_prev": period_prev,
                "symbol": {"$in": keys},
            },
            {"_id": 0, "symbol": 1, "name": 1},
        )
    except Exception:
        return {}
    for doc in docs or []:
        symbol = str(doc.get("symbol") or "")
        name = doc.get("name")
        if symbol and name:
            names[symbol] = name
    return names


@router.get("/periods")
def list_copywork_periods(
    current_user: Dict[str, Any] = Depends(get_current_active_user),
):
    coll = _coll("fund_copywork_meta")
    if coll is None:
        return {"success": True, "data": {"periods": [], "items": []}}
    try:
        docs = list(
            coll.find(
                {"universe": _UNIVERSE},
                {
                    "_id": 0,
                    "period_curr": 1,
                    "period_prev": 1,
                    "compare_mode": 1,
                    "quality_status": 1,
                    "comparison_scope": 1,
                    "disclosure_curr": 1,
                    "aum_basis": 1,
                },
            )
        )
    except Exception as exc:
        logger.exception("list fund copywork periods failed")
        raise HTTPException(status_code=503, detail="抄作业数据暂时不可用") from exc
    best: Dict[str, Dict[str, Any]] = {}
    for doc in docs:
        curr = str(doc.get("period_curr") or "")
        if not curr:
            continue
        prev = str(doc.get("period_prev") or "")
        old = best.get(curr)
        if old is None or prev > str(old.get("period_prev") or ""):
            best[curr] = doc
    periods = sorted(best, reverse=True)
    items = []
    for period in periods:
        doc = best[period]
        items.append(
            {
                **public_meta(doc),
                "period": period,
            }
        )
    return {"success": True, "data": {"periods": periods, "items": items}}


@router.get("/watchlist")
def list_copywork_watchlist(
    period: Optional[str] = Query(default=None, pattern=r"^\d{8}$"),
    current_user: Dict[str, Any] = Depends(get_current_active_user),
):
    meta = _meta_for(period)
    if not meta:
        return {"success": True, "data": {"period": period, "rows": [], "meta": {}}}
    products = _coll("fund_copywork_products")
    query = {
        "universe": _UNIVERSE,
        "period_curr": meta.get("period_curr"),
        "period_prev": meta.get("period_prev"),
    }
    try:
        rows = list(products.find(query, {"_id": 0, "holdings": 0}, sort=[("rank", 1)]))
    except Exception as exc:
        logger.exception("list fund copywork watchlist failed")
        raise HTTPException(status_code=503, detail="抄作业数据暂时不可用") from exc
    return {
        "success": True,
        "data": {
            "period": meta.get("period_curr"),
            "meta": public_meta(meta),
            "rows": [public_product(row) for row in rows],
        },
    }


@router.get("/products/{fund_key:path}")
def get_copywork_product(
    fund_key: str,
    period: Optional[str] = Query(default=None, pattern=r"^\d{8}$"),
    current_user: Dict[str, Any] = Depends(get_current_active_user),
):
    meta = _meta_for(period)
    if not meta:
        raise HTTPException(status_code=404, detail="没有该报告期的抄作业快照")
    key = unquote(fund_key)
    coll = _coll("fund_copywork_products")
    doc = coll.find_one(
        {
            "universe": _UNIVERSE,
            "fund_key": key,
            "period_curr": meta.get("period_curr"),
            "period_prev": meta.get("period_prev"),
        },
        {"_id": 0},
    )
    if not doc:
        raise HTTPException(status_code=404, detail="观察名单中没有该产品")
    product = public_product(doc, include_holdings=True)
    names = _holding_names(
        [row.get("symbol") for row in (product.get("holdings") or [])],
        period_prev=meta.get("period_prev"),
        period_curr=meta.get("period_curr"),
    )
    for row in product.get("holdings") or []:
        if not row.get("name"):
            row["name"] = names.get(str(row.get("symbol") or ""))
    return {
        "success": True,
        "data": {
            "meta": public_meta(meta),
            "product": product,
        },
    }


@router.get("/consensus")
def list_copywork_consensus(
    period: Optional[str] = Query(default=None, pattern=r"^\d{8}$"),
    limit: int = Query(default=50, ge=1, le=200),
    current_user: Dict[str, Any] = Depends(get_current_active_user),
):
    meta = _meta_for(period)
    if not meta:
        return {"success": True, "data": {"period": period, "rows": [], "meta": {}}}
    coll = _coll("fund_copywork_stocks")
    query = {
        "universe": _UNIVERSE,
        "period_curr": meta.get("period_curr"),
        "period_prev": meta.get("period_prev"),
    }
    try:
        rows = list(
            coll.find(query, {"_id": 0}, sort=[("n_operated", -1)], limit=limit)
        )
    except Exception as exc:
        logger.exception("list fund copywork consensus failed")
        raise HTTPException(status_code=503, detail="抄作业数据暂时不可用") from exc
    return {
        "success": True,
        "data": {
            "period": meta.get("period_curr"),
            "meta": public_meta(meta),
            "rows": [public_stock(row) for row in rows],
        },
    }


@router.get("/stock/{symbol}")
def get_copywork_stock(
    symbol: str,
    period: Optional[str] = Query(default=None, pattern=r"^\d{8}$"),
    current_user: Dict[str, Any] = Depends(get_current_active_user),
):
    meta = _meta_for(period)
    if not meta:
        raise HTTPException(status_code=404, detail="没有该报告期的抄作业快照")
    coll = _coll("fund_copywork_stocks")
    key = symbol.strip().upper()
    short = key.split(".")[0]
    doc = coll.find_one(
        {
            "universe": _UNIVERSE,
            "period_curr": meta.get("period_curr"),
            "period_prev": meta.get("period_prev"),
            "symbol": {"$in": [key, short, symbol.strip()]},
        },
        {"_id": 0},
    )
    if not doc:
        return {
            "success": True,
            "data": {
                "meta": public_meta(meta),
                "stock": {
                    "symbol": short,
                    "n_operated": 0,
                    "n_new": 0,
                    "n_increased": 0,
                    "n_decreased": 0,
                    "n_exited": 0,
                    "cards": [],
                },
            },
        }
    return {"success": True, "data": {"meta": public_meta(meta), "stock": public_stock(doc)}}
