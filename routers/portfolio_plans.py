"""Portfolio trade plan and paper-trading API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
import uuid

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from auth import get_current_user
from db import get_data_db, get_trading_db_collections


router = APIRouter(prefix="/portfolio-plans", tags=["portfolio-plans"])


class ReviewRequest(BaseModel):
    comment: str = Field(default="")


class PaperExecuteRequest(BaseModel):
    force: bool = Field(default=False)


def _jsonable(value: Any) -> Any:
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


def _require_plan(collections: Dict[str, Any], plan_id: str) -> Dict[str, Any]:
    plan = collections["portfolio_trade_plans"].find_one({"plan_id": plan_id})
    if not plan:
        raise HTTPException(status_code=404, detail="plan not found")
    return plan


def _reviewer(current_user: Dict[str, Any]) -> str:
    return str(current_user.get("username") or current_user.get("email") or current_user.get("id") or "unknown")


@router.get("/strategies")
async def list_portfolio_strategies(
    status: Optional[str] = Query(default=None),
    current_user: dict = Depends(get_current_user),
):
    collections = get_trading_db_collections()
    query: Dict[str, Any] = {}
    if status:
        query["status"] = status
    rows = list(collections["portfolio_strategy_configs"].find(query).sort("strategy_id", 1))
    return {"success": True, "data": [_jsonable(row) for row in rows]}


@router.get("/plans")
async def list_portfolio_plans(
    strategy_id: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    plan_type: Optional[str] = Query(default=None),
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    current_user: dict = Depends(get_current_user),
):
    collections = get_trading_db_collections()
    query: Dict[str, Any] = {}
    if strategy_id:
        query["strategy_id"] = strategy_id
    if status:
        query["status"] = status
    if plan_type:
        query["plan_type"] = plan_type
    if start_date or end_date:
        date_query: Dict[str, Any] = {}
        if start_date:
            date_query["$gte"] = start_date.replace("-", "")
        if end_date:
            date_query["$lte"] = end_date.replace("-", "")
        query["base_date"] = date_query
    rows = list(collections["portfolio_trade_plans"].find(query).sort("base_date", -1).limit(limit))
    return {"success": True, "data": [_jsonable(row) for row in rows]}


@router.get("/plans/{plan_id}")
async def get_portfolio_plan(plan_id: str, current_user: dict = Depends(get_current_user)):
    collections = get_trading_db_collections()
    plan = _require_plan(collections, plan_id)
    items = list(collections["portfolio_trade_plan_items"].find({"plan_id": plan_id}).sort("rank", 1))
    reviews = list(collections["portfolio_plan_reviews"].find({"plan_id": plan_id}).sort("created_at", -1))
    return {"success": True, "data": {"plan": _jsonable(plan), "items": _jsonable(items), "reviews": _jsonable(reviews)}}


@router.get("/plans/{plan_id}/items")
async def get_portfolio_plan_items(plan_id: str, current_user: dict = Depends(get_current_user)):
    collections = get_trading_db_collections()
    _require_plan(collections, plan_id)
    items = list(collections["portfolio_trade_plan_items"].find({"plan_id": plan_id}).sort("rank", 1))
    return {"success": True, "data": _jsonable(items)}


@router.post("/plans/{plan_id}/approve")
async def approve_portfolio_plan(plan_id: str, request: ReviewRequest, current_user: dict = Depends(get_current_user)):
    return _review_plan(plan_id, "approved", request.comment, current_user)


@router.post("/plans/{plan_id}/reject")
async def reject_portfolio_plan(plan_id: str, request: ReviewRequest, current_user: dict = Depends(get_current_user)):
    return _review_plan(plan_id, "rejected", request.comment, current_user)


def _review_plan(plan_id: str, decision: str, comment: str, current_user: Dict[str, Any]):
    collections = get_trading_db_collections()
    plan = _require_plan(collections, plan_id)
    if plan.get("status") not in {"needs_review", "approved", "rejected"}:
        raise HTTPException(status_code=400, detail=f"plan is not reviewable in status {plan.get('status')}")
    now = datetime.utcnow()
    review = {
        "plan_id": plan_id,
        "strategy_id": plan.get("strategy_id"),
        "decision": decision,
        "reviewer": _reviewer(current_user),
        "comment": comment,
        "created_at": now,
    }
    collections["portfolio_plan_reviews"].insert_one(review)
    collections["portfolio_trade_plans"].update_one(
        {"plan_id": plan_id},
        {"$set": {"status": decision, "reviewer": review["reviewer"], "reviewed_at": now}},
    )
    return {"success": True, "data": _jsonable(review)}


@router.get("/plans/{plan_id}/executions")
async def get_portfolio_plan_executions(plan_id: str, current_user: dict = Depends(get_current_user)):
    collections = get_trading_db_collections()
    _require_plan(collections, plan_id)
    rows = list(collections["portfolio_paper_executions"].find({"plan_id": plan_id}).sort("created_at", 1))
    return {"success": True, "data": _jsonable(rows)}


@router.post("/plans/{plan_id}/execute-paper")
async def execute_portfolio_plan_paper(
    plan_id: str,
    request: PaperExecuteRequest,
    current_user: dict = Depends(get_current_user),
):
    collections = get_trading_db_collections()
    data_collections = get_data_db()
    result = _apply_paper_execution(collections, data_collections, plan_id, force=request.force)
    return {"success": True, "data": _jsonable(result)}


@router.get("/strategies/{strategy_id}/equity")
async def get_portfolio_strategy_equity(
    strategy_id: str,
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
    current_user: dict = Depends(get_current_user),
):
    collections = get_trading_db_collections()
    query: Dict[str, Any] = {"strategy_id": strategy_id}
    if start_date or end_date:
        date_query: Dict[str, Any] = {}
        if start_date:
            date_query["$gte"] = start_date.replace("-", "")
        if end_date:
            date_query["$lte"] = end_date.replace("-", "")
        query["date"] = date_query
    rows = list(collections["portfolio_paper_equity"].find(query).sort("date", 1))
    return {"success": True, "data": _jsonable(rows)}


def _apply_paper_execution(collections: Dict[str, Any], data_collections: Dict[str, Any], plan_id: str, force: bool = False) -> Dict[str, Any]:
    plan = _require_plan(collections, plan_id)
    if plan.get("status") != "approved" and not force:
        raise HTTPException(status_code=400, detail="plan must be approved before paper execution")
    if collections["portfolio_paper_executions"].find_one({"plan_id": plan_id}) and not force:
        raise HTTPException(status_code=400, detail="plan already has paper executions")

    strategy_id = plan.get("strategy_id")
    execute_date = plan.get("execute_date")
    if not strategy_id or not execute_date:
        raise HTTPException(status_code=400, detail="plan is missing strategy_id or execute_date")

    previous = collections["portfolio_paper_positions"].find_one(
        {"strategy_id": strategy_id, "as_of_date": {"$lte": plan.get("base_date")}},
        sort=[("as_of_date", -1)],
    ) or {}
    positions = {row.get("symbol"): dict(row) for row in previous.get("positions", []) if row.get("symbol")}
    cash = float(previous.get("cash", plan.get("config_snapshot", {}).get("initial_capital", 1_000_000.0)))
    items = list(collections["portfolio_trade_plan_items"].find({"plan_id": plan_id}).sort("rank", 1))
    symbols = [item.get("symbol") for item in items if item.get("symbol")]
    open_prices = _load_prices(data_collections["volume_price"], symbols, execute_date, "open")
    close_prices = _load_prices(data_collections["volume_price"], symbols, execute_date, "close")
    now = datetime.utcnow()
    executions = []
    partial = False

    if force:
        collections["portfolio_paper_executions"].delete_many({"plan_id": plan_id})

    for item in items:
        symbol = item.get("symbol")
        delta = int(item.get("delta_shares", 0) or 0)
        if not symbol or delta == 0:
            continue
        price = open_prices.get(symbol)
        if not price:
            partial = True
            executions.append(_execution_doc(plan, item, "blocked", 0, None, 0.0, "missing_open_price", now))
            continue
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
                lot_size = int(plan.get("config_snapshot", {}).get("lot_size", 100))
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
        "strategy_id": strategy_id,
        "as_of_date": execute_date,
        "plan_id": plan_id,
        "cash": cash,
        "market_value": market_value,
        "equity": equity,
        "positions": position_rows,
        "updated_at": now,
    }
    collections["portfolio_paper_positions"].replace_one({"strategy_id": strategy_id, "as_of_date": execute_date}, snapshot, upsert=True)
    collections["portfolio_paper_equity"].replace_one(
        {"strategy_id": strategy_id, "date": execute_date},
        {"strategy_id": strategy_id, "date": execute_date, "plan_id": plan_id, "equity": equity, "cash": cash, "market_value": market_value, "updated_at": now},
        upsert=True,
    )
    collections["portfolio_trade_plans"].update_one({"plan_id": plan_id}, {"$set": {"status": status, "paper_executed_at": now}})
    return {"plan_id": plan_id, "status": status, "executions": executions, "position_snapshot": snapshot}


def _load_prices(volume_price, symbols: List[str], trade_date: str, field: str) -> Dict[str, float]:
    rows = volume_price.find({"symbol": {"$in": symbols}, "trade_date": trade_date}, {"_id": 0, "symbol": 1, field: 1, "open": 1, "close": 1})
    prices = {}
    for row in rows:
        value = row.get(field) or row.get("close") or row.get("open")
        try:
            price = float(value)
        except (TypeError, ValueError):
            continue
        if price > 0:
            prices[row["symbol"]] = price
    return prices


def _execution_doc(plan: Dict[str, Any], item: Dict[str, Any], action: str, quantity: int, price: Optional[float], amount: float, blocker: Optional[str], now: datetime) -> Dict[str, Any]:
    return {
        "execution_id": str(uuid.uuid4()),
        "plan_id": plan["plan_id"],
        "strategy_id": plan["strategy_id"],
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
