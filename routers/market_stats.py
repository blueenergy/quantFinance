"""Market statistics gallery API — 两融余额 and index PE series."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query

from auth import get_current_active_user
from db import get_data_db
from services.market_stats import (
    ALLA_CODE,
    MARGIN_ALL_EXCHANGE,
    MARGIN_DELTA_WINDOW,
    PE_INDEX_LABELS,
    attach_window_delta,
    lookback_start,
    merge_pe_by_code,
    pe_index_sort_key,
    pick_pe_index,
    serialize_margin_row,
    serialize_pe_row,
)

router = APIRouter(prefix="/market-stats", tags=["市场统计"])


def _empty_overview() -> Dict[str, Any]:
    return {
        "pe": {
            "index_code": None,
            "index_name": None,
            "available_indices": [],
            "latest": None,
            "series": [],
        },
        "margin": {
            "latest": None,
            "series": [],
        },
    }


@router.get("/overview", summary="市场统计图数据（两融 + 指数市盈率）")
async def get_market_stats_overview(
    days: int = Query(750, ge=60, le=2500, description="返回最近 N 个交易日"),
    pe_index: Optional[str] = Query(None, description="指数代码，如 ALLA.WI / 000001.SH；缺省按全A自算→上证→沪深300→创业板 回退"),
    current_user: dict = Depends(get_current_active_user),
):
    data = get_data_db()
    today = datetime.today().strftime("%Y%m%d")
    start = lookback_start(today, days + MARGIN_DELTA_WINDOW)
    payload = _empty_overview()

    pe_coll = data.get("index_daily_basic")
    alla_coll = data.get("market_pe_daily")
    index_docs: List[Dict[str, Any]] = []
    alla_docs: List[Dict[str, Any]] = []
    pe_projection = {
        "_id": 0,
        "ts_code": 1,
        "trade_date": 1,
        "pe": 1,
        "pe_ttm": 1,
        "pb": 1,
        "close": 1,
        "total_mv": 1,
        "n_universe": 1,
        "n_used": 1,
        "n_loss": 1,
        "n_missing": 1,
        "method": 1,
        "universe": 1,
    }
    if pe_coll is not None:
        index_docs = list(
            pe_coll.find({"trade_date": {"$gte": start}}, pe_projection).sort("trade_date", 1)
        )
    if alla_coll is not None:
        alla_docs = list(
            alla_coll.find(
                {"ts_code": ALLA_CODE, "trade_date": {"$gte": start}},
                pe_projection,
            ).sort("trade_date", 1)
        )
    by_code = merge_pe_by_code(index_docs, alla_docs)
    if by_code:
        available = [
            {
                "ts_code": code,
                "name": PE_INDEX_LABELS.get(code, code),
                "points": len(rows),
            }
            for code, rows in sorted(by_code.items(), key=lambda item: (pe_index_sort_key(item[0]), item[0]))
        ]
        chosen, name = pick_pe_index(by_code.keys(), pe_index)
        series_docs = by_code.get(chosen or "", [])[-days:]
        series = [serialize_pe_row(doc) for doc in series_docs]
        payload["pe"] = {
            "index_code": chosen,
            "index_name": name,
            "available_indices": available,
            "latest": series[-1] if series else None,
            "series": series,
        }

    margin_coll = data.get("margin_market_daily")
    if margin_coll is not None:
        margin_docs = list(
            margin_coll.find(
                {"exchange_id": MARGIN_ALL_EXCHANGE, "trade_date": {"$gte": start}},
                {"_id": 0, "trade_date": 1, "rzye": 1, "rqye": 1, "rzrqye": 1},
            ).sort("trade_date", 1)
        )
        with_delta = attach_window_delta(margin_docs, field="rzrqye", window=MARGIN_DELTA_WINDOW)
        series_docs = with_delta[-days:]
        series = [serialize_margin_row(doc) for doc in series_docs]
        payload["margin"] = {
            "latest": series[-1] if series else None,
            "series": series,
        }

    return payload


__all__ = ["router"]
