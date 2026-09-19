from __future__ import annotations

from typing import Any, Dict, Optional

from bson import ObjectId
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from auth import get_current_active_user, require_hermes_quant_internal_key
from db import get_db_collections
from services.intraday_t0_signals import (
    create_mock_positions_from_watchlist,
    ensure_intraday_t0_indexes,
    evaluate_signals_for_user,
    export_evaluations_csv,
    export_signals_csv,
    generate_signals_for_all_users,
    generate_signals_for_user,
    get_user_positions,
    performance_summary,
    upsert_mock_position,
)
from services.intraday_t0_trades import (
    day_close_check,
    day_summary,
    ensure_intraday_t0_trade_indexes,
    list_open_pairs,
    list_trades,
    preview_trade_from_signal,
    record_trade,
)
from services.intraday_t0_execution import (
    ensure_intraday_t0_execution_indexes,
    get_user_settings,
    list_executions,
    preview_execution_async,
    submit_execution_async,
    upsert_user_settings,
)


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


class ExecutionSettingsPayload(BaseModel):
    plan_id: Optional[str] = None
    securities_account_id: Optional[str] = None
    auto_execute_enabled: Optional[bool] = None
    auto_min_score: Optional[float] = Field(None, ge=0, le=100)
    sell_qty_ratio: Optional[float] = Field(None, gt=0, le=1)
    buy_qty_ratio: Optional[float] = Field(None, gt=0, le=1)
    buy_first_enabled: Optional[bool] = None


class ExecutionSubmitPayload(BaseModel):
    signal_id: str = Field(..., min_length=1)
    plan_id: Optional[str] = None
    securities_account_id: Optional[str] = None
    quantity: Optional[int] = Field(None, gt=0)
    pair_id: Optional[str] = Field(
        None,
        description="Optional pair id. Leave empty to open a new sell_first or buy_first pair.",
    )
    sell_qty_ratio: Optional[float] = Field(None, gt=0, le=1)
    buy_qty_ratio: Optional[float] = Field(None, gt=0, le=1)
    dry_run: bool = False


class RecordTradePayload(BaseModel):
    symbol: str = Field(..., min_length=1)
    leg: str = Field(..., pattern="^(sell|buy)$")
    quantity: int = Field(..., gt=0)
    price: float = Field(..., gt=0)
    trade_date: Optional[str] = None
    fees: Optional[float] = Field(None, ge=0)
    signal_id: Optional[str] = None
    pair_id: Optional[str] = Field(
        None,
        description="Optional pair id. Leave empty to open a new sell_first or buy_first pair.",
    )
    notes: Optional[str] = None


from utils.stock_utils import serialize_doc


def _user_id(current_user: Dict[str, Any]) -> str:
    return str(current_user["id"])


@router.get("/health/liveness")
def intraday_t0_liveness():
    """Structural health for dev/CI and k8s liveness — no live market data required."""
    from services.intraday_t0_health import run_liveness_checks

    report = run_liveness_checks()
    status_code = 200 if report.overall in {"pass", "warn"} else 503
    from fastapi.responses import JSONResponse

    return JSONResponse(content=report.to_dict(), status_code=status_code)


@router.get("/health/readiness")
def intraday_t0_readiness(
    trade_date: Optional[str] = Query(None, description="YYYYMMDD or YYYY-MM-DD"),
    user_id: Optional[str] = Query(None, description="Optional user scope"),
    _: bool = Depends(require_hermes_quant_internal_key),
):
    """Prod pre-flight: realtime pipeline + signal activity for trade_date."""
    from services.intraday_t0_health import run_readiness_checks

    report = run_readiness_checks(trade_date=trade_date, user_id=user_id)
    status_code = 200 if report.overall in {"pass", "warn"} else 503
    from fastapi.responses import JSONResponse

    return JSONResponse(content=report.to_dict(), status_code=status_code)


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


@router.get("/signals/export")
def export_signals(
    trade_date: Optional[str] = Query(None, description="YYYYMMDD or YYYY-MM-DD"),
    current_user: dict = Depends(get_current_active_user),
):
    from services.intraday_t0_signals import compact_trade_date

    try:
        td = compact_trade_date(trade_date) if trade_date else "all"
        content = export_signals_csv(_user_id(current_user), trade_date=trade_date)
        filename = f"intraday_t0_signals_{td}.csv"
        return Response(
            content=content,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"导出日内T+0信号失败: {exc}")


@router.get("/signals/evaluations/export")
def export_signal_evaluations(
    trade_date: Optional[str] = Query(None, description="YYYYMMDD or YYYY-MM-DD"),
    current_user: dict = Depends(get_current_active_user),
):
    from services.intraday_t0_signals import compact_trade_date

    try:
        td = compact_trade_date(trade_date) if trade_date else "all"
        content = export_evaluations_csv(_user_id(current_user), trade_date=trade_date)
        filename = f"intraday_t0_evaluations_{td}.csv"
        return Response(
            content=content,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"导出日内T+0评估失败: {exc}")


@router.get("/trades/preview")
def preview_trade(
    signal_id: str = Query(..., min_length=1),
    sell_qty_ratio: float = Query(0.5, gt=0, le=1),
    buy_qty_ratio: Optional[float] = Query(None, gt=0, le=1),
    current_user: dict = Depends(get_current_active_user),
):
    try:
        return preview_trade_from_signal(
            _user_id(current_user),
            signal_id,
            sell_qty_ratio=sell_qty_ratio,
            buy_qty_ratio=buy_qty_ratio,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"预览T+0成交失败: {exc}")


@router.post("/trades")
def create_trade(
    payload: RecordTradePayload,
    current_user: dict = Depends(get_current_active_user),
):
    try:
        return record_trade(_user_id(current_user), payload.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"记录T+0成交失败: {exc}")


@router.get("/trades")
def get_trades(
    trade_date: Optional[str] = Query(None, description="YYYYMMDD or YYYY-MM-DD"),
    symbol: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    current_user: dict = Depends(get_current_active_user),
):
    try:
        rows = list_trades(_user_id(current_user), trade_date=trade_date, symbol=symbol, limit=limit)
        return {"success": True, "count": len(rows), "data": rows}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"获取T+0成交记录失败: {exc}")


@router.get("/trades/open")
def get_open_trade_pairs(
    trade_date: Optional[str] = Query(None, description="YYYYMMDD or YYYY-MM-DD"),
    current_user: dict = Depends(get_current_active_user),
):
    try:
        rows = list_open_pairs(_user_id(current_user), trade_date=trade_date)
        return {"success": True, "count": len(rows), "data": rows}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"获取未回补配对失败: {exc}")


@router.get("/trades/day-summary")
def get_trade_day_summary(
    trade_date: Optional[str] = Query(None, description="YYYYMMDD or YYYY-MM-DD"),
    current_user: dict = Depends(get_current_active_user),
):
    try:
        return day_summary(_user_id(current_user), trade_date=trade_date)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"获取T+0日终汇总失败: {exc}")


@router.get("/execution/settings")
def get_execution_settings(current_user: dict = Depends(get_current_active_user)):
    try:
        return get_user_settings(_user_id(current_user))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"获取T+0执行设置失败: {exc}")


@router.put("/execution/settings")
def update_execution_settings(
    payload: ExecutionSettingsPayload,
    current_user: dict = Depends(get_current_active_user),
):
    try:
        return upsert_user_settings(
            _user_id(current_user),
            payload.model_dump(exclude_none=True),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"更新T+0执行设置失败: {exc}")


@router.post("/execution/preview")
async def preview_execution(
    payload: ExecutionSubmitPayload,
    current_user: dict = Depends(get_current_active_user),
):
    try:
        return await preview_execution_async(
            _user_id(current_user),
            payload.signal_id,
            plan_id=payload.plan_id,
            securities_account_id=payload.securities_account_id,
            quantity=payload.quantity,
            pair_id=payload.pair_id,
            sell_qty_ratio=payload.sell_qty_ratio,
            buy_qty_ratio=payload.buy_qty_ratio,
            current_user=current_user,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"预览T+0下单失败: {exc}")


@router.post("/execution/submit")
async def submit_execution(
    payload: ExecutionSubmitPayload,
    current_user: dict = Depends(get_current_active_user),
):
    try:
        return await submit_execution_async(
            _user_id(current_user),
            payload.signal_id,
            plan_id=payload.plan_id,
            securities_account_id=payload.securities_account_id,
            quantity=payload.quantity,
            pair_id=payload.pair_id,
            sell_qty_ratio=payload.sell_qty_ratio,
            buy_qty_ratio=payload.buy_qty_ratio,
            dry_run=payload.dry_run,
            current_user=current_user,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"提交T+0下单失败: {exc}")


@router.get("/execution/history")
def get_execution_history(
    limit: int = Query(100, ge=1, le=500),
    current_user: dict = Depends(get_current_active_user),
):
    try:
        rows = list_executions(_user_id(current_user), limit=limit)
        return {"success": True, "count": len(rows), "data": rows}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"获取T+0执行历史失败: {exc}")


@router.post("/trades/day-close-check")
def run_day_close_check(
    trade_date: Optional[str] = Query(None, description="YYYYMMDD or YYYY-MM-DD"),
    current_user: dict = Depends(get_current_active_user),
):
    try:
        return day_close_check(_user_id(current_user), trade_date=trade_date)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"T+0日终检查失败: {exc}")


@router.on_event("startup")
def _ensure_indexes_on_startup() -> None:
    ensure_intraday_t0_indexes()
    ensure_intraday_t0_trade_indexes()
    ensure_intraday_t0_execution_indexes()
