"""Helpers for constructing and validating executable live trade signals."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any, Dict, Iterable, List, Optional

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator


LIVE_SIGNAL_SOURCE_PORTFOLIO_PLAN = "portfolio_plan"
TERMINAL_SIGNAL_STATUSES = {"filled", "executed", "rejected", "cancelled", "canceled", "partial_cancelled", "failed"}
ACTIVE_SIGNAL_STATUSES = {"pending", "retry_pending", "submitted", "partial_filled"}
PRICE_TICK = Decimal("0.01")


def _round_price_to_tick(price: float, *, action: str) -> float:
    """Round protected limit prices to the A-share 0.01 price tick."""

    decimal_price = Decimal(str(price))
    rounding = ROUND_FLOOR if action == "sell" else ROUND_CEILING
    return float(decimal_price.quantize(PRICE_TICK, rounding=rounding))


def _protected_limit_price(reference_price: float, max_slippage_bps: int, *, action: str) -> float:
    reference = Decimal(str(reference_price))
    slippage = Decimal(int(max_slippage_bps)) / Decimal("10000")
    multiplier = Decimal("1") - slippage if action == "sell" else Decimal("1") + slippage
    rounding = ROUND_FLOOR if action == "sell" else ROUND_CEILING
    return float((reference * multiplier).quantize(PRICE_TICK, rounding=rounding))


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
    price_floor: Optional[float] = None
    price_ceiling: Optional[float] = None
    effective_limit_price: Optional[float] = None
    valid_until: Optional[datetime] = None
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
        doc["timestamp"] = self.created_at.timestamp()
        doc["datetime"] = self.created_at
        doc["strategy"] = self.strategy_template_id
        return doc


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
    risk_limits = account.get("risk_limits") or {}
    max_slippage_bps = int(risk_limits.get("max_slippage_bps") or account.get("max_slippage_bps") or 100)
    reference_price = price if price and price > 0 else None
    price_floor = None
    price_ceiling = None
    if reference_price is not None:
        if action == "sell":
            price_floor = _protected_limit_price(reference_price, max_slippage_bps, action=action)
        else:
            price_ceiling = _protected_limit_price(reference_price, max_slippage_bps, action=action)
    effective_limit_price = price_floor if action == "sell" else price_ceiling
    execution_priority = int(plan_item_rank if plan_item_rank is not None else 1000)
    if action == "sell":
        execution_priority -= 10000

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
        symbol=symbol,
        name=str(item.get("name") or ""),
        action=action,
        size=size,
        price=price,
        order_type="limit" if price else "market",
        execution_phase=execution_phase,
        execution_priority=execution_priority,
        plan_item_rank=plan_item_rank,
        reference_price=reference_price,
        max_slippage_bps=max_slippage_bps,
        price_floor=price_floor,
        price_ceiling=price_ceiling,
        effective_limit_price=effective_limit_price,
        valid_until=now + timedelta(minutes=10),
        created_at=now,
    )


def ensure_live_signal_contract(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and normalize an existing signal-like dict."""

    try:
        return LiveTradeSignal(**doc).to_mongo()
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"invalid live trade signal: {exc}") from exc


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
