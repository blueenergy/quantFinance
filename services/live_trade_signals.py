"""Helpers for constructing and validating executable live trade signals."""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, time as dt_time, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from services.execution_policy import (
    protected_limit_price as _protected_limit_price,
    build_price_guard,
    resolve_execution_policy,
)


LIVE_SIGNAL_SOURCE_PORTFOLIO_PLAN = "portfolio_plan"
TERMINAL_SIGNAL_STATUSES = {"filled", "executed", "rejected", "cancelled", "canceled", "partial_cancelled", "failed"}
ACTIVE_SIGNAL_STATUSES = {"pending", "retry_pending", "submitted", "partial_filled", "cancel_requested"}
CN_TZ = ZoneInfo("Asia/Shanghai")
CN_A_MORNING_OPEN = dt_time(9, 25)
CN_A_MORNING_CLOSE = dt_time(11, 30)
CN_A_AFTERNOON_OPEN = dt_time(13, 0)
CN_A_AFTERNOON_CLOSE = dt_time(15, 0)
DEFAULT_SIGNAL_VALID_TTL_SECONDS = 28800  # 8h: effectively entire A-share session (9:30–15:00)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _to_cn_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(CN_TZ)


def _from_cn_datetime(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _next_weekday(value) -> Any:
    current = value + timedelta(days=1)
    while current.weekday() >= 5:
        current += timedelta(days=1)
    return current


def default_activate_after(now: datetime) -> datetime:
    """Return the next conservative A-share executable window anchor."""

    local_now = _to_cn_datetime(now)
    local_date = local_now.date()
    local_time = local_now.time()

    if local_now.weekday() >= 5:
        next_date = local_date
        while next_date.weekday() >= 5:
            next_date += timedelta(days=1)
        return _from_cn_datetime(datetime.combine(next_date, CN_A_MORNING_OPEN, tzinfo=CN_TZ))

    if local_time < CN_A_MORNING_OPEN:
        return _from_cn_datetime(datetime.combine(local_date, CN_A_MORNING_OPEN, tzinfo=CN_TZ))
    if CN_A_MORNING_OPEN <= local_time <= CN_A_MORNING_CLOSE:
        return now
    if local_time < CN_A_AFTERNOON_OPEN:
        return _from_cn_datetime(datetime.combine(local_date, CN_A_AFTERNOON_OPEN, tzinfo=CN_TZ))
    if CN_A_AFTERNOON_OPEN <= local_time <= CN_A_AFTERNOON_CLOSE:
        return now

    next_date = _next_weekday(local_date)
    return _from_cn_datetime(datetime.combine(next_date, CN_A_MORNING_OPEN, tzinfo=CN_TZ))


def default_valid_until(now: datetime, activate_after: Optional[datetime] = None) -> datetime:
    mode = (os.getenv("PORTFOLIO_SIGNAL_VALID_UNTIL_MODE") or "from_activate_after").strip().lower()
    ttl = max(1, _env_int("PORTFOLIO_SIGNAL_VALID_TTL_SECONDS", DEFAULT_SIGNAL_VALID_TTL_SECONDS))
    if mode == "from_publish":
        anchor = now
    elif mode == "session_close":
        local_anchor = _to_cn_datetime(activate_after or now)
        return _from_cn_datetime(datetime.combine(local_anchor.date(), CN_A_AFTERNOON_CLOSE, tzinfo=CN_TZ))
    else:
        anchor = activate_after or now
    return anchor + timedelta(seconds=ttl)


class LiveTradeSignal(BaseModel):
    """Minimum executable signal contract consumed by quantTrader."""

    order_id: str
    user_id: str
    securities_account_id: str
    account_id: str
    broker: str
    mode: str = Field(default="live")
    is_executable: bool = Field(default=True)
    source: str
    plan_id: str
    strategy_template_id: str
    params_hash: str = Field(default="")
    symbol: str
    name: str = Field(default="")
    action: str
    size: int
    price: Optional[float] = None
    order_type: str = Field(default="limit")
    execution_phase: Optional[str] = None
    execution_priority: int = Field(default=1000)
    plan_item_rank: Optional[int] = None
    reference_price: Optional[float] = None
    reference_price_source: str = Field(default="estimated_price")
    max_slippage_bps: int = Field(default=100)
    fee_model: Dict[str, Any] = Field(default_factory=dict)
    price_floor: Optional[float] = None
    price_ceiling: Optional[float] = None
    effective_limit_price: Optional[float] = None
    activate_after: Optional[datetime] = None
    valid_until: Optional[datetime] = None
    phase_barrier: Optional[Dict[str, Any]] = None
    status: str = Field(default="pending")
    created_at: datetime

    @field_validator("mode")
    def _mode_must_be_live(cls, value: str) -> str:
        if value != "live":
            raise ValueError("live trade signal mode must be live")
        return value

    @field_validator("action")
    def _normalize_action(cls, value: str) -> str:
        normalized = str(value or "").lower()
        if normalized not in {"buy", "sell"}:
            raise ValueError("action must be buy or sell")
        return normalized

    @field_validator("size")
    def _size_must_be_positive(cls, value: int) -> int:
        if int(value or 0) <= 0:
            raise ValueError("size must be positive")
        return int(value)

    @field_validator("order_type")
    def _order_type_allowed(cls, value: str) -> str:
        normalized = str(value or "limit").lower()
        if normalized not in {"limit", "market"}:
            raise ValueError("order_type must be limit or market")
        return normalized

    @field_validator("execution_phase")
    def _execution_phase_allowed(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = str(value or "").lower()
        if normalized not in {"sell", "buy"}:
            raise ValueError("execution_phase must be sell or buy")
        return normalized

    @field_validator("max_slippage_bps")
    def _max_slippage_bps_non_negative(cls, value: int) -> int:
        if int(value or 0) < 0:
            raise ValueError("max_slippage_bps must be non-negative")
        return int(value)

    @model_validator(mode="after")
    def _default_execution_phase(self) -> "LiveTradeSignal":
        if self.execution_phase is None:
            self.execution_phase = self.action
        return self

    def to_mongo(self) -> Dict[str, Any]:
        doc = self.model_dump()
        created_at = self.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        doc["timestamp"] = created_at.timestamp()
        doc["datetime"] = self.created_at
        doc["strategy"] = self.strategy_template_id
        return doc


PHASE_BARRIER_PLAN_SELLS_TERMINAL = "plan_sells_terminal"

# Plan-level execution phase states (derived, see docs/stable_rebalance_execution.md D1).
PLAN_PHASE_APPROVED_PUBLISHED = "approved_published"
PLAN_PHASE_SELLING = "selling"
PLAN_PHASE_SELL_SETTLED = "sell_settled"
PLAN_PHASE_BUYING = "buying"
PLAN_PHASE_DONE = "done"


def build_plan_sells_terminal_barrier(plan_id: str) -> Dict[str, Any]:
    """Barrier carried on buy signals: hold until the plan's sells are terminal.

    Written in Phase 1, enforced by the trader in Phase 2 (see design doc D3).
    """

    return {"type": PHASE_BARRIER_PLAN_SELLS_TERMINAL, "plan_id": str(plan_id)}


def _signal_is_terminal(signal: Dict[str, Any]) -> bool:
    return str(signal.get("status") or "").lower() in TERMINAL_SIGNAL_STATUSES


def sells_barrier_satisfied(signals: Iterable[Dict[str, Any]]) -> bool:
    """Return True when every live sell signal in ``signals`` is terminal.

    Barrier definition (design doc D2): for a plan, the sell barrier is satisfied
    when all ``mode=live`` ``execution_phase=sell`` signals are in a terminal
    status. Cancelled/rejected sells count as terminal (the barrier means "no
    in-flight sells", not "all sells filled"). A plan with no sell signals
    trivially satisfies the barrier.
    """

    for signal in signals:
        if str(signal.get("mode") or "live").lower() != "live":
            continue
        phase = str(signal.get("execution_phase") or signal.get("action") or "").lower()
        if phase != "sell":
            continue
        if not _signal_is_terminal(signal):
            return False
    return True


def derive_execution_phase_state(signals: Iterable[Dict[str, Any]]) -> str:
    """Derive the plan-level execution phase from its live signals.

    This is a computed value (design doc D1); it is never stored as authoritative
    state. ``partial_done`` is intentionally not produced in Phase 1 (D3).
    """

    signals = [s for s in signals if str(s.get("mode") or "live").lower() == "live"]
    if not signals:
        return PLAN_PHASE_APPROVED_PUBLISHED

    sells = [s for s in signals if str(s.get("execution_phase") or s.get("action") or "").lower() == "sell"]
    buys = [s for s in signals if str(s.get("execution_phase") or s.get("action") or "").lower() == "buy"]

    sells_settled = sells_barrier_satisfied(signals)
    if not sells_settled:
        return PLAN_PHASE_SELLING

    if not buys:
        return PLAN_PHASE_DONE

    if all(_signal_is_terminal(s) for s in buys):
        return PLAN_PHASE_DONE

    buys_started = any(str(s.get("status") or "").lower() != "pending" for s in buys)
    if buys_started:
        return PLAN_PHASE_BUYING

    return PLAN_PHASE_SELL_SETTLED


def stable_live_order_id(
    *,
    plan_id: str,
    symbol: str,
    action: str,
    size: int,
    target_shares: Optional[int] = None,
    delta_shares: Optional[int] = None,
) -> str:
    """Build a deterministic idempotency key for plan-published live signals."""

    raw = "|".join(
        [
            str(plan_id),
            str(symbol),
            str(action).lower(),
            str(int(size or 0)),
            str(target_shares if target_shares is not None else ""),
            str(delta_shares if delta_shares is not None else ""),
        ]
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
    return f"live-plan-{digest}"


def build_portfolio_plan_live_signal(
    *,
    plan: Dict[str, Any],
    item: Dict[str, Any],
    account: Dict[str, Any],
    now: datetime,
) -> Optional[LiveTradeSignal]:
    delta = int(item.get("delta_shares", 0) or 0)
    symbol = str(item.get("symbol") or "").strip()
    if not symbol or delta == 0:
        return None

    action = "buy" if delta > 0 else "sell"
    execution_phase = action
    size = abs(delta)
    price = item.get("estimated_price")
    if price is not None:
        price = float(price)
    rank = item.get("rank")
    plan_item_rank = int(rank) if rank is not None else None
    reference_price_source = str(item.get("price_source") or "estimated_price")
    policy = resolve_execution_policy(account, reference_price_source=reference_price_source)
    reference_price = price if price and price > 0 else None
    guard = build_price_guard(reference_price, action, policy)
    max_slippage_bps = guard.max_slippage_bps
    price_floor = guard.price_floor
    price_ceiling = guard.price_ceiling
    effective_limit_price = guard.effective_limit_price
    execution_priority = int(plan_item_rank if plan_item_rank is not None else 1000)
    if action == "sell":
        execution_priority -= 10000
    activate_after = default_activate_after(now)
    # Phase 1: buy signals carry the sell barrier; enforcement lands in Phase 2.
    phase_barrier = (
        build_plan_sells_terminal_barrier(str(plan["plan_id"])) if action == "buy" else None
    )

    order_id = stable_live_order_id(
        plan_id=str(plan["plan_id"]),
        symbol=symbol,
        action=action,
        size=size,
        target_shares=int(item.get("target_shares", 0) or 0),
        delta_shares=delta,
    )
    return LiveTradeSignal(
        order_id=order_id,
        user_id=str(plan["user_id"]),
        securities_account_id=str(account["_id"]),
        account_id=str(account.get("account_id") or ""),
        broker=str(account.get("broker") or ""),
        source=LIVE_SIGNAL_SOURCE_PORTFOLIO_PLAN,
        plan_id=str(plan["plan_id"]),
        strategy_template_id=str(plan["strategy_template_id"]),
        params_hash=str(plan.get("params_hash") or ""),
        symbol=symbol,
        name=str(item.get("name") or ""),
        action=action,
        size=size,
        price=price,
        order_type=guard.order_type if reference_price is not None else "market",
        execution_phase=execution_phase,
        execution_priority=execution_priority,
        plan_item_rank=plan_item_rank,
        reference_price=reference_price,
        reference_price_source=reference_price_source,
        max_slippage_bps=max_slippage_bps,
        fee_model=account.get("fee_model") or {},
        price_floor=price_floor,
        price_ceiling=price_ceiling,
        effective_limit_price=effective_limit_price,
        activate_after=activate_after,
        valid_until=default_valid_until(now, activate_after),
        phase_barrier=phase_barrier,
        created_at=now,
    )


def ensure_live_signal_contract(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and normalize an existing signal-like dict."""

    try:
        return LiveTradeSignal(**doc).to_mongo()
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"invalid live trade signal: {exc}") from exc


def in_flight_sell_qty(
    finance_collections: Dict[str, Any],
    user_id: str,
    symbols: Iterable[str],
    securities_account_id: Optional[str] = None,
) -> int:
    """Shares already committed to the broker by live sell orders still in flight.

    ``trader_positions`` is a periodic snapshot, so its available quantity can
    still include shares an active sell order has claimed. Callers that must not
    double-spend that quantity subtract this.

    Deliberately does not try to tell whether the broker snapshot already froze
    these shares: comparing signal timestamps against ``synced_at`` needs a
    timezone-correct conversion that is easy to get wrong, and erring toward a
    smaller available quantity only makes the caller more conservative.
    """
    candidates = [str(s).strip() for s in symbols if str(s or "").strip()]
    if not candidates:
        return 0
    query: Dict[str, Any] = {
        "user_id": str(user_id),
        "mode": "live",
        "action": "sell",
        "symbol": {"$in": candidates},
        "status": {"$in": sorted(ACTIVE_SIGNAL_STATUSES)},
    }
    if securities_account_id:
        query["securities_account_id"] = str(securities_account_id)
    total = 0
    for row in finance_collections["trade_signals"].find(query, {"_id": 0, "size": 1}):
        try:
            size = int(row.get("size") or 0)
        except (TypeError, ValueError):
            continue
        if size > 0:
            total += size
    return total


def active_signal_query_for_order_ids(user_id: str, order_ids: Iterable[str]) -> Dict[str, Any]:
    return {
        "user_id": str(user_id),
        "order_id": {"$in": list(order_ids)},
        "mode": "live",
        "is_executable": True,
        "status": {"$in": sorted(ACTIVE_SIGNAL_STATUSES)},
    }


def summarize_signal_statuses(signals: List[Dict[str, Any]]) -> Dict[str, int]:
    summary: Dict[str, int] = {}
    for signal in signals:
        status = str(signal.get("status") or "unknown")
        summary[status] = summary.get(status, 0) + 1
    return summary
