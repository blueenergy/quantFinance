"""Manual repricing / replacement live buy signals (preview + confirm)."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta
from datetime import date as date_cls
from typing import Any, Dict, Optional, Tuple

from bson import ObjectId
from bson.errors import InvalidId

from db import get_data_db
from routers.stock import _symbol_candidates
from services.live_trade_signals import _protected_limit_price, LiveTradeSignal

LIVE_SIGNAL_SOURCE_MANUAL_REPRICE = "manual_reprice"

# Max relative deviation of user limit from market reference (risk guard).
_MAX_LIMIT_VS_MARKET_DEVIATION = float(os.getenv("REPRICE_MAX_LIMIT_VS_MARKET_DEVIATION", "0.25"))
_REALTIME_MAX_STALE_DAYS = int(os.getenv("REPRICE_REALTIME_MAX_STALE_DAYS", "1"))


class RepriceError(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _first_float(*values: Any) -> Optional[float]:
    for v in values:
        if v is None or v == "":
            continue
        try:
            f = float(v)
            if f == f:  # not NaN
                return f
        except (TypeError, ValueError):
            continue
    return None


def _parse_doc_date(value: Any) -> Optional[date_cls]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date_cls):
        return value
    text = str(value).strip()
    for fmt, length in (("%Y-%m-%d", 10), ("%Y%m%d", 8)):
        try:
            return datetime.strptime(text[:length], fmt).date()
        except (TypeError, ValueError):
            continue
    return None


def _market_doc_date(doc: Optional[Dict[str, Any]]) -> Optional[date_cls]:
    if not doc:
        return None
    return (
        _parse_doc_date(doc.get("trade_date"))
        or _parse_doc_date(doc.get("date"))
        or _parse_doc_date(doc.get("updated_at"))
        or _parse_doc_date(doc.get("created_at"))
    )


def _is_realtime_fresh(rt_doc: Dict[str, Any], daily_doc: Optional[Dict[str, Any]]) -> bool:
    rt_date = _market_doc_date(rt_doc)
    if not rt_date:
        return False

    daily_date = _market_doc_date(daily_doc)
    if daily_date:
        return rt_date >= daily_date

    return (datetime.utcnow().date() - rt_date).days <= _REALTIME_MAX_STALE_DAYS


def resolve_market_reference_price(symbol: str) -> Tuple[Optional[float], str]:
    """Best-effort reference price for A-share symbol.

    Use realtime when it is at least as fresh as the latest daily bar; otherwise
    fall back to daily close so stale realtime snapshots cannot anchor orders.
    """
    data = get_data_db()
    candidates = _symbol_candidates(symbol)

    daily_doc = None
    vp = data.get("volume_price")
    if vp is not None:
        daily_doc = vp.find_one({"symbol": {"$in": candidates}}, sort=[("trade_date", -1)])

    rt = data.get("volume_price_realtime_latest")
    if rt is not None:
        doc = rt.find_one(
            {"symbol": {"$in": candidates}},
            sort=[("trade_time", -1), ("trade_date", -1), ("_id", -1)],
        )
        if doc:
            px = _first_float(doc.get("close"), doc.get("price"), doc.get("latest_price"))
            if px and px > 0 and _is_realtime_fresh(doc, daily_doc):
                return float(px), "realtime"

    if daily_doc:
        px = _first_float(daily_doc.get("close"))
        if px and px > 0:
            return float(px), "daily_close"

    return None, "unavailable"


def _load_securities_account(collections: Dict[str, Any], securities_account_id: str, user_id: str) -> Optional[Dict[str, Any]]:
    if not securities_account_id:
        return None
    coll = collections.get("securities_accounts")
    if coll is None:
        return None
    try:
        oid = ObjectId(securities_account_id)
    except (InvalidId, TypeError):
        return None
    return coll.find_one({"_id": oid, "user_id": user_id})


def _append_reprice_audit(
    collections: Dict[str, Any],
    *,
    plan_id: str,
    user_id: str,
    reviewer: str,
    message: str,
    payload: Dict[str, Any],
) -> None:
    logs = collections.get("portfolio_plan_operation_logs")
    if logs is None:
        return
    now = datetime.utcnow()
    doc = {
        "log_id": str(uuid.uuid4()),
        "plan_id": plan_id or "",
        "user_id": user_id,
        "task_id": None,
        "operation_type": "signal_reprice_confirm",
        "operation_symbol": payload.get("symbol"),
        "status": "success",
        "message": message,
        "payload": {**payload, "reviewer": reviewer},
        "created_at": now,
        "updated_at": now,
    }
    logs.insert_one(doc)


def _normalize_cancelled_buy(signal: Dict[str, Any]) -> None:
    action = str(signal.get("action") or "").lower()
    if action != "buy":
        raise RepriceError("reprice is only supported for buy signals", 400)
    st = str(signal.get("status") or "").lower()
    if st not in {"cancelled", "canceled", "partial_cancelled"}:
        raise RepriceError("signal must be cancelled or partial_cancelled to reprice", 400)


def build_reprice_preview(
    collections: Dict[str, Any],
    *,
    user_id: str,
    order_id: str,
) -> Dict[str, Any]:
    signals_coll = collections["trade_signals"]
    signal = signals_coll.find_one({"order_id": order_id, "user_id": user_id})
    if not signal:
        raise RepriceError("signal not found", 404)

    _normalize_cancelled_buy(signal)

    symbol = str(signal.get("symbol") or "").strip()
    if not symbol:
        raise RepriceError("signal missing symbol", 400)
    if not str(signal.get("securities_account_id") or "").strip():
        raise RepriceError("signal missing securities_account_id", 400)

    market_px, market_src = resolve_market_reference_price(symbol)
    max_slippage_bps = int(signal.get("max_slippage_bps") or 100)
    old_limit = _first_float(signal.get("effective_limit_price"), signal.get("price_ceiling"), signal.get("price"))
    old_ref = _first_float(signal.get("reference_price")) or old_limit

    suggested_limit = None
    if market_px and market_px > 0:
        suggested_limit = _protected_limit_price(market_px, max_slippage_bps + 30, action="buy")
    elif old_ref and old_ref > 0:
        suggested_limit = _protected_limit_price(old_ref, max_slippage_bps + 30, action="buy")

    size = int(signal.get("size") or 0)
    if size <= 0:
        raise RepriceError("signal has invalid size", 400)

    def _dev(a: Optional[float], b: Optional[float]) -> Optional[float]:
        if a is None or b is None or b == 0:
            return None
        return (a - b) / b * 100.0

    return {
        "success": True,
        "preview": True,
        "order_id": order_id,
        "symbol": symbol,
        "size": size,
        "market_reference_price": market_px,
        "market_reference_source": market_src,
        "original_effective_limit_price": old_limit,
        "original_reference_price": old_ref,
        "suggested_effective_limit_price": suggested_limit,
        "max_slippage_bps": max_slippage_bps,
        "deviation_suggested_vs_market_pct": _dev(suggested_limit, market_px),
        "deviation_original_limit_vs_market_pct": _dev(old_limit, market_px),
        "max_allowed_deviation_vs_market_pct": _MAX_LIMIT_VS_MARKET_DEVIATION * 100,
        "plan_id": signal.get("plan_id"),
    }


def confirm_reprice_and_insert_signal(
    collections: Dict[str, Any],
    *,
    user_id: str,
    order_id: str,
    effective_limit_price: float,
    size_override: Optional[int],
    reason: str,
    reviewer: str,
) -> Dict[str, Any]:
    signals_coll = collections["trade_signals"]
    signal = signals_coll.find_one({"order_id": order_id, "user_id": user_id})
    if not signal:
        raise RepriceError("signal not found", 404)

    _normalize_cancelled_buy(signal)

    symbol = str(signal.get("symbol") or "").strip()
    if not symbol:
        raise RepriceError("signal missing symbol", 400)
    if not str(signal.get("securities_account_id") or "").strip():
        raise RepriceError("signal missing securities_account_id", 400)

    base_size = int(signal.get("size") or 0)
    size = int(size_override) if size_override is not None else base_size
    if size <= 0:
        raise RepriceError("size must be positive", 400)

    market_px, market_src = resolve_market_reference_price(symbol)
    if market_px and market_px > 0:
        lo = market_px * (1.0 - _MAX_LIMIT_VS_MARKET_DEVIATION)
        hi = market_px * (1.0 + _MAX_LIMIT_VS_MARKET_DEVIATION)
        if effective_limit_price < lo or effective_limit_price > hi:
            raise RepriceError(
                f"effective_limit_price out of allowed band vs market reference "
                f"({market_px:.4f} from {market_src}); allowed [{lo:.4f}, {hi:.4f}]",
                400,
            )

    account = _load_securities_account(
        collections, str(signal.get("securities_account_id") or ""), user_id
    )
    risk = (account or {}).get("risk_limits") or {}
    max_order_amount = risk.get("max_order_amount")
    if max_order_amount is not None:
        try:
            cap = float(max_order_amount)
            if cap > 0 and effective_limit_price * size > cap:
                raise RepriceError("order notional exceeds account max_order_amount", 400)
        except RepriceError:
            raise
        except (TypeError, ValueError):
            pass

    now = datetime.utcnow()
    new_order_id = f"live-reprice-{uuid.uuid4().hex[:20]}"

    lt = LiveTradeSignal(
        order_id=new_order_id,
        user_id=str(user_id),
        securities_account_id=str(signal.get("securities_account_id") or ""),
        account_id=str(signal.get("account_id") or ""),
        broker=str(signal.get("broker") or ""),
        source=LIVE_SIGNAL_SOURCE_MANUAL_REPRICE,
        plan_id=str(signal.get("plan_id") or ""),
        strategy_template_id=str(signal.get("strategy_template_id") or ""),
        symbol=symbol,
        name=str(signal.get("name") or ""),
        action="buy",
        size=size,
        price=float(effective_limit_price),
        order_type="limit",
        execution_phase="buy",
        execution_priority=int(signal.get("execution_priority") or 1000),
        plan_item_rank=signal.get("plan_item_rank"),
        reference_price=float(market_px) if market_px else float(effective_limit_price),
        reference_price_source=str(market_src or "manual_reprice"),
        max_slippage_bps=int(signal.get("max_slippage_bps") or 100),
        fee_model=signal.get("fee_model") or {},
        price_floor=None,
        price_ceiling=float(effective_limit_price),
        effective_limit_price=float(effective_limit_price),
        valid_until=now + timedelta(hours=4),
        status="pending",
        created_at=now,
    )
    doc = lt.to_mongo()
    doc["replaces_order_id"] = order_id
    doc["reprice_reason"] = reason[:2000] if reason else ""
    doc["created_by_user_id"] = user_id
    doc["is_executable"] = True
    doc["mode"] = "live"

    signals_coll.insert_one(doc)

    plan_id = str(signal.get("plan_id") or "")
    _append_reprice_audit(
        collections,
        plan_id=plan_id,
        user_id=user_id,
        reviewer=reviewer,
        message=f"replacement signal {new_order_id} for {order_id}",
        payload={
            "parent_order_id": order_id,
            "new_order_id": new_order_id,
            "symbol": symbol,
            "size": size,
            "effective_limit_price": effective_limit_price,
            "market_reference_price": market_px,
            "market_reference_source": market_src,
            "reason": reason[:2000] if reason else "",
        },
    )

    return {
        "success": True,
        "new_order_id": new_order_id,
        "data": {"order_id": new_order_id},
    }
