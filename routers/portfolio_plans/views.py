"""Read-model/view endpoints for portfolio plans.

Mechanical extraction only, no behavior change. The sub-router is mounted by
``portfolio_plans.__init__`` onto the existing ``/portfolio-plans`` router.
Helpers that still live in the package module are resolved through ``_pp`` at
call time so existing tests can keep monkeypatching the package namespace.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query

from auth import get_current_user

import routers.portfolio_plans as _pp
from services.live_equity import (
    compute_live_plan_realtime_equity,
    list_live_plan_equity,
)
from services.portfolio_lineage_paper import (
    build_lineage_paper_trades,
    merge_lineage_paper_executions,
)
from . import _offload
from ._shared import _int_value, _jsonable, _numeric


router = APIRouter()


@router.get("/plans/{plan_id}/lineage-equity")
@_offload
def get_portfolio_plan_lineage_equity(
    plan_id: str,
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
    current_user: dict = Depends(get_current_user),
):
    collections = _pp.get_trading_db_collections()
    user_id = _pp._current_user_id(current_user)
    plan = _pp._require_plan(collections, plan_id, current_user)
    query = _pp._lineage_query_for_plan(collections, plan, user_id)
    if start_date or end_date:
        date_query: Dict[str, Any] = {}
        if start_date:
            date_query["$gte"] = start_date.replace("-", "")
        if end_date:
            date_query["$lte"] = end_date.replace("-", "")
        query["date"] = date_query
    equity_rows = list(collections["portfolio_paper_equity"].find(query).sort("date", 1))
    position_query = dict(query)
    if "date" in position_query:
        position_query["as_of_date"] = position_query.pop("date")
    position_rows = list(collections["portfolio_paper_positions"].find(position_query).sort("as_of_date", 1))
    rows_by_key: Dict[tuple, Dict[str, Any]] = {}
    for row in equity_rows:
        key = (row.get("plan_id"), row.get("date"))
        rows_by_key[key] = dict(row)
    for row in position_rows:
        date = row.get("as_of_date")
        key = (row.get("plan_id"), date)
        if key in rows_by_key or not date:
            continue
        rows_by_key[key] = {
            "strategy_template_id": row.get("strategy_template_id"),
            "user_id": row.get("user_id"),
            "params_hash": row.get("params_hash"),
            "date": date,
            "plan_id": row.get("plan_id"),
            "inherited_from_plan_id": row.get("inherited_from_plan_id"),
            "inherited_from_as_of_date": row.get("inherited_from_as_of_date"),
            "equity": row.get("equity"),
            "cash": row.get("cash"),
            "market_value": row.get("market_value"),
            "updated_at": row.get("updated_at"),
            "source": "position_snapshot",
        }
    latest_by_date: Dict[str, Dict[str, Any]] = {}
    for row in rows_by_key.values():
        date = str(row.get("date") or "")
        if not date:
            continue
        existing = latest_by_date.get(date)
        if existing is None or str(row.get("updated_at") or "") >= str(existing.get("updated_at") or ""):
            latest_by_date[date] = row
    rows = sorted(latest_by_date.values(), key=lambda row: str(row.get("date") or ""))
    plan_ids = sorted({row.get("plan_id") for row in rows if row.get("plan_id")})
    plan_map = {
        row.get("plan_id"): row
        for row in collections["portfolio_trade_plans"].find(
            {"plan_id": {"$in": plan_ids}, "user_id": user_id},
            {"_id": 0, "plan_id": 1, "base_date": 1, "plan_type": 1, "status": 1},
        )
    } if plan_ids else {}
    enriched = []
    for row in rows:
        source_plan = plan_map.get(row.get("plan_id"), {})
        enriched.append(
            {
                **row,
                "source_plan_base_date": source_plan.get("base_date"),
                "source_plan_type": source_plan.get("plan_type"),
                "source_plan_status": source_plan.get("status"),
                "is_current_plan": row.get("plan_id") == plan_id,
            }
        )
    return {
        "success": True,
        "data": _jsonable(
            {
                "plan_id": plan_id,
                "strategy_template_id": plan.get("strategy_template_id"),
                "params_hash": plan.get("params_hash"),
                "rows": enriched,
                "source_plan_ids": plan_ids,
                "initial_capital": _pp._lineage_initial_capital(plan),
            }
        ),
    }


@router.get("/plans/{plan_id}/lineage-paper-positions")
@_offload
def get_lineage_paper_positions(plan_id: str, current_user: dict = Depends(get_current_user)):
    """Latest paper position snapshot across the plan's params_hash lineage."""

    collections = _pp.get_trading_db_collections()
    data_collections = _pp.get_data_db()
    user_id = _pp._current_user_id(current_user)
    plan = _pp._require_plan(collections, plan_id, current_user)
    snapshot = _pp._latest_lineage_paper_snapshot(collections, plan, user_id)
    raw_positions = snapshot.get("positions") or []
    symbols = sorted({str(row.get("symbol")) for row in raw_positions if row.get("symbol")})
    quotes = _pp._market_price_quotes(
        data_collections,
        symbols,
        day_pnl_date=_pp._business_today_yyyymmdd(),
    )
    positions: List[Dict[str, Any]] = []
    total_market_value = 0.0
    total_day_pnl = 0.0
    for row in sorted(raw_positions, key=lambda item: str(item.get("symbol") or "")):
        symbol = str(row.get("symbol") or "")
        shares = _int_value(row.get("shares"))
        if shares <= 0:
            continue
        quote = quotes.get(symbol) or quotes.get(symbol.split(".", 1)[0]) or {}
        avg_cost = _numeric(row.get("cost_basis") or row.get("mark_price"))
        last_price = _numeric(quote.get("price"), _numeric(row.get("mark_price") or row.get("cost_basis")))
        market_value = shares * last_price
        total_market_value += market_value
        previous_close = _numeric(quote.get("previous_close"))
        day_change = last_price - previous_close if last_price > 0 and previous_close > 0 else None
        day_pnl = day_change * shares if day_change is not None else None
        if day_pnl is not None:
            total_day_pnl += day_pnl
        positions.append(
            {
                "symbol": symbol,
                "name": row.get("name", ""),
                "shares": shares,
                "avg_cost": avg_cost,
                "last_price": last_price,
                "price_source": quote.get("price_source") or "paper_mark",
                "price_as_of": quote.get("price_as_of"),
                "previous_close": previous_close if previous_close > 0 else None,
                "day_change": round(day_change, 4) if day_change is not None else None,
                "day_change_pct": round(day_change / previous_close, 6) if day_change is not None and previous_close > 0 else None,
                "day_pnl": round(day_pnl, 2) if day_pnl is not None else None,
                "market_value": round(market_value, 2),
                "realized_pnl": 0.0,
                "unrealized_pnl": round((last_price - avg_cost) * shares, 2) if last_price and avg_cost else 0.0,
                "unrealized_pnl_pct": round(last_price / avg_cost - 1, 6) if last_price > 0 and avg_cost > 0 else None,
            }
        )
    equity = _numeric(snapshot.get("equity"))
    cash = _numeric(snapshot.get("cash"))
    return {
        "success": True,
        "data": _jsonable(
            {
                "anchor_plan_id": plan_id,
                "strategy_template_id": plan.get("strategy_template_id"),
                "params_hash": plan.get("params_hash"),
                "as_of_date": snapshot.get("as_of_date"),
                "source_plan_id": snapshot.get("plan_id"),
                "positions": positions,
                "summary": {
                    "holding_count": len(positions),
                    "total_market_value": round(total_market_value, 2),
                    "total_realized_pnl": 0.0,
                    "total_unrealized_pnl": sum(_numeric(row.get("unrealized_pnl")) for row in positions),
                    "total_day_pnl": round(total_day_pnl, 2),
                    "equity": equity,
                    "cash": cash,
                },
            }
        ),
    }


@router.get("/plans/{plan_id}/lineage-paper-executions")
@_offload
def get_lineage_paper_executions(plan_id: str, current_user: dict = Depends(get_current_user)):
    """FIFO round-trip trade rows from paper executions across the plan lineage."""

    trading_collections = _pp.get_trading_db_collections()
    data_collections = _pp.get_data_db()
    user_id = _pp._current_user_id(current_user)
    plan = _pp._require_plan(trading_collections, plan_id, current_user)
    lineage_ids = _pp._lineage_plan_ids_for_plan(trading_collections, plan, user_id)
    if not lineage_ids:
        return {
            "success": True,
            "data": _jsonable(
                {
                    "anchor_plan_id": plan_id,
                    "lineage_plan_ids": [],
                    "strategy_template_id": plan.get("strategy_template_id"),
                    "params_hash": plan.get("params_hash"),
                    "executions": [],
                    "trades": [],
                    "execution_count": 0,
                    "trade_count": 0,
                }
            ),
        }

    execution_rows = list(
        trading_collections["portfolio_paper_executions"]
        .find({"user_id": user_id, "plan_id": {"$in": lineage_ids}})
        .sort([("execute_date", 1), ("created_at", 1)])
    )
    plan_rows = list(
        trading_collections["portfolio_trade_plans"].find(
            {"user_id": user_id, "plan_id": {"$in": lineage_ids}},
            {"_id": 0, "plan_id": 1, "base_date": 1},
        )
    )
    plan_meta = {str(row.get("plan_id")): dict(row) for row in plan_rows if row.get("plan_id")}
    merged = merge_lineage_paper_executions(execution_rows, plan_meta)
    symbols = sorted({str(row.get("symbol")) for row in merged if row.get("symbol")})
    prices, _realtime_docs = _pp._load_fresh_market_prices(data_collections, symbols)
    name_map = {
        str(row.get("symbol")): str(row.get("name") or "")
        for row in merged
        if row.get("symbol") and row.get("name")
    }
    for symbol, name in list(name_map.items()):
        name_map.setdefault(symbol.split(".", 1)[0], name)
    trades = build_lineage_paper_trades(merged, prices, name_map=name_map)

    return {
        "success": True,
        "data": _jsonable(
            {
                "anchor_plan_id": plan_id,
                "lineage_plan_ids": lineage_ids,
                "strategy_template_id": plan.get("strategy_template_id"),
                "params_hash": plan.get("params_hash"),
                "trade_source": "paper_executions",
                "executions": merged,
                "trades": trades,
                "execution_count": len(merged),
                "trade_count": len(trades),
            }
        ),
    }


@router.get("/plans/{plan_id}/live-executions")
@_offload
def get_portfolio_plan_live_executions(plan_id: str, current_user: dict = Depends(get_current_user)):
    trading_collections = _pp.get_trading_db_collections()
    finance_collections = _pp.get_db_collections()
    data_collections = _pp.get_data_db()
    _pp._require_plan(trading_collections, plan_id, current_user)
    user_id = _pp._current_user_id(current_user)

    signal_query = {
        "user_id": user_id,
        "plan_id": plan_id,
        "mode": "live",
    }
    signals = _pp._attach_signal_names(
        data_collections,
        [
            _pp.add_display_time_fields(row)
            for row in finance_collections["trade_signals"].find(signal_query).sort("created_at", 1)
        ],
    )
    order_ids = [row.get("order_id") for row in signals if row.get("order_id")]
    execution_query = {"user_id": user_id, "order_id": {"$in": order_ids}} if order_ids else None
    executions = (
        [
            _pp.add_display_time_fields(row)
            for row in finance_collections["trade_executions"].find(execution_query).sort("timestamp", -1)
        ]
        if execution_query
        else []
    )
    return {
        "success": True,
        "data": _jsonable(
            {
                "plan_id": plan_id,
                "signals": signals,
                "executions": executions,
                "signal_count": len(signals),
                "execution_count": len(executions),
            }
        ),
    }


@router.get("/plans/{plan_id}/live-equity")
@_offload
def get_portfolio_plan_live_equity(
    plan_id: str,
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
    current_user: dict = Depends(get_current_user),
):
    collections = _pp.get_trading_db_collections()
    _pp._require_plan(collections, plan_id, current_user)
    rows = list_live_plan_equity(
        collections,
        user_id=_pp._current_user_id(current_user),
        plan_id=plan_id,
        start_date=start_date,
        end_date=end_date,
    )
    return {"success": True, "data": _jsonable(rows)}


@router.get("/plans/{plan_id}/live-realtime-equity")
@_offload
def get_portfolio_plan_live_realtime_equity(plan_id: str, current_user: dict = Depends(get_current_user)):
    trading_collections = _pp.get_trading_db_collections()
    finance_collections = _pp.get_db_collections()
    data_collections = _pp.get_data_db()
    plan = _pp._require_plan(trading_collections, plan_id, current_user)
    row = compute_live_plan_realtime_equity(
        trading_collections,
        finance_collections,
        data_collections,
        user_id=_pp._current_user_id(current_user),
        plan_id=plan_id,
        plan=plan,
    )
    return {"success": True, "data": _jsonable(row)}


@router.get("/portfolios")
@_offload
def list_portfolios(current_user: dict = Depends(get_current_user)):
    """Enumerate live and paper portfolio lineages (strategy_template_id + params_hash)."""

    trading_collections = _pp.get_trading_db_collections()
    finance_collections = _pp.get_db_collections()
    user_id = _pp._current_user_id(current_user)
    portfolios = _pp._list_portfolios_payload(trading_collections, finance_collections, user_id)
    return {"success": True, "data": _jsonable({"portfolios": portfolios})}


@router.get("/plans/{plan_id}/portfolio-summary")
@_offload
def get_portfolio_plan_summary(
    plan_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Deferred portfolio summary: paper snapshot, holdings, equity, approval policy."""

    trading_collections = _pp.get_trading_db_collections()
    finance_collections = _pp.get_db_collections()
    user_id = _pp._current_user_id(current_user)
    plan = _pp._require_plan(trading_collections, plan_id, current_user)
    summary = _pp._build_portfolio_summary_payload(
        trading_collections, finance_collections, plan, user_id
    )
    return {"success": True, "data": _jsonable(summary)}


@router.get("/live-portfolios")
@_offload
def list_live_portfolios(current_user: dict = Depends(get_current_user)):
    """Enumerate live-traded plan lineages (strategy_template_id + params_hash) for the user."""

    trading_collections = _pp.get_trading_db_collections()
    finance_collections = _pp.get_db_collections()
    user_id = _pp._current_user_id(current_user)
    portfolios = [
        row for row in _pp._list_portfolios_payload(trading_collections, finance_collections, user_id)
        if row.get("execution_venue") == "live"
    ]
    return {"success": True, "data": _jsonable({"portfolios": portfolios})}


@router.get("/plans/{plan_id}/lineage-timeline")
@_offload
def get_portfolio_plan_lineage_timeline(
    plan_id: str,
    limit: int = Query(default=60, ge=1, le=240),
    offset: int = Query(default=0, ge=0),
    current_user: dict = Depends(get_current_user),
):
    trading_collections = _pp.get_trading_db_collections()
    finance_collections = _pp.get_db_collections()
    data_collections = _pp.get_data_db()
    user_id = _pp._current_user_id(current_user)
    plan = _pp._require_plan(trading_collections, plan_id, current_user)
    payload = _pp._build_lineage_timeline_payload(
        trading_collections,
        finance_collections,
        data_collections,
        plan,
        user_id,
        limit=limit,
        offset=offset,
    )
    return {"success": True, "data": _jsonable(payload)}


@router.get("/plans/{plan_id}/lineage-live-executions")
@_offload
def get_lineage_live_executions(plan_id: str, current_user: dict = Depends(get_current_user)):
    trading_collections = _pp.get_trading_db_collections()
    finance_collections = _pp.get_db_collections()
    data_collections = _pp.get_data_db()
    user_id = _pp._current_user_id(current_user)
    plan = _pp._require_plan(trading_collections, plan_id, current_user)

    snapshot, overlay = _pp._load_snapshot_with_realtime(
        trading_collections, finance_collections, data_collections, plan, user_id
    )
    if not snapshot:
        return {
            "success": True,
            "data": _jsonable(
                {
                    "anchor_plan_id": plan_id,
                    "lineage_plan_ids": [],
                    "strategy_template_id": plan.get("strategy_template_id"),
                    "params_hash": plan.get("params_hash"),
                    "signals": [],
                    "executions": [],
                    "trades": [],
                    "signal_count": 0,
                    "execution_count": 0,
                    "trade_count": 0,
                }
            ),
        }

    trades = overlay.get("trades") or []
    watermark = snapshot.get("watermark") or {}
    return {
        "success": True,
        "data": _jsonable(
            {
                "anchor_plan_id": plan_id,
                "lineage_plan_ids": snapshot.get("lineage_plan_ids") or [],
                "strategy_template_id": plan.get("strategy_template_id"),
                "params_hash": plan.get("params_hash"),
                "signals": [],
                "executions": [],
                "trades": trades,
                "signal_count": int(watermark.get("signal_count") or 0),
                "execution_count": int(watermark.get("execution_count") or 0),
                "trade_count": len(trades),
            }
        ),
    }


@router.get("/plans/{plan_id}/lineage-live-positions")
@_offload
def get_lineage_live_positions(plan_id: str, current_user: dict = Depends(get_current_user)):
    trading_collections = _pp.get_trading_db_collections()
    finance_collections = _pp.get_db_collections()
    data_collections = _pp.get_data_db()
    user_id = _pp._current_user_id(current_user)
    plan = _pp._require_plan(trading_collections, plan_id, current_user)

    snapshot, overlay = _pp._load_snapshot_with_realtime(
        trading_collections, finance_collections, data_collections, plan, user_id
    )
    if not snapshot:
        return {
            "success": True,
            "data": _jsonable(
                {
                    "anchor_plan_id": plan_id,
                    "lineage_plan_ids": [],
                    "strategy_template_id": plan.get("strategy_template_id"),
                    "params_hash": plan.get("params_hash"),
                    "positions": [],
                    "summary": {
                        "total_market_value": 0.0,
                        "total_realized_pnl": 0.0,
                        "total_unrealized_pnl": 0.0,
                        "total_cost_basis": 0.0,
                        "holding_count": 0,
                        "position_row_count": 0,
                    },
                    "latest_price_update": "",
                }
            ),
        }

    return {
        "success": True,
        "data": _jsonable(
            {
                "anchor_plan_id": plan_id,
                "lineage_plan_ids": snapshot.get("lineage_plan_ids") or [],
                "strategy_template_id": plan.get("strategy_template_id"),
                "params_hash": plan.get("params_hash"),
                "positions": overlay.get("positions") or [],
                "summary": overlay.get("summary") or {},
                "latest_price_update": overlay.get("latest_price_update") or "",
            }
        ),
    }


@router.get("/plans/{plan_id}/lineage-live-equity")
@_offload
def get_lineage_live_equity(
    plan_id: str,
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
    current_user: dict = Depends(get_current_user),
):
    """Return daily live equity rows for the current portfolio lineage."""

    trading_collections = _pp.get_trading_db_collections()
    finance_collections = _pp.get_db_collections()
    data_collections = _pp.get_data_db()
    user_id = _pp._current_user_id(current_user)
    plan = _pp._require_plan(trading_collections, plan_id, current_user)

    chart_payload = _pp._build_lineage_live_equity_chart(
        trading_collections,
        finance_collections,
        data_collections,
        plan,
        user_id,
        plan_id,
        start_date=start_date,
        end_date=end_date,
    )
    snapshot, overlay = _pp._load_snapshot_with_realtime(
        trading_collections, finance_collections, data_collections, plan, user_id
    )
    if snapshot and overlay.get("current_book_equity"):
        current_book_equity = overlay["current_book_equity"]
    else:
        rows_for_chart = chart_payload.get("rows") or []
        row_source = chart_payload.get("row_source") or ""
        latest_equity_row = rows_for_chart[-1] if rows_for_chart else {}
        initial_capital = _pp._lineage_initial_capital(plan)
        current_equity = _numeric(latest_equity_row.get("equity"))
        current_cash = _numeric(latest_equity_row.get("cash"))
        current_market_value = _numeric(latest_equity_row.get("market_value"))
        if row_source == "portfolio_live_equity_legacy":
            current_equity = initial_capital
            current_cash = initial_capital
            current_market_value = 0.0
        execution_count = 0 if row_source == "portfolio_live_equity_legacy" else None
        current_book_equity = {
            "equity": current_equity,
            "cash": current_cash,
            "market_value": current_market_value,
            "initial_capital": initial_capital,
            "lineage_plan_ids": chart_payload.get("lineage_plan_ids") or [],
            "execution_count": execution_count,
            "latest_price_update": latest_equity_row.get("updated_at"),
            "source": row_source,
        }

    return {
        "success": True,
        "data": _jsonable({**chart_payload, "current_book_equity": current_book_equity}),
    }


@router.get("/plans/{plan_id}/live-summary")
@_offload
def get_portfolio_plan_live_summary(plan_id: str, current_user: dict = Depends(get_current_user)):
    """Consolidated live lineage snapshot for overview (positions, trades, equity book)."""

    trading_collections = _pp.get_trading_db_collections()
    finance_collections = _pp.get_db_collections()
    data_collections = _pp.get_data_db()
    user_id = _pp._current_user_id(current_user)
    plan = _pp._require_plan(trading_collections, plan_id, current_user)

    chart_payload = _pp._build_lineage_live_equity_chart(
        trading_collections, finance_collections, data_collections, plan, user_id, plan_id
    )
    snapshot, overlay = _pp._load_snapshot_with_realtime(
        trading_collections, finance_collections, data_collections, plan, user_id
    )
    try:
        reconcile_payload = _pp._reconcile_live_holdings(
            trading_collections, finance_collections, data_collections, plan, user_id, snapshot=snapshot
        )
    except Exception:
        reconcile_payload = {"applicable": False, "in_sync": True, "diffs": [], "securities_account_id": ""}

    current_book_equity = overlay.get("current_book_equity")
    if not current_book_equity:
        rows_for_chart = chart_payload.get("rows") or []
        latest_equity_row = rows_for_chart[-1] if rows_for_chart else {}
        initial_capital = _pp._lineage_initial_capital(plan)
        current_book_equity = {
            "equity": _numeric(latest_equity_row.get("equity"), initial_capital),
            "cash": _numeric(latest_equity_row.get("cash"), initial_capital),
            "market_value": _numeric(latest_equity_row.get("market_value")),
            "initial_capital": initial_capital,
            "lineage_plan_ids": chart_payload.get("lineage_plan_ids") or [],
            "execution_count": 0,
            "latest_price_update": latest_equity_row.get("updated_at"),
            "source": chart_payload.get("row_source") or "portfolio_live_equity_legacy",
        }

    snapshot_meta: Dict[str, Any] = {
        "available": bool(snapshot),
        "updated_at": snapshot.get("updated_at") if snapshot else None,
        "build_ms": snapshot.get("build_ms") if snapshot else None,
        "watermark": snapshot.get("watermark") if snapshot else {},
        "stale": None,
        "last_rebuild_reason": snapshot.get("last_rebuild_reason") if snapshot else "",
        "last_rebuild_trigger_order_id": snapshot.get("last_rebuild_trigger_order_id") if snapshot else "",
    }
    if snapshot:
        try:
            snapshot_meta["stale"] = bool(_pp.probe_stale(finance_collections, snapshot))
        except Exception:
            snapshot_meta["stale"] = None

    return {
        "success": True,
        "data": _jsonable(
            {
                "anchor_plan_id": plan_id,
                "strategy_template_id": plan.get("strategy_template_id"),
                "params_hash": plan.get("params_hash"),
                "lineage_plan_ids": snapshot.get("lineage_plan_ids") if snapshot else chart_payload.get("lineage_plan_ids"),
                "equity": {
                    "rows": chart_payload.get("rows") or [],
                    "caveat": chart_payload.get("caveat") or "",
                    "current_book_equity": current_book_equity,
                },
                "positions": {
                    "positions": overlay.get("positions") or [],
                    "summary": overlay.get("summary") or {},
                    "latest_price_update": overlay.get("latest_price_update") or "",
                },
                "trades": overlay.get("trades") or [],
                "reconcile": reconcile_payload,
                "snapshot_meta": snapshot_meta,
            }
        ),
    }


@router.get("/strategies/{strategy_template_id}/equity")
@_offload
def get_portfolio_strategy_equity(
    strategy_template_id: str,
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
    current_user: dict = Depends(get_current_user),
):
    collections = _pp.get_trading_db_collections()
    _pp._require_strategy(collections, strategy_template_id)
    query: Dict[str, Any] = {"strategy_template_id": strategy_template_id, "user_id": _pp._current_user_id(current_user)}
    if start_date or end_date:
        date_query: Dict[str, Any] = {}
        if start_date:
            date_query["$gte"] = start_date.replace("-", "")
        if end_date:
            date_query["$lte"] = end_date.replace("-", "")
        query["date"] = date_query
    rows = list(collections["portfolio_paper_equity"].find(query).sort("date", 1))
    return {"success": True, "data": _jsonable(rows)}


@router.get("/strategies/{strategy_template_id}/realtime-equity")
@_offload
def get_portfolio_strategy_realtime_equity(
    strategy_template_id: str,
    current_user: dict = Depends(get_current_user),
):
    collections = _pp.get_trading_db_collections()
    data_collections = _pp.get_data_db()
    _pp._require_strategy(collections, strategy_template_id)
    snapshot = collections["portfolio_paper_positions"].find_one(
        {"strategy_template_id": strategy_template_id, "user_id": _pp._current_user_id(current_user)},
        sort=[("as_of_date", -1), ("updated_at", -1)],
    )
    if not snapshot:
        return {"success": True, "data": None}

    positions = snapshot.get("positions", []) or []
    symbols = [row.get("symbol") for row in positions if row.get("symbol")]
    realtime_prices = _pp._load_prices(data_collections["volume_price_realtime_latest"], symbols, "", "close", latest=True)
    realtime_docs = _pp._load_realtime_docs(data_collections["volume_price_realtime_latest"], symbols)
    latest_close_prices = _pp._load_latest_close_prices(data_collections["volume_price"], symbols)

    position_rows = []
    market_value = 0.0
    for pos in positions:
        symbol = pos.get("symbol")
        shares = int(pos.get("shares", 0) or 0)
        realtime_doc = realtime_docs.get(symbol, {})
        price_source = "realtime"
        price = realtime_prices.get(symbol)
        if price is None:
            price = latest_close_prices.get(symbol)
            price_source = "latest_close" if price is not None else "paper_mark"
        if price is None:
            price = pos.get("mark_price") or pos.get("cost_basis") or 0
        price = float(price or 0)
        value = shares * price
        market_value += value
        position_rows.append(
            {
                **pos,
                "realtime_price": price,
                "realtime_market_value": value,
                "realtime_trade_time": realtime_doc.get("realtime_trade_time"),
                "snapshot_time": realtime_doc.get("snapshot_time"),
                "updated_at": realtime_doc.get("updated_at"),
                "price_source": price_source,
            }
        )

    cash = float(snapshot.get("cash", 0) or 0)
    realtime_equity = cash + market_value
    latest_equity = collections["portfolio_paper_equity"].find_one(
        {"strategy_template_id": strategy_template_id, "user_id": _pp._current_user_id(current_user)},
        sort=[("date", -1)],
    ) or {}
    base_equity = float(latest_equity.get("equity", snapshot.get("equity", 0)) or 0)
    change = realtime_equity - base_equity if base_equity else None
    change_pct = change / base_equity if base_equity else None
    latest_update = max(
        [str(row.get("updated_at") or row.get("realtime_trade_time") or row.get("snapshot_time") or "") for row in position_rows],
        default="",
    )
    return {
        "success": True,
        "data": _jsonable(
            {
                "strategy_template_id": strategy_template_id,
                "as_of_date": snapshot.get("as_of_date"),
                "cash": cash,
                "market_value": market_value,
                "equity": realtime_equity,
                "base_equity": base_equity,
                "change": change,
                "change_pct": change_pct,
                "latest_update": latest_update,
                "positions": position_rows,
            }
        ),
    }
