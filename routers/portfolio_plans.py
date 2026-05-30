"""Portfolio trade plan and paper-trading API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
import uuid

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db as finance_db
from db import get_data_db, get_db_collections, get_trading_db_collections
from utils.time_display import add_display_time_fields
from services.live_trade_signals import (
    active_signal_query_for_order_ids,
    build_portfolio_plan_live_signal,
    summarize_signal_statuses,
)


router = APIRouter(prefix="/portfolio-plans", tags=["portfolio-plans"])


class ReviewRequest(BaseModel):
    comment: str = Field(default="")


class PaperExecuteRequest(BaseModel):
    force: bool = Field(default=False)


class PublishLiveSignalsRequest(BaseModel):
    securities_account_id: str
    dry_run: bool = Field(default=True)


class GeneratePlanRequest(BaseModel):
    strategy_id: str
    base_date: str
    mode: str = Field(default="auto", pattern="^(auto|monitor|rebalance)$")
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


def _current_user_id(current_user: Dict[str, Any]) -> str:
    user_id = current_user.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="current user id is missing")
    return str(user_id)


def _require_plan(collections: Dict[str, Any], plan_id: str, current_user: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    query: Dict[str, Any] = {"plan_id": plan_id}
    if current_user is not None:
        query["user_id"] = _current_user_id(current_user)
    plan = collections["portfolio_trade_plans"].find_one(query)
    if not plan:
        raise HTTPException(status_code=404, detail="plan not found")
    return plan


def _plan_has_live_signals(finance_collections: Dict[str, Any], plan_id: str, user_id: str) -> bool:
    return bool(
        finance_collections["trade_signals"].find_one(
            {
                "user_id": user_id,
                "plan_id": plan_id,
                "mode": "live",
                "source": "portfolio_plan",
            }
        )
    )


def _require_strategy(collections: Dict[str, Any], strategy_id: str) -> Dict[str, Any]:
    strategy = collections["portfolio_strategy_configs"].find_one({"strategy_id": strategy_id})
    if not strategy:
        raise HTTPException(status_code=404, detail="strategy not found")
    return strategy


def _reviewer(current_user: Dict[str, Any]) -> str:
    return str(current_user.get("username") or current_user.get("email") or current_user.get("id") or "unknown")


def _normalize_yyyymmdd(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    raw = str(value).replace("-", "").strip()
    if len(raw) >= 8 and raw[:8].isdigit():
        return raw[:8]
    return None


def _latest_available_score_watermark(stock_scores, universe_index: str) -> Dict[str, Any]:
    query: Dict[str, Any] = {"index_codes": universe_index}
    row = stock_scores.find_one(query, sort=[("score_date", -1)], projection={"_id": 0, "score_date": 1})
    source_scope = "index_codes"
    if not row:
        query = {}
        row = stock_scores.find_one(query, sort=[("score_date", -1)], projection={"_id": 0, "score_date": 1})
        source_scope = "global"
    score_date = str(row.get("score_date")) if row and row.get("score_date") else None
    count = stock_scores.count_documents({**query, "score_date": score_date}) if score_date else 0
    return {
        "latest_available_score_date": score_date,
        "latest_available_score_count": count,
        "latest_available_score_source": "stock_scores",
        "latest_available_score_scope": source_scope,
    }


def _require_securities_account(collections: Dict[str, Any], securities_account_id: str, user_id: str) -> Dict[str, Any]:
    query_ids: List[Any] = [securities_account_id]
    try:
        query_ids.insert(0, ObjectId(securities_account_id))
    except Exception:
        pass
    account = None
    for query_id in query_ids:
        account = collections["securities_accounts"].find_one({"_id": query_id, "user_id": user_id})
        if account:
            break
    if not account:
        raise HTTPException(status_code=404, detail="securities account not found")
    account["_id"] = str(account.get("_id"))
    return account


def _account_live_enabled(account: Dict[str, Any]) -> bool:
    return bool(
        account.get("live_trading_enabled")
        or account.get("live_enabled")
        or account.get("allow_live_trading")
    )


def _numeric(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _latest_account_snapshot(finance_collections: Dict[str, Any], account: Dict[str, Any], user_id: str) -> Dict[str, Any]:
    return finance_collections["trader_accounts"].find_one(
        {"user_id": user_id, "securities_account_id": str(account.get("_id"))},
        sort=[("synced_at", -1)],
    ) or {}


def _position_snapshot(finance_collections: Dict[str, Any], signal: Dict[str, Any], user_id: str) -> Dict[str, Any]:
    return finance_collections["trader_positions"].find_one(
        {
            "user_id": user_id,
            "securities_account_id": signal.get("securities_account_id"),
            "symbol": signal.get("symbol"),
        },
        sort=[("synced_at", -1)],
    ) or {}


def _daily_live_signal_amount(finance_collections: Dict[str, Any], account: Dict[str, Any], user_id: str) -> float:
    today_prefix = datetime.utcnow().date().isoformat()
    rows = finance_collections["trade_signals"].find(
        {"user_id": user_id, "securities_account_id": str(account.get("_id")), "mode": "live"}
    )
    total = 0.0
    for row in rows:
        created_at = row.get("created_at")
        if isinstance(created_at, datetime):
            is_today = created_at.date() == datetime.utcnow().date()
        else:
            is_today = str(created_at or "").startswith(today_prefix)
        if is_today and str(row.get("status") or "") not in {"cancelled", "canceled", "rejected", "failed"}:
            total += abs(int(row.get("size", 0) or 0)) * _numeric(row.get("price"))
    return total


def _load_stock_guard_docs(data_collections: Dict[str, Any], symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    if not symbols:
        return {}
    stock_info_rows = data_collections["stock_info"].find({"symbol": {"$in": symbols}})
    realtime_rows = data_collections["volume_price_realtime_latest"].find({"symbol": {"$in": symbols}})
    docs = {row.get("symbol"): dict(row) for row in stock_info_rows if row.get("symbol")}
    for row in realtime_rows:
        symbol = row.get("symbol")
        if symbol:
            docs.setdefault(symbol, {}).update(row)
    return docs


def _buy_blockers_from_market_doc(signal: Dict[str, Any], market_doc: Dict[str, Any]) -> List[str]:
    blockers: List[str] = []
    price = _numeric(signal.get("price"))
    if price <= 0:
        blockers.append("missing_price")
    name = str(market_doc.get("name") or market_doc.get("stock_name") or "")
    if market_doc.get("is_st") or "ST" in name.upper():
        blockers.append("st_stock")
    if market_doc.get("is_suspended") or market_doc.get("suspended") or market_doc.get("paused"):
        blockers.append("suspended")
    limit_up = _numeric(market_doc.get("limit_up") or market_doc.get("up_limit"))
    last_price = _numeric(market_doc.get("close") or market_doc.get("last_price"))
    if limit_up > 0 and last_price >= limit_up:
        blockers.append("limit_up")
    return blockers


def _build_live_publish_risk_report(
    *,
    finance_collections: Dict[str, Any],
    data_collections: Dict[str, Any],
    account: Dict[str, Any],
    signals: List[Dict[str, Any]],
    user_id: str,
) -> Dict[str, Any]:
    account_snapshot = _latest_account_snapshot(finance_collections, account, user_id)
    available_cash = _numeric(account_snapshot.get("available_cash") or account_snapshot.get("cash"))
    total_assets = _numeric(account_snapshot.get("total_assets") or account_snapshot.get("total_asset"))
    risk_limits = account.get("risk_limits") or {}
    max_order_amount = _numeric(risk_limits.get("max_order_amount") or account.get("max_order_amount"), float("inf"))
    max_daily_amount = _numeric(risk_limits.get("max_daily_amount") or account.get("max_daily_amount"), float("inf"))
    max_position_pct = _numeric(risk_limits.get("max_position_pct") or account.get("max_position_pct"), 0.0)
    daily_used_amount = _daily_live_signal_amount(finance_collections, account, user_id)
    market_docs = _load_stock_guard_docs(data_collections, [signal["symbol"] for signal in signals])

    cash_reserved = 0.0
    day_reserved = 0.0
    items = []
    for signal in signals:
        amount = abs(int(signal.get("size", 0) or 0)) * _numeric(signal.get("price"))
        blockers: List[str] = []
        if not _account_live_enabled(account):
            blockers.append("live_trading_disabled")
        if not account.get("broker") or not account.get("account_id"):
            blockers.append("account_binding_missing")
        if max_order_amount != float("inf") and amount > max_order_amount:
            blockers.append("max_order_amount_exceeded")
        if max_daily_amount != float("inf") and daily_used_amount + day_reserved + amount > max_daily_amount:
            blockers.append("max_daily_amount_exceeded")
        if signal.get("action") == "buy":
            blockers.extend(_buy_blockers_from_market_doc(signal, market_docs.get(signal["symbol"], {})))
            if available_cash and cash_reserved + amount > available_cash:
                blockers.append("insufficient_cash")
            if total_assets > 0 and max_position_pct > 0:
                position = _position_snapshot(finance_collections, signal, user_id)
                current_market_value = _numeric(position.get("market_value"))
                if (current_market_value + amount) / total_assets * 100 > max_position_pct:
                    blockers.append("max_position_pct_exceeded")
            cash_reserved += amount
        elif signal.get("action") == "sell":
            position = _position_snapshot(finance_collections, signal, user_id)
            available_qty = int(position.get("available_qty") or position.get("can_use_volume") or position.get("qty") or 0)
            if int(signal.get("size", 0) or 0) > available_qty:
                blockers.append("insufficient_available_position")
        day_reserved += amount
        items.append(
            {
                "order_id": signal.get("order_id"),
                "symbol": signal.get("symbol"),
                "action": signal.get("action"),
                "size": signal.get("size"),
                "price": signal.get("price"),
                "estimated_amount": amount,
                "blockers": sorted(set(blockers)),
            }
        )

    return {
        "account": {
            "securities_account_id": str(account.get("_id")),
            "broker": account.get("broker"),
            "account_id": account.get("account_id"),
            "live_trading_enabled": _account_live_enabled(account),
            "available_cash": available_cash,
        },
        "limits": {
            "max_order_amount": None if max_order_amount == float("inf") else max_order_amount,
            "max_daily_amount": None if max_daily_amount == float("inf") else max_daily_amount,
            "max_position_pct": max_position_pct or None,
            "daily_used_amount": daily_used_amount,
            "new_signal_amount": day_reserved,
        },
        "items": items,
        "blocked_count": sum(1 for item in items if item["blockers"]),
    }


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
    query: Dict[str, Any] = {"user_id": _current_user_id(current_user)}
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


@router.post("/plans/generate")
async def generate_portfolio_plan(request: GeneratePlanRequest, current_user: dict = Depends(get_current_user)):
    collections = get_trading_db_collections()
    user_id = _current_user_id(current_user)
    strategy = _require_strategy(collections, request.strategy_id)
    if strategy.get("status") == "disabled":
        raise HTTPException(status_code=400, detail="strategy is disabled")
    now = datetime.utcnow()
    task = {
        "task_id": str(uuid.uuid4()),
        "task_type": "generate_portfolio_plan",
        "user_id": user_id,
        "strategy_id": request.strategy_id,
        "base_date": request.base_date,
        "mode": request.mode,
        "force": request.force,
        "status": "pending",
        "attempts": 0,
        "plan_id": None,
        "error_message": None,
        "created_at": now,
        "updated_at": now,
    }
    collections["portfolio_plan_generation_tasks"].insert_one(task)
    return {"success": True, "data": _jsonable(task)}


@router.get("/plans/generation-tasks")
async def list_portfolio_plan_generation_tasks(
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
):
    collections = get_trading_db_collections()
    query: Dict[str, Any] = {"user_id": _current_user_id(current_user)}
    if status:
        query["status"] = status
    rows = list(collections["portfolio_plan_generation_tasks"].find(query).sort("created_at", -1).limit(limit))
    return {"success": True, "data": _jsonable(rows)}


@router.get("/plans/generation-tasks/{task_id}")
async def get_portfolio_plan_generation_task(task_id: str, current_user: dict = Depends(get_current_user)):
    collections = get_trading_db_collections()
    task = collections["portfolio_plan_generation_tasks"].find_one(
        {"task_id": task_id, "user_id": _current_user_id(current_user)}
    )
    if not task:
        raise HTTPException(status_code=404, detail="generation task not found")
    return {"success": True, "data": _jsonable(task)}


@router.get("/workers/status")
async def list_portfolio_worker_status(
    worker_type: Optional[str] = Query(default="portfolio_plan_generator"),
    limit: int = Query(default=20, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
):
    _current_user_id(current_user)
    collections = get_trading_db_collections()
    query: Dict[str, Any] = {}
    if worker_type:
        query["worker_type"] = worker_type
    rows = list(collections["portfolio_worker_status"].find(query).sort("last_seen_at", -1).limit(limit))
    return {"success": True, "data": _jsonable(rows)}


@router.get("/data-watermarks/plan-generation")
async def get_portfolio_plan_generation_watermark(
    strategy_id: Optional[str] = Query(default=None),
    base_date: Optional[str] = Query(default=None),
    current_user: dict = Depends(get_current_user),
):
    _current_user_id(current_user)
    collections = get_trading_db_collections()
    strategy = _require_strategy(collections, strategy_id) if strategy_id else {}
    universe_index = str(strategy.get("universe_index") or "csi1000")
    normalized_base_date = _normalize_yyyymmdd(base_date)

    scoring_runs = finance_db["scoring_runs"]
    universe_query = {"universe_index": universe_index}
    target_query = dict(universe_query)
    if normalized_base_date:
        target_query["score_date"] = normalized_base_date
    target_run = scoring_runs.find_one(target_query, sort=[("updated_at", -1)]) if normalized_base_date else None
    latest_run = scoring_runs.find_one(universe_query, sort=[("score_date", -1), ("updated_at", -1)])
    latest_completed_run = scoring_runs.find_one(
        {**universe_query, "status": "completed"},
        sort=[("score_date", -1), ("updated_at", -1)],
    )
    available_score_watermark = _latest_available_score_watermark(finance_db["stock_scores"], universe_index)
    return {
        "success": True,
        "data": _jsonable(
            {
                "strategy_id": strategy_id,
                "base_date": normalized_base_date,
                "universe_index": universe_index,
                "target_scoring_run": target_run,
                "target_status": target_run.get("status") if target_run else None,
                "target_is_running": bool(target_run and target_run.get("status") == "running"),
                "latest_scoring_run": latest_run,
                "latest_completed_scoring_run": latest_completed_run,
                **available_score_watermark,
            }
        ),
    }


@router.get("/plans/{plan_id}")
async def get_portfolio_plan(plan_id: str, current_user: dict = Depends(get_current_user)):
    collections = get_trading_db_collections()
    data_collections = get_data_db()
    plan = _require_plan(collections, plan_id, current_user)
    owner_query = {"plan_id": plan_id, "user_id": _current_user_id(current_user)}
    items = list(collections["portfolio_trade_plan_items"].find(owner_query).sort("rank", 1))
    reviews = list(collections["portfolio_plan_reviews"].find(owner_query).sort("created_at", -1))
    execution_status = _paper_execution_status(collections, data_collections, plan, items)
    return {
        "success": True,
        "data": {
            "plan": _jsonable(plan),
            "items": _jsonable(items),
            "reviews": _jsonable(reviews),
            "execution_status": _jsonable(execution_status),
        },
    }


@router.get("/plans/{plan_id}/items")
async def get_portfolio_plan_items(plan_id: str, current_user: dict = Depends(get_current_user)):
    collections = get_trading_db_collections()
    _require_plan(collections, plan_id, current_user)
    items = list(collections["portfolio_trade_plan_items"].find({"plan_id": plan_id, "user_id": _current_user_id(current_user)}).sort("rank", 1))
    return {"success": True, "data": _jsonable(items)}


@router.post("/plans/{plan_id}/approve")
async def approve_portfolio_plan(plan_id: str, request: ReviewRequest, current_user: dict = Depends(get_current_user)):
    return _review_plan(plan_id, "approved", request.comment, current_user)


@router.post("/plans/{plan_id}/reject")
async def reject_portfolio_plan(plan_id: str, request: ReviewRequest, current_user: dict = Depends(get_current_user)):
    return _review_plan(plan_id, "rejected", request.comment, current_user)


def _review_plan(plan_id: str, decision: str, comment: str, current_user: Dict[str, Any]):
    collections = get_trading_db_collections()
    plan = _require_plan(collections, plan_id, current_user)
    if plan.get("status") not in {"needs_review", "approved", "rejected"}:
        raise HTTPException(status_code=400, detail=f"plan is not reviewable in status {plan.get('status')}")
    now = datetime.utcnow()
    user_id = _current_user_id(current_user)
    review = {
        "plan_id": plan_id,
        "strategy_id": plan.get("strategy_id"),
        "user_id": user_id,
        "decision": decision,
        "reviewer": _reviewer(current_user),
        "comment": comment,
        "created_at": now,
    }
    collections["portfolio_plan_reviews"].insert_one(review)
    collections["portfolio_trade_plans"].update_one(
        {"plan_id": plan_id, "user_id": user_id},
        {"$set": {"status": decision, "reviewer": review["reviewer"], "reviewed_at": now}},
    )
    return {"success": True, "data": _jsonable(review)}


@router.get("/plans/{plan_id}/executions")
async def get_portfolio_plan_executions(plan_id: str, current_user: dict = Depends(get_current_user)):
    collections = get_trading_db_collections()
    _require_plan(collections, plan_id, current_user)
    rows = list(collections["portfolio_paper_executions"].find({"plan_id": plan_id, "user_id": _current_user_id(current_user)}).sort("created_at", 1))
    return {"success": True, "data": _jsonable(rows)}


@router.get("/plans/{plan_id}/live-executions")
async def get_portfolio_plan_live_executions(plan_id: str, current_user: dict = Depends(get_current_user)):
    trading_collections = get_trading_db_collections()
    finance_collections = get_db_collections()
    _require_plan(trading_collections, plan_id, current_user)
    user_id = _current_user_id(current_user)

    signal_query = {
        "user_id": user_id,
        "plan_id": plan_id,
        "mode": "live",
        "source": "portfolio_plan",
    }
    signals = [add_display_time_fields(row) for row in finance_collections["trade_signals"].find(signal_query).sort("created_at", 1)]
    order_ids = [row.get("order_id") for row in signals if row.get("order_id")]
    execution_query = {"user_id": user_id, "order_id": {"$in": order_ids}} if order_ids else None
    executions = (
        [add_display_time_fields(row) for row in finance_collections["trade_executions"].find(execution_query).sort("timestamp", -1)]
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


@router.post("/plans/{plan_id}/execute-paper")
async def execute_portfolio_plan_paper(
    plan_id: str,
    request: PaperExecuteRequest,
    current_user: dict = Depends(get_current_user),
):
    collections = get_trading_db_collections()
    data_collections = get_data_db()
    _require_plan(collections, plan_id, current_user)
    result = _apply_paper_execution(collections, data_collections, plan_id, force=request.force, user_id=_current_user_id(current_user))
    return {"success": True, "data": _jsonable(result)}


@router.post("/plans/{plan_id}/publish-live-signals")
async def publish_portfolio_plan_live_signals(
    plan_id: str,
    request: PublishLiveSignalsRequest,
    current_user: dict = Depends(get_current_user),
):
    trading_collections = get_trading_db_collections()
    finance_collections = get_db_collections()
    data_collections = get_data_db()
    user_id = _current_user_id(current_user)
    plan = _require_plan(trading_collections, plan_id, current_user)
    if plan.get("status") != "approved":
        raise HTTPException(status_code=400, detail="plan must be approved before publishing live signals")
    if plan.get("paper_executed_at") or trading_collections["portfolio_paper_executions"].find_one({"plan_id": plan_id, "user_id": user_id}):
        raise HTTPException(status_code=400, detail="plan already has paper executions; cannot publish live signals")
    account = _require_securities_account(finance_collections, request.securities_account_id, user_id)
    items = list(
        trading_collections["portfolio_trade_plan_items"]
        .find({"plan_id": plan_id, "user_id": user_id})
        .sort("rank", 1)
    )
    now = datetime.utcnow()
    signals = []
    skipped_items = []
    for item in items:
        try:
            signal = build_portfolio_plan_live_signal(plan=plan, item=item, account=account, now=now)
        except Exception as exc:
            skipped_items.append({"symbol": item.get("symbol"), "reason": str(exc)})
            continue
        if signal is not None:
            signals.append(signal.to_mongo())

    order_ids = [signal["order_id"] for signal in signals]
    existing_signals = list(
        finance_collections["trade_signals"].find(active_signal_query_for_order_ids(user_id, order_ids))
    ) if order_ids else []
    existing_order_ids = {row.get("order_id") for row in existing_signals}
    new_signals = [signal for signal in signals if signal.get("order_id") not in existing_order_ids]
    risk_report = _build_live_publish_risk_report(
        finance_collections=finance_collections,
        data_collections=data_collections,
        account=account,
        signals=new_signals,
        user_id=user_id,
    )
    blocked = [item for item in risk_report["items"] if item["blockers"]]
    if blocked and not request.dry_run:
        raise HTTPException(status_code=409, detail={"message": "live signal risk check failed", "risk_report": risk_report})

    inserted_count = 0
    if not request.dry_run and new_signals:
        finance_collections["trade_signals"].insert_many(new_signals)
        inserted_count = len(new_signals)
        trading_collections["portfolio_trade_plans"].update_one(
            {"plan_id": plan_id, "user_id": user_id},
            {
                "$set": {
                    "live_signals_published_at": now,
                    "live_signal_count": len(signals),
                    "live_signal_inserted_count": inserted_count,
                    "live_signal_status_summary": {"pending": inserted_count, **summarize_signal_statuses(existing_signals)},
                }
            },
        )

    return {
        "success": True,
        "data": _jsonable(
            {
                "plan_id": plan_id,
                "dry_run": request.dry_run,
                "signals": signals,
                "new_signals": new_signals,
                "existing_signals": existing_signals,
                "skipped_items": skipped_items,
                "risk_report": risk_report,
                "inserted_count": inserted_count,
                "existing_count": len(existing_signals),
                "blocked": bool(blocked),
            }
        ),
    }


@router.get("/strategies/{strategy_id}/equity")
async def get_portfolio_strategy_equity(
    strategy_id: str,
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
    current_user: dict = Depends(get_current_user),
):
    collections = get_trading_db_collections()
    _require_strategy(collections, strategy_id)
    query: Dict[str, Any] = {"strategy_id": strategy_id, "user_id": _current_user_id(current_user)}
    if start_date or end_date:
        date_query: Dict[str, Any] = {}
        if start_date:
            date_query["$gte"] = start_date.replace("-", "")
        if end_date:
            date_query["$lte"] = end_date.replace("-", "")
        query["date"] = date_query
    rows = list(collections["portfolio_paper_equity"].find(query).sort("date", 1))
    return {"success": True, "data": _jsonable(rows)}


@router.get("/strategies/{strategy_id}/realtime-equity")
async def get_portfolio_strategy_realtime_equity(
    strategy_id: str,
    current_user: dict = Depends(get_current_user),
):
    collections = get_trading_db_collections()
    data_collections = get_data_db()
    _require_strategy(collections, strategy_id)
    snapshot = collections["portfolio_paper_positions"].find_one(
        {"strategy_id": strategy_id, "user_id": _current_user_id(current_user)},
        sort=[("as_of_date", -1)],
    )
    if not snapshot:
        return {"success": True, "data": None}

    positions = snapshot.get("positions", []) or []
    symbols = [row.get("symbol") for row in positions if row.get("symbol")]
    realtime_prices = _load_prices(data_collections["volume_price_realtime_latest"], symbols, "", "close", latest=True)
    realtime_docs = _load_realtime_docs(data_collections["volume_price_realtime_latest"], symbols)
    latest_close_prices = _load_latest_close_prices(data_collections["volume_price"], symbols)

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
        {"strategy_id": strategy_id, "user_id": _current_user_id(current_user)},
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
                "strategy_id": strategy_id,
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


def _apply_paper_execution(collections: Dict[str, Any], data_collections: Dict[str, Any], plan_id: str, force: bool = False, user_id: Optional[str] = None) -> Dict[str, Any]:
    plan = _require_plan(collections, plan_id, {"id": user_id} if user_id else None)
    if plan.get("status") != "approved" and not force:
        raise HTTPException(status_code=400, detail="plan must be approved before paper execution")
    if user_id and _plan_has_live_signals(get_db_collections(), plan_id, user_id) and not force:
        raise HTTPException(status_code=400, detail="plan already has live signals; cannot execute paper trading")
    if collections["portfolio_paper_executions"].find_one({"plan_id": plan_id}) and not force:
        raise HTTPException(status_code=400, detail="plan already has paper executions")

    strategy_id = plan.get("strategy_id")
    user_id = plan.get("user_id")
    execute_date = plan.get("execute_date")
    if not strategy_id or not execute_date:
        raise HTTPException(status_code=400, detail="plan is missing strategy_id or execute_date")

    previous_query = {"strategy_id": strategy_id, "as_of_date": {"$lte": plan.get("base_date")}}
    if user_id:
        previous_query["user_id"] = user_id
    previous = collections["portfolio_paper_positions"].find_one(
        previous_query,
        sort=[("as_of_date", -1)],
    ) or {}
    positions = {row.get("symbol"): dict(row) for row in previous.get("positions", []) if row.get("symbol")}
    cash = float(previous.get("cash", plan.get("config_snapshot", {}).get("initial_capital", 1_000_000.0)))
    items = list(collections["portfolio_trade_plan_items"].find({"plan_id": plan_id}).sort("rank", 1))
    symbols = [item.get("symbol") for item in items if item.get("symbol")]
    execution_symbols = sorted(
        {
            item.get("symbol")
            for item in items
            if item.get("symbol") and int(item.get("delta_shares", 0) or 0) != 0
        }
    )
    open_prices = _load_prices(data_collections["volume_price"], symbols, execute_date, "open")
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
        "user_id": user_id,
        "as_of_date": execute_date,
        "plan_id": plan_id,
        "cash": cash,
        "market_value": market_value,
        "equity": equity,
        "positions": position_rows,
        "updated_at": now,
    }
    position_query = {"strategy_id": strategy_id, "as_of_date": execute_date}
    equity_query = {"strategy_id": strategy_id, "date": execute_date}
    if user_id:
        position_query["user_id"] = user_id
        equity_query["user_id"] = user_id
    collections["portfolio_paper_positions"].replace_one(position_query, snapshot, upsert=True)
    collections["portfolio_paper_equity"].replace_one(
        equity_query,
        {"strategy_id": strategy_id, "user_id": user_id, "date": execute_date, "plan_id": plan_id, "equity": equity, "cash": cash, "market_value": market_value, "updated_at": now},
        upsert=True,
    )
    collections["portfolio_trade_plans"].update_one({"plan_id": plan_id}, {"$set": {"status": status, "paper_executed_at": now}})
    return {"plan_id": plan_id, "status": status, "executions": executions, "position_snapshot": snapshot}


def _paper_execution_status(collections: Dict[str, Any], data_collections: Dict[str, Any], plan: Dict[str, Any], items: List[Dict[str, Any]]) -> Dict[str, Any]:
    plan_id = str(plan.get("plan_id") or "")
    execution_query = {"plan_id": plan_id}
    if plan.get("user_id"):
        execution_query["user_id"] = plan.get("user_id")
    execute_date = plan.get("execute_date")
    effective_execute_date = execute_date or datetime.now().strftime("%Y%m%d")
    execution_symbols = sorted(
        {
            item.get("symbol")
            for item in items
            if item.get("symbol") and int(item.get("delta_shares", 0) or 0) != 0
        }
    )
    open_prices = _load_prices(data_collections["volume_price"], execution_symbols, effective_execute_date, "open") if execution_symbols else {}
    missing_symbols = [symbol for symbol in execution_symbols if symbol not in open_prices]
    latest_execution = collections["portfolio_paper_executions"].find_one(
        execution_query,
        sort=[("created_at", -1)],
    )
    execution_count = collections["portfolio_paper_executions"].count_documents(execution_query)

    if plan.get("status") == "approved":
        backend_status = "waiting_for_open_prices" if missing_symbols else "ready_for_background_execution"
    elif plan.get("status") in {"executed_paper", "partially_executed"}:
        backend_status = str(plan.get("status"))
    else:
        backend_status = "not_approved"

    return {
        "execute_date": execute_date,
        "effective_execute_date": effective_execute_date,
        "uses_runtime_execute_date": not bool(execute_date),
        "open_price_ready": not missing_symbols,
        "missing_open_price_count": len(missing_symbols),
        "missing_open_price_examples": missing_symbols[:10],
        "backend_execution_status": backend_status,
        "execution_count": execution_count,
        "latest_execution_result": latest_execution,
    }


def _load_prices(volume_price, symbols: List[str], trade_date: str, field: str, latest: bool = False) -> Dict[str, float]:
    query: Dict[str, Any] = {"symbol": {"$in": symbols}}
    if not latest:
        query["trade_date"] = trade_date
    rows = volume_price.find(query, {"_id": 0, "symbol": 1, field: 1, "open": 1, "close": 1})
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


def _load_realtime_docs(volume_price_realtime, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    rows = volume_price_realtime.find(
        {"symbol": {"$in": symbols}},
        {
            "_id": 0,
            "symbol": 1,
            "close": 1,
            "realtime_trade_time": 1,
            "snapshot_time": 1,
            "updated_at": 1,
        },
    )
    return {row["symbol"]: row for row in rows if row.get("symbol")}


def _load_latest_close_prices(volume_price, symbols: List[str]) -> Dict[str, float]:
    prices: Dict[str, float] = {}
    for symbol in symbols:
        row = volume_price.find_one(
            {"symbol": symbol},
            sort=[("trade_date", -1)],
            projection={"_id": 0, "symbol": 1, "close": 1},
        )
        if not row:
            continue
        try:
            price = float(row.get("close"))
        except (TypeError, ValueError):
            continue
        if price > 0:
            prices[symbol] = price
    return prices


def _execution_doc(plan: Dict[str, Any], item: Dict[str, Any], action: str, quantity: int, price: Optional[float], amount: float, blocker: Optional[str], now: datetime) -> Dict[str, Any]:
    return {
        "execution_id": str(uuid.uuid4()),
        "plan_id": plan["plan_id"],
        "strategy_id": plan["strategy_id"],
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
