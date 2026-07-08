"""Paper-trading domain endpoints for portfolio plans.

Split out of the package ``__init__`` as a mechanical extraction (no behavior
change). Endpoints register on a prefix-less sub-router that ``__init__``
aggregates onto the ``/portfolio-plans`` router, so the public API paths and
OpenAPI tags are unchanged.

Cross-module helpers that still live in the package ``__init__`` (DB getters,
``_require_plan``, market/price loaders, lineage effects, ...) are referenced
through the package module (``_pp.<name>``) so they resolve dynamically at call
time. This keeps unit tests that ``monkeypatch`` those names on the
``portfolio_plans`` package working exactly as before.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query

from auth import get_current_user
from services.portfolio_record_kind import RECORD_KIND_TRADE_PLAN

import routers.portfolio_plans as _pp
from . import (
    PaperExecuteRequest,
    PaperRebalanceRequest,
    _offload,
)
from ._shared import (
    _business_today_yyyymmdd,
    _int_value,
    _jsonable,
    _numeric,
    _symbol_key,
)


router = APIRouter()


@router.post("/plans/{plan_id}/paper-rebalance")
async def paper_rebalance_portfolio(
    plan_id: str,
    request: PaperRebalanceRequest,
    current_user: dict = Depends(get_current_user),
):
    """One-click intraday PAPER position change (reduce / clear / add) for a lineage.

    The paper-book mirror of ``live-rebalance``: computes per-symbol deltas from
    ``targets`` against the latest lineage paper snapshot, fills them immediately
    at the realtime price (no next-day-open cron), and writes the new paper
    position snapshot / equity / executions plus a manual (origin=manual) plan for
    audit. Liquidation is just every held symbol mapped to 0.
    """
    trading_collections = _pp.get_trading_db_collections()
    data_collections = _pp.get_data_db()
    user_id = _pp._current_user_id(current_user)

    plan = _pp._require_plan(trading_collections, plan_id, current_user)
    if not request.targets:
        raise HTTPException(status_code=400, detail="targets must include at least one symbol")

    strategy_template_id = str(plan.get("strategy_template_id") or "")
    params_hash = str(plan.get("params_hash") or "")
    if not strategy_template_id:
        raise HTTPException(status_code=400, detail="plan is missing strategy_template_id")

    params_snapshot = plan.get("params_snapshot") or {}
    lot_size = _int_value(params_snapshot.get("lot_size"), 100) or 100
    initial_capital = _numeric(params_snapshot.get("initial_capital"), 1_000_000.0)

    now = datetime.utcnow()
    base_date = _business_today_yyyymmdd()

    inherited = _pp._latest_lineage_position_snapshot(trading_collections, plan, user_id, base_date)
    positions: Dict[str, Dict[str, Any]] = {
        str(row.get("symbol")): dict(row)
        for row in (inherited.get("positions") or [])
        if row.get("symbol")
    }
    cash = _numeric(inherited.get("cash"), initial_capital)

    target_symbols = {str(s) for s in request.targets.keys() if s}
    all_symbols = sorted(target_symbols | set(positions.keys()))
    price_map, _realtime_docs = _pp._load_fresh_market_prices(data_collections, all_symbols)

    def _price_for(symbol: str, pos: Dict[str, Any]) -> Optional[float]:
        return (
            _numeric(price_map.get(symbol))
            or _numeric(price_map.get(_symbol_key(symbol)))
            or _numeric(pos.get("last_price"))
            or _numeric(pos.get("mark_price"))
            or _numeric(pos.get("cost_basis"))
            or None
        )

    items: List[Dict[str, Any]] = []
    manual_exclude_symbols: List[str] = []
    estimated_buy = 0.0
    estimated_sell = 0.0
    rank = 0
    for symbol in sorted(target_symbols):
        pos = positions.get(symbol, {})
        current_shares = _int_value(pos.get("shares"))
        price = _price_for(symbol, pos)
        target_shares, blockers = _pp._normalize_live_target_shares(
            current_shares,
            request.targets[symbol],
            lot_size,
            symbol=symbol,
            price=price,
        )
        delta = target_shares - current_shares
        if delta == 0:
            continue
        rank += 1
        item_blockers = list(blockers)
        if not price:
            item_blockers.append("missing_price")
        action = "buy" if delta > 0 else "sell"
        if action == "buy":
            estimated_buy += abs(delta) * float(price or 0)
        else:
            estimated_sell += abs(delta) * float(price or 0)
        if target_shares < current_shares and request.exclude_after:
            manual_exclude_symbols.append(symbol)
        items.append(
            {
                "plan_id": None,  # filled after plan_id is built
                "strategy_template_id": strategy_template_id,
                "user_id": user_id,
                "params_hash": params_hash,
                "base_date": base_date,
                "execute_date": base_date,
                "rank": rank,
                "symbol": symbol,
                "name": pos.get("name", ""),
                "industry": pos.get("industry", ""),
                "action": action,
                "current_shares": current_shares,
                "target_shares": target_shares,
                "delta_shares": delta,
                "drift_delta_shares": delta,
                "estimated_price": price,
                "estimated_amount": abs(delta) * float(price or 0) if price else None,
                "blockers": item_blockers,
                "created_at": now,
            }
        )

    if not items:
        raise HTTPException(status_code=400, detail="targets produced no executable changes after lot-size normalization")

    is_liquidation = all(item["action"] == "sell" and item["target_shares"] == 0 for item in items)
    manual_action = "liquidate" if is_liquidation else "rebalance"

    stamp = now.strftime("%Y%m%d%H%M%S")
    new_plan_id = f"{strategy_template_id}_{(params_hash or 'na')[:8]}_{user_id}_paperrb_{stamp}{uuid.uuid4().hex[:6]}"
    for item in items:
        item["plan_id"] = new_plan_id

    plan_doc = {
        "plan_id": new_plan_id,
        "strategy_template_id": strategy_template_id,
        "user_id": user_id,
        "preset_id": plan.get("preset_id"),
        "params_hash": params_hash,
        "params_snapshot": params_snapshot,
        "params_override": plan.get("params_override") or {},
        "strategy_name": plan.get("strategy_name"),
        "base_date": base_date,
        "execute_date": base_date,
        "plan_type": "rebalance",
        "record_kind": RECORD_KIND_TRADE_PLAN,
        "origin": "manual",
        "manual_action": manual_action,
        "is_liquidation": is_liquidation,
        "is_rebalance_day": True,
        "monitor_can_trade": False,
        "trading_enabled": True,
        "executable": True,
        "non_executable_reason": None,
        "exclude_after": bool(request.exclude_after),
        "manual_exclude_symbols": sorted(set(manual_exclude_symbols)),
        "anchor_plan_id": plan_id,
        "created_at": now,
        "updated_at": now,
    }

    # Simulate fills immediately at the realtime price.
    work_positions = {sym: dict(pos) for sym, pos in positions.items()}
    work_cash = cash
    executions: List[Dict[str, Any]] = []
    partial = False
    # Sell first, then buy: credit same-day sell proceeds before buys so a
    # rank-leading buy is not starved of cash its funding sells would provide
    # (avoids spurious partial). sorted() is stable, preserving rank order
    # within each side.
    for item in sorted(items, key=lambda it: 0 if int(it.get("delta_shares", 0) or 0) < 0 else 1):
        symbol = item["symbol"]
        delta = int(item["delta_shares"])
        price = item.get("estimated_price")
        current = work_positions.get(
            symbol,
            {"symbol": symbol, "name": item.get("name", ""), "industry": item.get("industry", ""), "shares": 0, "cost_basis": 0.0},
        )
        if not price:
            executions.append(_execution_doc(plan_doc, item, "blocked", 0, 0.0, 0.0, "missing_price", now))
            partial = True
            continue
        if delta < 0:
            quantity = min(abs(delta), int(current.get("shares", 0) or 0))
            amount = quantity * price
            work_cash += amount
            current["shares"] = int(current.get("shares", 0) or 0) - quantity
            executions.append(_execution_doc(plan_doc, item, "sell", quantity, price, amount, None, now))
        else:
            quantity = delta
            amount = quantity * price
            if amount > work_cash:
                quantity = (int(work_cash // price) // max(1, lot_size)) * max(1, lot_size)
                amount = quantity * price
                partial = True
            if quantity <= 0:
                executions.append(_execution_doc(plan_doc, item, "blocked", 0, price, 0.0, "insufficient_cash", now))
                partial = True
                continue
            work_cash -= amount
            old_shares = int(current.get("shares", 0) or 0)
            old_cost = float(current.get("cost_basis", 0) or 0)
            current["cost_basis"] = ((old_shares * old_cost) + amount) / (old_shares + quantity) if old_shares + quantity else price
            current["shares"] = old_shares + quantity
            executions.append(_execution_doc(plan_doc, item, "buy", quantity, price, amount, None, now))
        if int(current.get("shares", 0) or 0) > 0:
            work_positions[symbol] = current
        elif symbol in work_positions:
            del work_positions[symbol]

    position_rows = []
    market_value = 0.0
    for symbol, pos in sorted(work_positions.items()):
        mark_price = _price_for(symbol, pos) or float(pos.get("cost_basis", 0) or 0)
        value = int(pos.get("shares", 0) or 0) * mark_price
        market_value += value
        position_rows.append({**pos, "mark_price": mark_price, "market_value": value})
    equity = work_cash + market_value
    status = "partially_executed" if partial else "executed_paper"

    snapshot = {
        "strategy_template_id": strategy_template_id,
        "user_id": user_id,
        "params_hash": params_hash,
        "as_of_date": base_date,
        "plan_id": new_plan_id,
        "inherited_from_plan_id": inherited.get("plan_id"),
        "inherited_from_as_of_date": inherited.get("as_of_date"),
        "cash": work_cash,
        "market_value": market_value,
        "equity": equity,
        "positions": position_rows,
        "updated_at": now,
    }

    changed_symbols = sorted({item["symbol"] for item in items})
    if request.dry_run:
        return {
            "success": True,
            "data": _jsonable(
                {
                    "dry_run": True,
                    "plan_id": new_plan_id,
                    "plan": plan_doc,
                    "items": items,
                    "executions": executions,
                    "position_snapshot": snapshot,
                    "manual_action": manual_action,
                    "changed_symbols": changed_symbols,
                    "status": status,
                }
            ),
        }

    plan_doc["status"] = status
    plan_doc["paper_executed_at"] = now
    trading_collections["portfolio_trade_plans"].insert_one(dict(plan_doc))
    trading_collections["portfolio_trade_plan_items"].insert_many([dict(item) for item in items])
    if executions:
        trading_collections["portfolio_paper_executions"].insert_many([dict(ex) for ex in executions])
    trading_collections["portfolio_paper_positions"].replace_one(
        {"plan_id": new_plan_id, "as_of_date": base_date, "user_id": user_id}, snapshot, upsert=True
    )
    trading_collections["portfolio_paper_equity"].replace_one(
        {"plan_id": new_plan_id, "date": base_date, "user_id": user_id},
        {
            "strategy_template_id": strategy_template_id,
            "user_id": user_id,
            "params_hash": params_hash,
            "date": base_date,
            "plan_id": new_plan_id,
            "inherited_from_plan_id": inherited.get("plan_id"),
            "inherited_from_as_of_date": inherited.get("as_of_date"),
            "equity": equity,
            "cash": work_cash,
            "market_value": market_value,
            "updated_at": now,
        },
        upsert=True,
    )
    lineage_effects = _pp._apply_manual_lineage_effects_native(
        trading_collections,
        plan_doc,
        final_positions=position_rows,
        acted_by=user_id,
    )

    return {
        "success": True,
        "data": _jsonable(
            {
                "dry_run": False,
                "plan_id": new_plan_id,
                "status": status,
                "manual_action": manual_action,
                "executions": executions,
                "position_snapshot": snapshot,
                "changed_symbols": changed_symbols,
                "lineage_effects": lineage_effects,
            }
        ),
    }


@router.post("/plans/{plan_id}/execute-paper")
async def execute_portfolio_plan_paper(
    plan_id: str,
    request: PaperExecuteRequest,
    current_user: dict = Depends(get_current_user),
):
    collections = _pp.get_trading_db_collections()
    data_collections = _pp.get_data_db()
    _pp._require_plan(collections, plan_id, current_user)
    result = _apply_paper_execution(collections, data_collections, plan_id, force=request.force, user_id=_pp._current_user_id(current_user))
    return {"success": True, "data": _jsonable(result)}


@router.get("/plans/{plan_id}/executions")
@_offload
def get_portfolio_plan_executions(plan_id: str, current_user: dict = Depends(get_current_user)):
    collections = _pp.get_trading_db_collections()
    _pp._require_plan(collections, plan_id, current_user)
    rows = list(collections["portfolio_paper_executions"].find({"plan_id": plan_id, "user_id": _pp._current_user_id(current_user)}).sort("created_at", 1))
    return {"success": True, "data": _jsonable(rows)}


@router.get("/plans/{plan_id}/equity")
@_offload
def get_portfolio_plan_equity(
    plan_id: str,
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
    current_user: dict = Depends(get_current_user),
):
    collections = _pp.get_trading_db_collections()
    _pp._require_plan(collections, plan_id, current_user)
    query: Dict[str, Any] = {"plan_id": plan_id, "user_id": _pp._current_user_id(current_user)}
    if start_date or end_date:
        date_query: Dict[str, Any] = {}
        if start_date:
            date_query["$gte"] = start_date.replace("-", "")
        if end_date:
            date_query["$lte"] = end_date.replace("-", "")
        query["date"] = date_query
    rows = list(collections["portfolio_paper_equity"].find(query).sort("date", 1))
    return {"success": True, "data": _jsonable(rows)}


def _apply_paper_execution(collections: Dict[str, Any], data_collections: Dict[str, Any], plan_id: str, force: bool = False, user_id: Optional[str] = None) -> Dict[str, Any]:
    plan = _pp._require_plan(collections, plan_id, {"id": user_id} if user_id else None)
    if plan.get("status") != "approved" and not force:
        raise HTTPException(status_code=400, detail="plan must be approved before paper execution")
    if user_id and _pp._plan_has_live_signals(_pp.get_db_collections(), plan_id, user_id):
        raise HTTPException(status_code=400, detail="plan already has live signals; cannot execute paper trading")
    execution_query = {"plan_id": plan_id}
    if user_id:
        execution_query["user_id"] = user_id
    if collections["portfolio_paper_executions"].find_one(execution_query) and not force:
        raise HTTPException(status_code=400, detail="plan already has paper executions")

    strategy_template_id = plan.get("strategy_template_id")
    user_id = plan.get("user_id")
    execute_date = str(plan.get("execute_date") or "").replace("-", "")
    if not execute_date:
        raise HTTPException(
            status_code=400,
            detail="缺少 execute_date，请等待自动执行或先补齐下一交易日 execute_date",
        )
    if not strategy_template_id:
        raise HTTPException(status_code=400, detail="plan is missing strategy_template_id")

    params_snapshot = plan.get("params_snapshot") or {}
    inherited_snapshot = _pp._latest_lineage_position_snapshot(collections, plan, user_id, execute_date)
    positions: Dict[str, Dict[str, Any]] = {
        row.get("symbol"): dict(row)
        for row in (inherited_snapshot.get("positions") or [])
        if row.get("symbol")
    }
    cash = float(inherited_snapshot.get("cash", params_snapshot.get("initial_capital", 1_000_000.0)) or 0)
    inherited_equity = float(inherited_snapshot.get("equity", params_snapshot.get("initial_capital", 1_000_000.0)) or 0)
    item_query = {"plan_id": plan_id}
    if user_id:
        item_query["user_id"] = user_id
    items = list(collections["portfolio_trade_plan_items"].find(item_query).sort("rank", 1))
    symbols = [item.get("symbol") for item in items if item.get("symbol")]
    execution_symbols = sorted(
        {
            item.get("symbol")
            for item in items
            if item.get("symbol") and int(item.get("delta_shares", 0) or 0) != 0
        }
    )
    open_prices = _pp._load_prices(data_collections["volume_price"], symbols, execute_date, "open")
    missing_open_prices = [symbol for symbol in execution_symbols if symbol not in open_prices]
    if missing_open_prices:
        raise HTTPException(
            status_code=409,
            detail=(
                f"开盘价未就绪：execute_date={execute_date}, "
                f"missing_count={len(missing_open_prices)}, "
                f"examples={', '.join(missing_open_prices[:10])}"
            ),
        )
    close_prices = _pp._load_prices(data_collections["volume_price"], symbols, execute_date, "close")
    now = datetime.utcnow()
    executions = []
    partial = False

    if force:
        collections["portfolio_paper_executions"].delete_many(execution_query)

    # Sell first, then buy: A-share sell proceeds are usable for same-day buys,
    # so process all sells before any buy to credit cash up front. A single rank
    # ordered pass would otherwise starve a buy that precedes its funding sells
    # and report a spurious partial. sorted() is stable, preserving rank order
    # within each side.
    for item in sorted(items, key=lambda it: 0 if int(it.get("delta_shares", 0) or 0) < 0 else 1):
        symbol = item.get("symbol")
        delta = int(item.get("delta_shares", 0) or 0)
        if not symbol or delta == 0:
            continue
        price = open_prices.get(symbol)
        if not price:
            raise HTTPException(status_code=409, detail=f"开盘价未就绪：{symbol} {execute_date}")
        current = positions.get(symbol, {"symbol": symbol, "name": item.get("name", ""), "industry": item.get("industry", ""), "shares": 0, "cost_basis": 0.0})
        if delta < 0:
            quantity = min(abs(delta), int(current.get("shares", 0) or 0))
            amount = quantity * price
            cash += amount
            current["shares"] = int(current.get("shares", 0) or 0) - quantity
            executions.append(_execution_doc(plan, item, "sell", quantity, price, amount, None, now))
        else:
            quantity = delta
            amount = quantity * price
            if amount > cash:
                lot_size = int(params_snapshot.get("lot_size", 100))
                quantity = (int(cash // price) // max(1, lot_size)) * max(1, lot_size)
                amount = quantity * price
                partial = True
            if quantity <= 0:
                executions.append(_execution_doc(plan, item, "blocked", 0, price, 0.0, "insufficient_cash", now))
                partial = True
                continue
            cash -= amount
            old_shares = int(current.get("shares", 0) or 0)
            old_cost = float(current.get("cost_basis", 0) or 0)
            current["cost_basis"] = ((old_shares * old_cost) + amount) / (old_shares + quantity) if old_shares + quantity else price
            current["shares"] = old_shares + quantity
            executions.append(_execution_doc(plan, item, "buy", quantity, price, amount, None, now))
        if int(current.get("shares", 0) or 0) > 0:
            positions[symbol] = current
        elif symbol in positions:
            del positions[symbol]

    position_rows = []
    market_value = 0.0
    for symbol, pos in sorted(positions.items()):
        mark_price = close_prices.get(symbol) or open_prices.get(symbol) or float(pos.get("cost_basis", 0) or 0)
        value = int(pos.get("shares", 0) or 0) * mark_price
        market_value += value
        position_rows.append({**pos, "mark_price": mark_price, "market_value": value})
    equity = cash + market_value
    status = "partially_executed" if partial else "executed_paper"

    if executions:
        collections["portfolio_paper_executions"].insert_many(executions)
    snapshot = {
        "strategy_template_id": strategy_template_id,
        "user_id": user_id,
        "params_hash": plan.get("params_hash"),
        "as_of_date": execute_date,
        "plan_id": plan_id,
        "inherited_from_plan_id": inherited_snapshot.get("plan_id"),
        "inherited_from_as_of_date": inherited_snapshot.get("as_of_date"),
        "cash": cash,
        "market_value": market_value,
        "equity": equity,
        "positions": position_rows,
        "updated_at": now,
    }
    position_query = {"plan_id": plan_id, "as_of_date": execute_date}
    equity_query = {"plan_id": plan_id, "date": execute_date}
    if user_id:
        position_query["user_id"] = user_id
        equity_query["user_id"] = user_id
    collections["portfolio_paper_positions"].replace_one(position_query, snapshot, upsert=True)
    collections["portfolio_paper_equity"].replace_one(
        equity_query,
        {
            "strategy_template_id": strategy_template_id,
            "user_id": user_id,
            "params_hash": plan.get("params_hash"),
            "date": execute_date,
            "plan_id": plan_id,
            "inherited_from_plan_id": inherited_snapshot.get("plan_id"),
            "inherited_from_as_of_date": inherited_snapshot.get("as_of_date"),
            "equity": equity,
            "cash": cash,
            "market_value": market_value,
            "updated_at": now,
        },
        upsert=True,
    )
    plan_update_query = {"plan_id": plan_id}
    if user_id:
        plan_update_query["user_id"] = user_id
    collections["portfolio_trade_plans"].update_one(plan_update_query, {"$set": {"status": status, "paper_executed_at": now}})
    lineage_effects = _pp._apply_manual_lineage_effects_native(
        collections,
        plan,
        final_positions=position_rows,
        acted_by=user_id,
    )
    return {
        "plan_id": plan_id,
        "status": status,
        "executions": executions,
        "position_snapshot": snapshot,
        "inherited_from_plan_id": inherited_snapshot.get("plan_id"),
        "inherited_from_as_of_date": inherited_snapshot.get("as_of_date"),
        "inherited_equity": inherited_equity,
        "lineage_effects": lineage_effects,
    }


def _paper_execution_status(collections: Dict[str, Any], data_collections: Dict[str, Any], plan: Dict[str, Any], items: List[Dict[str, Any]]) -> Dict[str, Any]:
    plan_id = str(plan.get("plan_id") or "")
    execution_query = {"plan_id": plan_id}
    if plan.get("user_id"):
        execution_query["user_id"] = plan.get("user_id")
    execute_date = plan.get("execute_date")
    effective_execute_date = execute_date or None
    plan_status = str(plan.get("status") or "")

    missing_symbols: List[str] = []
    missing_execute_date = plan_status == "approved" and not bool(execute_date)
    if plan_status == "approved" and execute_date:
        execution_symbols = sorted(
            {
                item.get("symbol")
                for item in items
                if item.get("symbol") and int(item.get("delta_shares", 0) or 0) != 0
            }
        )
        open_prices = _pp._load_prices(data_collections["volume_price"], execution_symbols, effective_execute_date, "open") if execution_symbols else {}
        missing_symbols = [symbol for symbol in execution_symbols if symbol not in open_prices]

    should_load_executions = plan_status in {"approved", "executed_paper", "partially_executed"} or bool(plan.get("paper_executed_at"))
    latest_execution = None
    execution_count = 0
    if should_load_executions:
        latest_execution = collections["portfolio_paper_executions"].find_one(
            execution_query,
            sort=[("created_at", -1)],
        )
        execution_count = collections["portfolio_paper_executions"].count_documents(execution_query)

    if missing_execute_date:
        backend_status = "missing_execute_date"
    elif plan_status == "approved":
        backend_status = "waiting_for_open_prices" if missing_symbols else "ready_for_background_execution"
    elif plan_status in {"executed_paper", "partially_executed"}:
        backend_status = plan_status
    else:
        backend_status = "not_approved"

    return {
        "execute_date": execute_date,
        "effective_execute_date": effective_execute_date,
        "uses_runtime_execute_date": False,
        "open_price_ready": bool(execute_date) and not missing_symbols,
        "missing_execute_date": missing_execute_date,
        "missing_open_price_count": len(missing_symbols),
        "missing_open_price_examples": missing_symbols[:10],
        "backend_execution_status": backend_status,
        "execution_count": execution_count,
        "latest_execution_result": latest_execution,
    }


def _execution_doc(plan: Dict[str, Any], item: Dict[str, Any], action: str, quantity: int, price: Optional[float], amount: float, blocker: Optional[str], now: datetime) -> Dict[str, Any]:
    return {
        "execution_id": str(uuid.uuid4()),
        "plan_id": plan["plan_id"],
        "strategy_template_id": plan["strategy_template_id"],
        "user_id": plan.get("user_id"),
        "symbol": item.get("symbol"),
        "name": item.get("name", ""),
        "action": action,
        "quantity": int(quantity),
        "price": price,
        "amount": amount,
        "blocker": blocker,
        "execute_date": plan.get("execute_date"),
        "created_at": now,
    }
