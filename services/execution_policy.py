"""Pure execution-policy helpers for live trade signal price protection.

Designed to be migration-friendly: no FastAPI, Mongo, or env reads. Callers pass
account dicts and reference-price metadata; this module returns slippage limits
and protected limit prices for quantTrader to consume via trade_signals fields.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any, Dict, Optional

from services.portfolio_plan_market import PRICE_SOURCE_DAILY_CLOSE

PRICE_TICK = Decimal("0.01")

STYLE_BALANCED = "balanced"
STYLE_OPEN_AUCTION = "open_auction"

DEFAULT_CHASE_EXTRA_BPS = 30
BALANCED_MAX_SLIPPAGE_BPS = 30
OPEN_AUCTION_MAX_SLIPPAGE_BPS = 100

# Sources that anchor on prior close / next-open execution rather than live tape.
_OPEN_ANCHOR_SOURCES = frozenset(
    {
        PRICE_SOURCE_DAILY_CLOSE,
        "daily_close",
        "latest_close",
    }
)


@dataclass(frozen=True)
class ExecutionPolicy:
    style: str
    max_slippage_bps: int
    order_type: str = "limit"
    chase_extra_bps: int = DEFAULT_CHASE_EXTRA_BPS


@dataclass(frozen=True)
class ExecutionPriceGuard:
    reference_price: float
    max_slippage_bps: int
    price_floor: Optional[float]
    price_ceiling: Optional[float]
    effective_limit_price: Optional[float]
    order_type: str


def protected_limit_price(reference_price: float, max_slippage_bps: int, *, action: str) -> float:
    """Compute a buy ceiling or sell floor from reference price and slippage bps."""

    reference = Decimal(str(reference_price))
    slippage = Decimal(int(max_slippage_bps)) / Decimal("10000")
    multiplier = Decimal("1") - slippage if action == "sell" else Decimal("1") + slippage
    rounding = ROUND_FLOOR if action == "sell" else ROUND_CEILING
    return float((reference * multiplier).quantize(PRICE_TICK, rounding=rounding))


def _style_for_reference_source(reference_price_source: Optional[str]) -> str:
    source = str(reference_price_source or "").strip().lower()
    if source in _OPEN_ANCHOR_SOURCES:
        return STYLE_OPEN_AUCTION
    return STYLE_BALANCED


def _default_slippage_for_style(style: str) -> int:
    if style == STYLE_OPEN_AUCTION:
        return OPEN_AUCTION_MAX_SLIPPAGE_BPS
    return BALANCED_MAX_SLIPPAGE_BPS


def _account_slippage_override(account: Optional[Dict[str, Any]]) -> Optional[int]:
    if not account:
        return None
    risk_limits = account.get("risk_limits") or {}
    raw = risk_limits.get("max_slippage_bps")
    if raw in (None, ""):
        raw = account.get("max_slippage_bps")
    if raw in (None, ""):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return max(0, value)


def resolve_execution_policy(
    account: Optional[Dict[str, Any]] = None,
    *,
    reference_price_source: Optional[str] = None,
    intent: Optional[str] = None,
    max_slippage_bps: Optional[int] = None,
) -> ExecutionPolicy:
    """Resolve execution policy from account overrides and price-source context."""

    del intent  # reserved for future explicit caller hints (urgent / passive / slice)

    if max_slippage_bps is not None:
        bps = max(0, int(max_slippage_bps))
        style = _style_for_reference_source(reference_price_source)
        return ExecutionPolicy(style=style, max_slippage_bps=bps)

    override = _account_slippage_override(account)
    if override is not None:
        style = _style_for_reference_source(reference_price_source)
        return ExecutionPolicy(style=style, max_slippage_bps=override)

    style = _style_for_reference_source(reference_price_source)
    return ExecutionPolicy(
        style=style,
        max_slippage_bps=_default_slippage_for_style(style),
    )


def build_price_guard(
    reference_price: Optional[float],
    action: str,
    policy: ExecutionPolicy,
) -> ExecutionPriceGuard:
    """Build protected limit prices for a buy or sell action."""

    normalized_action = str(action or "").lower()
    if normalized_action not in {"buy", "sell"}:
        raise ValueError("action must be buy or sell")

    if reference_price is None or float(reference_price) <= 0:
        return ExecutionPriceGuard(
            reference_price=float(reference_price or 0),
            max_slippage_bps=policy.max_slippage_bps,
            price_floor=None,
            price_ceiling=None,
            effective_limit_price=None,
            order_type="market",
        )

    ref = float(reference_price)
    if normalized_action == "sell":
        price_floor = protected_limit_price(ref, policy.max_slippage_bps, action="sell")
        return ExecutionPriceGuard(
            reference_price=ref,
            max_slippage_bps=policy.max_slippage_bps,
            price_floor=price_floor,
            price_ceiling=None,
            effective_limit_price=price_floor,
            order_type=policy.order_type,
        )

    price_ceiling = protected_limit_price(ref, policy.max_slippage_bps, action="buy")
    return ExecutionPriceGuard(
        reference_price=ref,
        max_slippage_bps=policy.max_slippage_bps,
        price_floor=None,
        price_ceiling=price_ceiling,
        effective_limit_price=price_ceiling,
        order_type=policy.order_type,
    )
