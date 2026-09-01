"""Intraday T+0 live execution (Phase 3) — preview/submit via portfolio live-rebalance."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from typing import Any, Dict, List, Optional, Tuple

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING

from db import get_db_collections
from services.intraday_t0_signals import compact_trade_date, now_ts, to_float, to_int
from services.intraday_t0_trades import (
    DEFAULT_BUY_QTY_RATIO,
    DEFAULT_SELL_QTY_RATIO,
    _broker_available_qty,
    _buy_first_open_capacity,
    find_open_pair_for_symbol,
    list_open_pairs,
    preview_trade_from_signal,
    record_trade,
    trade_exists_for_signal,
)
from utils.stock_utils import serialize_doc
from utils.symbol_utils import normalize_symbol

log = logging.getLogger(__name__)

WATCH_TYPES = {"SELL_WATCH", "BUY_WATCH"}
_DEFAULT_AUTO_MIN_SCORE = 80.0
_EXEC_INDEXES_READY = False


def _global_auto_execute_enabled() -> bool:
    return str(os.getenv("INTRADAY_T0_AUTO_EXECUTE_ENABLED", "false")).lower() in {"1", "true", "yes"}


def ensure_intraday_t0_execution_indexes() -> None:
    global _EXEC_INDEXES_READY
    if _EXEC_INDEXES_READY:
        return
    settings = get_db_collections()["intraday_t0_execution_settings"]
    settings.create_index([("user_id", ASCENDING)], unique=True, name="user_id_unique")
    executions = get_db_collections()["intraday_t0_executions"]
    executions.create_index(
        [("user_id", ASCENDING), ("created_at", DESCENDING)],
        name="user_created_idx",
    )
    executions.create_index(
        [("signal_id", ASCENDING)],
        unique=True,
        name="signal_id_unique",
        partialFilterExpression={"signal_id": {"$type": "string"}},
    )
    _EXEC_INDEXES_READY = True


def _settings_coll():
    return get_db_collections()["intraday_t0_execution_settings"]


def _executions_coll():
    return get_db_collections()["intraday_t0_executions"]


def _default_settings(user_id: str) -> Dict[str, Any]:
    return {
        "user_id": user_id,
        "plan_id": None,
        "securities_account_id": None,
        "auto_execute_enabled": False,
        "auto_min_score": _DEFAULT_AUTO_MIN_SCORE,
        "sell_qty_ratio": DEFAULT_SELL_QTY_RATIO,
        "buy_qty_ratio": DEFAULT_BUY_QTY_RATIO,
        "buy_first_enabled": False,
        "updated_at": None,
    }


def get_user_settings(user_id: str) -> Dict[str, Any]:
    ensure_intraday_t0_execution_indexes()
    doc = _settings_coll().find_one({"user_id": user_id})
    if not doc:
        data = _default_settings(user_id)
        data["global_auto_execute_enabled"] = _global_auto_execute_enabled()
        return {"success": True, "data": data}
    payload = _default_settings(user_id)
    payload.update({k: doc.get(k) for k in payload if k in doc})
    if payload.get("buy_qty_ratio") is None:
        payload["buy_qty_ratio"] = payload.get("sell_qty_ratio") or DEFAULT_BUY_QTY_RATIO
    if payload.get("buy_first_enabled") is None:
        payload["buy_first_enabled"] = False
    if payload.get("plan_id"):
        try:
            resolved = _resolve_account_from_plan(user_id, str(payload["plan_id"]))
            if resolved:
                payload["securities_account_id"] = resolved
        except Exception:
            pass
    payload["global_auto_execute_enabled"] = _global_auto_execute_enabled()
    return {"success": True, "data": payload}


def upsert_user_settings(user_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    ensure_intraday_t0_execution_indexes()
    allowed = {
        "plan_id",
        "securities_account_id",
        "auto_execute_enabled",
        "auto_min_score",
        "sell_qty_ratio",
        "buy_qty_ratio",
        "buy_first_enabled",
    }
    update = {k: payload[k] for k in allowed if k in payload}
    if "auto_min_score" in update:
        update["auto_min_score"] = float(update["auto_min_score"])
    for ratio_key in ("sell_qty_ratio", "buy_qty_ratio"):
        if ratio_key in update:
            ratio = float(update[ratio_key])
            if ratio <= 0 or ratio > 1:
                raise ValueError(f"{ratio_key} must be in (0, 1]")
            update[ratio_key] = ratio
    if "buy_first_enabled" in update:
        update["buy_first_enabled"] = bool(update["buy_first_enabled"])
    update["user_id"] = user_id
    update["updated_at"] = now_ts()
    if update.get("plan_id"):
        resolved = _resolve_account_from_plan(user_id, str(update["plan_id"]))
        if resolved:
            update["securities_account_id"] = resolved
    _settings_coll().update_one({"user_id": user_id}, {"$set": update}, upsert=True)
    return get_user_settings(user_id)


def _load_signal(signal_id: str, user_id: str) -> Dict[str, Any]:
    try:
        oid = ObjectId(signal_id)
    except Exception as exc:
        raise ValueError("invalid signal_id") from exc
    doc = get_db_collections()["intraday_t0_signals"].find_one({"_id": oid, "user_id": user_id})
    if not doc:
        raise ValueError("signal not found")
    return doc


def execution_exists_for_signal(signal_id: str) -> bool:
    ensure_intraday_t0_execution_indexes()
    return _executions_coll().find_one({"signal_id": signal_id}, {"_id": 1}) is not None


def _resolve_account_from_plan(
    user_id: str,
    plan_id: str,
    current_user: Optional[Dict[str, Any]] = None,
) -> str:
    import routers.portfolio_plans as pp

    current_user = current_user or {"id": user_id}
    trading = pp.get_trading_db_collections()
    finance = pp.get_db_collections()
    plan = pp._require_plan(trading, plan_id, current_user)
    account_id = pp._lineage_live_account_id_from_fills(trading, finance, plan, user_id)
    if not account_id:
        account_id = pp._lineage_live_account_id_from_signals(trading, finance, plan, user_id)
    return str(account_id or "")


def _resolve_account_plan(
    user_id: str,
    plan_id: Optional[str],
    securities_account_id: Optional[str],
    current_user: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    settings = get_user_settings(user_id)["data"]
    resolved_plan = (plan_id or settings.get("plan_id") or "").strip()
    if not resolved_plan:
        raise ValueError("plan_id required — select a live portfolio first")
    resolved_account = (securities_account_id or settings.get("securities_account_id") or "").strip()
    if not resolved_account:
        resolved_account = _resolve_account_from_plan(user_id, resolved_plan, current_user)
    if not resolved_account:
        raise ValueError("selected portfolio has no bound securities account")
    return {"plan_id": resolved_plan, "securities_account_id": resolved_account}


def _lineage_shares(user_id: str, plan_id: str, symbol: str, current_user: Dict[str, Any]) -> int:
    import routers.portfolio_plans as pp

    trading = pp.get_trading_db_collections()
    finance = pp.get_db_collections()
    data = pp.get_data_db()
    plan = pp._require_plan(trading, plan_id, current_user)
    holdings = pp._lineage_live_holdings(trading, finance, data, plan, user_id)
    symbol_key = pp._symbol_key(symbol)
    for row in holdings:
        if pp._symbol_key(row.get("symbol")) == symbol_key:
            return to_int(row.get("shares"))
    return 0


def _round_lot(qty: int) -> int:
    if qty <= 0:
        return 0
    rounded = qty // 100 * 100
    return rounded if rounded >= 100 else (100 if qty >= 100 else 0)


def _resolve_open_pair(user_id: str, symbol: str, pair_id: Optional[str]) -> Dict[str, Any]:
    open_pairs = list_open_pairs(user_id)
    normalized = normalize_symbol(symbol)
    if pair_id:
        for pair in open_pairs:
            if pair.get("pair_id") == pair_id and normalize_symbol(pair.get("symbol") or "") == normalized:
                return pair
        raise ValueError("pair_id not found among open pairs for symbol")
    candidates = [p for p in open_pairs if normalize_symbol(p.get("symbol") or "") == normalized]
    if not candidates:
        raise ValueError("no open pair for symbol")
    return candidates[-1]


def _route_signal_execution(
    user_id: str,
    signal_type: str,
    symbol: str,
    *,
    pair_id: Optional[str],
    buy_first_enabled: bool,
) -> Tuple[str, Optional[str], str]:
    """Return (action, pair_id, pair_style) for a signal execution."""
    open_pair: Optional[Dict[str, Any]] = None
    if pair_id:
        open_pair = _resolve_open_pair(user_id, symbol, pair_id)
    else:
        open_pair = find_open_pair_for_symbol(user_id, symbol)

    if signal_type == "SELL_WATCH":
        if open_pair and open_pair.get("style") == "buy_first":
            return "sell", str(open_pair.get("pair_id")), "buy_first"
        if open_pair and open_pair.get("style") == "sell_first":
            raise ValueError("open sell_first pair exists — buy back before selling again")
        return "sell", pair_id or str(uuid.uuid4()), "sell_first"

    if signal_type == "BUY_WATCH":
        if open_pair and open_pair.get("style") == "sell_first":
            return "buy", str(open_pair.get("pair_id")), "sell_first"
        if open_pair and open_pair.get("style") == "buy_first":
            raise ValueError("open buy_first pair exists — sell back before buying again")
        if not buy_first_enabled:
            raise ValueError("buy_first disabled — enable buy_first_enabled in execution settings")
        return "buy", pair_id or str(uuid.uuid4()), "buy_first"

    raise ValueError("only BUY_WATCH / SELL_WATCH signals can be executed")


def build_execution_intent(
    user_id: str,
    signal_id: str,
    *,
    plan_id: Optional[str] = None,
    securities_account_id: Optional[str] = None,
    quantity: Optional[int] = None,
    pair_id: Optional[str] = None,
    sell_qty_ratio: Optional[float] = None,
    buy_qty_ratio: Optional[float] = None,
    current_user: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    ensure_intraday_t0_execution_indexes()
    signal = _load_signal(signal_id, user_id)
    signal_type = str(signal.get("signal_type") or "")
    if signal_type not in WATCH_TYPES:
        raise ValueError("only BUY_WATCH / SELL_WATCH signals can be executed")
    current_user = current_user or {"id": user_id}
    accounts = _resolve_account_plan(user_id, plan_id, securities_account_id, current_user)
    symbol = normalize_symbol(str(signal.get("symbol") or ""))
    if not symbol:
        raise ValueError("signal has no symbol")

    settings = get_user_settings(user_id)["data"]
    sell_ratio = float(sell_qty_ratio if sell_qty_ratio is not None else settings.get("sell_qty_ratio") or DEFAULT_SELL_QTY_RATIO)
    buy_ratio = float(
        buy_qty_ratio
        if buy_qty_ratio is not None
        else settings.get("buy_qty_ratio")
        or settings.get("sell_qty_ratio")
        or DEFAULT_BUY_QTY_RATIO
    )
    buy_first_enabled = bool(settings.get("buy_first_enabled"))
    trade_preview = preview_trade_from_signal(
        user_id,
        signal_id,
        sell_qty_ratio=sell_ratio,
        buy_qty_ratio=buy_ratio,
    )
    warnings = list(trade_preview.get("warnings") or [])

    action, resolved_pair_id, pair_style = _route_signal_execution(
        user_id,
        signal_type,
        symbol,
        pair_id=pair_id,
        buy_first_enabled=buy_first_enabled,
    )

    current_shares = _lineage_shares(user_id, accounts["plan_id"], symbol, current_user)

    if action == "sell" and pair_style == "sell_first":
        available = to_int(trade_preview.get("available_qty"))
        qty = to_int(quantity) if quantity is not None else to_int(trade_preview.get("suggested_qty"))
        qty = _round_lot(qty)
        if available > 0 and qty > available:
            qty = _round_lot(available)
            warnings.append("quantity_capped_by_available_qty")
        if qty <= 0:
            raise ValueError("no sellable quantity after lot normalization")
        target_shares = max(0, current_shares - qty)
    elif action == "buy" and pair_style == "sell_first":
        pair = _resolve_open_pair(user_id, symbol, resolved_pair_id)
        uncovered = to_int(pair.get("uncovered_qty"))
        if uncovered <= 0:
            raise ValueError("open pair has no uncovered quantity")
        qty = to_int(quantity) if quantity is not None else uncovered
        qty = min(_round_lot(qty), _round_lot(uncovered))
        if qty <= 0:
            raise ValueError("buy quantity must be positive after lot normalization")
        target_shares = current_shares + qty
    elif action == "buy" and pair_style == "buy_first":
        capacity = _buy_first_open_capacity(user_id, symbol)
        qty = to_int(quantity) if quantity is not None else to_int(trade_preview.get("suggested_qty"))
        qty = _round_lot(qty)
        if capacity > 0 and qty > capacity:
            qty = _round_lot(capacity)
            warnings.append("quantity_capped_by_buy_first_capacity")
        if qty <= 0:
            raise ValueError("no buy_first capacity after lot normalization")
        target_shares = current_shares + qty
    elif action == "sell" and pair_style == "buy_first":
        pair = _resolve_open_pair(user_id, symbol, resolved_pair_id)
        uncovered = to_int(pair.get("uncovered_qty"))
        if uncovered <= 0:
            raise ValueError("open pair has no uncovered quantity")
        available = _broker_available_qty(user_id, symbol)
        qty = to_int(quantity) if quantity is not None else to_int(trade_preview.get("suggested_qty"))
        qty = _round_lot(qty)
        max_qty = min(uncovered, available) if available > 0 else uncovered
        qty = min(qty, _round_lot(max_qty))
        if qty <= 0:
            raise ValueError("no sellable quantity to close buy_first pair")
        if available > 0 and qty > available:
            qty = _round_lot(available)
            warnings.append("quantity_capped_by_available_qty")
        target_shares = max(0, current_shares - qty)
    else:
        raise ValueError(f"unsupported execution route: action={action} pair_style={pair_style}")

    return {
        "signal_id": signal_id,
        "signal_type": signal_type,
        "signal_score": signal.get("signal_score"),
        "symbol": symbol,
        "action": action,
        "pair_style": pair_style,
        "quantity": qty,
        "current_shares": current_shares,
        "target_shares": target_shares,
        "pair_id": resolved_pair_id,
        "plan_id": accounts["plan_id"],
        "securities_account_id": accounts["securities_account_id"],
        "sell_qty_ratio": sell_ratio,
        "buy_qty_ratio": buy_ratio,
        "available_qty": trade_preview.get("available_qty"),
        "warnings": warnings,
    }


async def preview_execution_async(
    user_id: str,
    signal_id: str,
    *,
    plan_id: Optional[str] = None,
    securities_account_id: Optional[str] = None,
    quantity: Optional[int] = None,
    pair_id: Optional[str] = None,
    sell_qty_ratio: Optional[float] = None,
    buy_qty_ratio: Optional[float] = None,
    current_user: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    current_user = current_user or {"id": user_id}
    intent = build_execution_intent(
        user_id,
        signal_id,
        plan_id=plan_id,
        securities_account_id=securities_account_id,
        quantity=quantity,
        pair_id=pair_id,
        sell_qty_ratio=sell_qty_ratio,
        buy_qty_ratio=buy_qty_ratio,
        current_user=current_user,
    )
    from routers.portfolio_plans import LiveRebalanceRequest
    from routers.portfolio_plans.publishing import live_rebalance_portfolio

    request = LiveRebalanceRequest(
        securities_account_id=intent["securities_account_id"],
        targets={intent["symbol"]: intent["target_shares"]},
        exclude_after=False,
        dry_run=True,
    )
    rebalance = await live_rebalance_portfolio(intent["plan_id"], request, current_user)
    return {
        "success": True,
        "intent": intent,
        "already_executed": execution_exists_for_signal(signal_id),
        "rebalance_preview": rebalance.get("data") if isinstance(rebalance, dict) else rebalance,
    }


def sync_execution_trade_leg(
    user_id: str,
    intent: Dict[str, Any],
    signal: Dict[str, Any],
    *,
    auto: bool = False,
) -> Optional[Dict[str, Any]]:
    """Mirror a submitted execution leg into the trade ledger for pair routing."""
    signal_id = str(intent.get("signal_id") or "")
    if signal_id and trade_exists_for_signal(user_id, signal_id):
        return None
    price = to_float(signal.get("price"))
    if price is None or price <= 0:
        log.warning(
            "skip trade ledger sync: signal %s has no price",
            signal_id or intent.get("symbol"),
        )
        return None
    trade_date = compact_trade_date(signal.get("trade_date"))
    try:
        return record_trade(
            user_id,
            {
                "symbol": intent["symbol"],
                "leg": intent["action"],
                "quantity": intent["quantity"],
                "price": price,
                "trade_date": trade_date,
                "fees": 0.0,
                "signal_id": signal_id or None,
                "pair_id": intent.get("pair_id"),
                "notes": "auto_sync_from_execution" if auto else "sync_from_execution",
            },
        )
    except ValueError as exc:
        log.warning(
            "trade ledger sync failed user=%s signal=%s: %s",
            user_id,
            signal_id,
            exc,
        )
        return None


async def submit_execution_async(
    user_id: str,
    signal_id: str,
    *,
    plan_id: Optional[str] = None,
    securities_account_id: Optional[str] = None,
    quantity: Optional[int] = None,
    pair_id: Optional[str] = None,
    sell_qty_ratio: Optional[float] = None,
    buy_qty_ratio: Optional[float] = None,
    dry_run: bool = False,
    auto: bool = False,
    current_user: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    ensure_intraday_t0_execution_indexes()
    if execution_exists_for_signal(signal_id):
        raise ValueError("signal already submitted for execution")
    current_user = current_user or {"id": user_id}
    signal = _load_signal(signal_id, user_id)
    intent = build_execution_intent(
        user_id,
        signal_id,
        plan_id=plan_id,
        securities_account_id=securities_account_id,
        quantity=quantity,
        pair_id=pair_id,
        sell_qty_ratio=sell_qty_ratio,
        buy_qty_ratio=buy_qty_ratio,
        current_user=current_user,
    )
    from routers.portfolio_plans import LiveRebalanceRequest
    from routers.portfolio_plans.publishing import live_rebalance_portfolio

    request = LiveRebalanceRequest(
        securities_account_id=intent["securities_account_id"],
        targets={intent["symbol"]: intent["target_shares"]},
        exclude_after=False,
        dry_run=dry_run,
    )
    rebalance = await live_rebalance_portfolio(intent["plan_id"], request, current_user)
    rebalance_data = rebalance.get("data") if isinstance(rebalance, dict) else rebalance
    if dry_run:
        return {
            "success": True,
            "dry_run": True,
            "intent": intent,
            "rebalance_preview": rebalance_data,
        }

    signals = []
    if isinstance(rebalance_data, dict):
        signals = rebalance_data.get("signals") or rebalance_data.get("new_signals") or []
    order_ids = [s.get("order_id") for s in signals if s.get("order_id")]
    exec_doc = {
        "user_id": user_id,
        "signal_id": signal_id,
        "plan_id": intent["plan_id"],
        "securities_account_id": intent["securities_account_id"],
        "symbol": intent["symbol"],
        "action": intent["action"],
        "pair_style": intent.get("pair_style"),
        "quantity": intent["quantity"],
        "target_shares": intent["target_shares"],
        "pair_id": intent.get("pair_id"),
        "order_ids": order_ids,
        "auto": bool(auto),
        "status": "submitted",
        "created_at": now_ts(),
        "rebalance_plan_id": (rebalance_data or {}).get("plan_id"),
    }
    _executions_coll().insert_one(exec_doc)
    trade_sync = sync_execution_trade_leg(user_id, intent, signal, auto=auto)
    return {
        "success": True,
        "intent": intent,
        "execution": serialize_doc(exec_doc),
        "trade_sync": trade_sync,
        "rebalance": rebalance_data,
    }


def list_executions(user_id: str, limit: int = 100) -> List[Dict[str, Any]]:
    ensure_intraday_t0_execution_indexes()
    rows = (
        _executions_coll()
        .find({"user_id": user_id})
        .sort([("created_at", DESCENDING), ("_id", DESCENDING)])
        .limit(limit)
    )
    return [serialize_doc(doc) for doc in rows]


def maybe_auto_execute_after_signal(user_id: str, signal_doc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Fire-and-forget auto execution after signal insert when user settings allow."""
    if not _global_auto_execute_enabled():
        return None
    signal_type = str(signal_doc.get("signal_type") or "")
    if signal_type not in WATCH_TYPES:
        return None
    settings = get_user_settings(user_id)["data"]
    if not settings.get("auto_execute_enabled"):
        return None
    if signal_type == "BUY_WATCH":
        symbol = normalize_symbol(str(signal_doc.get("symbol") or ""))
        open_pair = find_open_pair_for_symbol(user_id, symbol) if symbol else None
        opening_buy_first = not open_pair or open_pair.get("style") != "sell_first"
        if opening_buy_first and not settings.get("buy_first_enabled"):
            return None
    score = to_float(signal_doc.get("signal_score"))
    min_score = to_float(settings.get("auto_min_score"), _DEFAULT_AUTO_MIN_SCORE) or _DEFAULT_AUTO_MIN_SCORE
    if score is None or score < min_score:
        return None
    signal_id = str(signal_doc.get("_id") or "")
    if not signal_id or execution_exists_for_signal(signal_id):
        return None
    current_user = {"id": user_id}

    async def _run() -> Dict[str, Any]:
        return await submit_execution_async(
            user_id,
            signal_id,
            auto=True,
            current_user=current_user,
        )

    try:
        return asyncio.run(_run())
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_run())
        finally:
            loop.close()
    except Exception as exc:
        log.warning("intraday T+0 auto execute failed user=%s signal=%s: %s", user_id, signal_id, exc)
        _executions_coll().update_one(
            {"signal_id": signal_id},
            {
                "$set": {
                    "user_id": user_id,
                    "signal_id": signal_id,
                    "status": "failed",
                    "error": str(exc),
                    "auto": True,
                    "created_at": now_ts(),
                }
            },
            upsert=True,
        )
        return None
