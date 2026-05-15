from __future__ import annotations

from typing import Any, Dict, Optional

from bson import ObjectId
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from auth import get_current_active_user, require_hermes_quant_internal_key
from db import get_db_collections
from services.intraday_t0_signals import (
    create_mock_positions_from_watchlist,
    ensure_intraday_t0_indexes,
    evaluate_signals_for_user,
    generate_signals_for_all_users,
    generate_signals_for_user,
    get_user_positions,
    performance_summary,
    upsert_mock_position,
)
from utils.stock_utils import serialize_doc


router = APIRouter(prefix="/intraday-t0", tags=["日内T+0"])


class MockPositionPayload(BaseModel):
    symbol: str = Field(..., min_length=1)
    name: Optional[str] = None
    quantity: int = Field(0, ge=0)
    available_quantity: Optional[int] = Field(None, ge=0)
    cost_price: float = Field(0.0, ge=0)
    enabled: bool = True


class GenerateSignalsPayload(BaseModel):
    trade_date: Optional[str] = None
    snapshot_time: Optional[str] = None
    source: str = "auto"


class WatchlistImportPayload(BaseModel):
    default_quantity: int = Field(100, ge=0)


def _user_id(current_user: Dict[str, Any]) -> str:
    return str(current_user["id"])


@router.get("/positions")
def get_positions(
    source: str = Query("auto", pattern="^(auto|real|mock|watchlist)$"),
    trade_date: Optional[str] = Query(None, description="YYYYMMDD or YYYY-MM-DD"),
    current_user: dict = Depends(get_current_active_user),
):
    """Return normalized real/mock/watchlist positions enriched with realtime daily K data."""
    try:
        actual_source, positions = get_user_positions(_user_id(current_user), source=source, trade_date=trade_date)
        market = {}
        if positions:
            from services.intraday_t0_signals import compact_trade_date, latest_market_spectrum

            market = latest_market_spectrum(compact_trade_date(trade_date or positions[0].get("trade_date")))
        return {
            "success": True,
            "source": actual_source,
            "count": len(positions),
            "data": positions,
            "market": market,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"获取日内T+0持仓失败: {exc}")


@router.post("/mock-positions")
def create_mock_position(
    payload: MockPositionPayload,
    current_user: dict = Depends(get_current_active_user),
):
    try:
        position = upsert_mock_position(_user_id(current_user), payload.model_dump(exclude_none=True))
        return {"success": True, "data": position}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"创建模拟仓位失败: {exc}")


@router.put("/mock-positions/{position_id}")
def update_mock_position(
    position_id: str,
    payload: MockPositionPayload,
    current_user: dict = Depends(get_current_active_user),
):
    try:
        ObjectId(position_id)
    except Exception:
        raise HTTPException(status_code=400, detail="无效的 position_id")
    try:
        position = upsert_mock_position(_user_id(current_user), payload.model_dump(exclude_none=True), position_id)
        return {"success": True, "data": position}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"更新模拟仓位失败: {exc}")


@router.delete("/mock-positions/{position_id}")
def delete_mock_position(
    position_id: str,
    current_user: dict = Depends(get_current_active_user),
):
    try:
        oid = ObjectId(position_id)
    except Exception:
        raise HTTPException(status_code=400, detail="无效的 position_id")
    result = get_db_collections()["intraday_t0_mock_positions"].delete_one(
        {"_id": oid, "user_id": _user_id(current_user)}
    )
    return {"success": True, "deleted_count": result.deleted_count}


@router.post("/mock-positions/from-watchlist")
def create_mock_positions_from_watchlist_endpoint(
    payload: Optional[WatchlistImportPayload] = Body(None),
    current_user: dict = Depends(get_current_active_user),
):
    payload = payload or WatchlistImportPayload()
    try:
        positions = create_mock_positions_from_watchlist(
            _user_id(current_user),
            default_quantity=payload.default_quantity,
        )
        return {"success": True, "count": len(positions), "data": positions}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"从自选股生成模拟仓位失败: {exc}")


@router.post("/signals/generate")
def generate_signals(
    payload: Optional[GenerateSignalsPayload] = Body(None),
    current_user: dict = Depends(get_current_active_user),
):
    payload = payload or GenerateSignalsPayload()
    try:
        return generate_signals_for_user(
            _user_id(current_user),
            trade_date=payload.trade_date,
            snapshot_time=payload.snapshot_time,
            source=payload.source,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"生成日内T+0信号失败: {exc}")


@router.post("/signals/generate-all")
def generate_all_signals_internal(
    payload: Optional[GenerateSignalsPayload] = Body(None),
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """Internal endpoint called after realtime daily K sync finishes."""
    payload = payload or GenerateSignalsPayload()
    try:
        return generate_signals_for_all_users(payload.trade_date, payload.snapshot_time)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"批量生成日内T+0信号失败: {exc}")


@router.get("/signals")
def get_signals(
    trade_date: Optional[str] = Query(None, description="YYYYMMDD or YYYY-MM-DD"),
    symbol: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    current_user: dict = Depends(get_current_active_user),
):
    from services.intraday_t0_signals import compact_trade_date, normalize_symbol

    query: Dict[str, Any] = {"user_id": _user_id(current_user)}
    if trade_date:
        query["trade_date"] = compact_trade_date(trade_date)
    if symbol:
        query["symbol"] = normalize_symbol(symbol)
    cursor = (
        get_db_collections()["intraday_t0_signals"]
        .find(query)
        .sort([("snapshot_time", -1), ("_id", -1)])
        .limit(limit)
    )
    rows = [serialize_doc(doc) for doc in cursor]
    return {"success": True, "count": len(rows), "data": rows}


@router.post("/signals/evaluate")
def evaluate_signals(
    trade_date: Optional[str] = Query(None, description="YYYYMMDD or YYYY-MM-DD"),
    current_user: dict = Depends(get_current_active_user),
):
    try:
        return evaluate_signals_for_user(_user_id(current_user), trade_date=trade_date)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"评估日内T+0信号失败: {exc}")


@router.get("/signals/performance")
def get_signal_performance(
    trade_date: Optional[str] = Query(None, description="YYYYMMDD or YYYY-MM-DD"),
    current_user: dict = Depends(get_current_active_user),
):
    try:
        return performance_summary(_user_id(current_user), trade_date=trade_date)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"获取日内T+0表现统计失败: {exc}")


@router.on_event("startup")
def _ensure_indexes_on_startup() -> None:
    ensure_intraday_t0_indexes()
