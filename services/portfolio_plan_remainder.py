"""Gap-fill ("top-up") for portfolio plans whose live execution fell short.

After a plan is published live, some signals get cancelled or only partially
filled, so the deployed capital never reaches the planned target. This module
computes, **against the live broker account as the baseline**, the remaining
shares needed to finish what the plan originally intended to trade, capped by
the planned per-position budget and by available cash.

Design invariants:

- Baseline is the **live account** (`account_current_shares` from
  ``trader_positions``), not the paper lineage used at plan generation.
- Only items the plan **meant to trade** (original ``delta_shares != 0``) are
  topped up, and only in the **same direction**. This avoids inventing brand-new
  sells of pre-existing holdings the plan never intended to touch.
- Buys are capped **by remaining budget** (planned target cost minus current
  position value) and then by **available cash**; we never overspend past the
  original budget envelope even if the price moved up.
- Top-up is **preview-first**: the router returns a dry-run breakdown and a risk
  report; signals are only inserted after explicit human confirmation.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_FLOOR
from typing import Any, Callable, Dict, List, Optional

from services.live_trade_signals import (
    LiveTradeSignal,
    _protected_limit_price,
)

LIVE_SIGNAL_SOURCE_PLAN_TOPUP = "plan_topup"

DEFAULT_LOT_SIZE = 100
DEFAULT_VALID_FOR = timedelta(minutes=10)

# Skip / informational reason codes surfaced in the preview.
REASON_NOT_PLANNED_TRADE = "item_not_a_planned_trade"
REASON_ALREADY_AT_TARGET = "already_at_target"
REASON_LIVE_SIGNAL_ACTIVE = "live_signal_active"
REASON_MISSING_PRICE = "reference_price_unavailable"
REASON_ACCOUNT_NOT_SYNCED = "account_not_synced"
REASON_BUDGET_CAPPED = "budget_capped"
REASON_CASH_CAPPED = "cash_capped"
REASON_AVAILABLE_BELOW_GAP = "available_position_below_gap"
REASON_ESTIMATED_PRICE_MISSING = "estimated_price_missing"


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f


def _round_down_lot(shares: float, lot_size: int) -> int:
    """Round a share count down to the nearest tradable lot."""

    if lot_size <= 0:
        lot_size = DEFAULT_LOT_SIZE
    if shares <= 0:
        return 0
    lots = int(Decimal(str(shares)) / Decimal(lot_size))
    return lots * lot_size


@dataclass
class RemainderSpec:
    """Per-item remainder computation result."""

    symbol: str
    name: str
    rank: Optional[int]
    action: Optional[str]
    target_shares: int
    account_current_shares: Optional[int]
    account_available_shares: Optional[int]
    gap_shares: int
    topup_shares: int
    reference_price: Optional[float]
    reference_price_source: str
    estimated_price: Optional[float]
    estimated_amount: float
    lot_size: int
    actionable: bool
    reasons: List[str] = field(default_factory=list)

    def to_public(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "rank": self.rank,
            "action": self.action,
            "target_shares": self.target_shares,
            "account_current_shares": self.account_current_shares,
            "account_available_shares": self.account_available_shares,
            "gap_shares": self.gap_shares,
            "topup_shares": self.topup_shares,
            "reference_price": self.reference_price,
            "reference_price_source": self.reference_price_source,
            "estimated_price": self.estimated_price,
            "estimated_amount": self.estimated_amount,
            "lot_size": self.lot_size,
            "actionable": self.actionable,
            "reasons": sorted(set(self.reasons)),
        }


def build_remainder_specs(
    *,
    plan: Dict[str, Any],
    enriched_items: List[Dict[str, Any]],
    available_cash: Optional[float],
    account_synced: bool,
    active_symbols: Optional[set] = None,
    price_resolver: Callable[[str], tuple],
    default_lot_size: int = DEFAULT_LOT_SIZE,
) -> List[RemainderSpec]:
    """Compute per-item remainder specs (pure; no DB access).

    ``enriched_items`` must already carry live context (``account_current_shares``
    / ``account_available_shares``) as produced by
    ``_attach_live_plan_item_context``. ``price_resolver(symbol) -> (price, source)``
    supplies the current market reference price for budget/cash math.

    Processing order is sells first, then buys by rank, so the running cash
    budget reflects a sell-then-buy execution.
    """

    active_symbols = active_symbols or set()
    lot_size = _to_int(
        (plan.get("params_snapshot") or {}).get("lot_size"), default_lot_size
    ) or default_lot_size

    specs: List[RemainderSpec] = []
    for item in enriched_items:
        specs.append(
            _evaluate_item(
                item=item,
                lot_size=lot_size,
                account_synced=account_synced,
                active_symbols=active_symbols,
                price_resolver=price_resolver,
            )
        )

    # Apply the global available-cash constraint to buys, in execution order:
    # sells first (free up cash / consistent with sell->buy phase), then buys by
    # ascending rank. Sells do not consume cash.
    remaining_cash = _to_float(available_cash)
    buy_specs = sorted(
        [s for s in specs if s.actionable and s.action == "buy"],
        key=lambda s: (s.rank if s.rank is not None else 1_000_000),
    )
    for spec in buy_specs:
        if remaining_cash is None:
            # No cash snapshot: leave shares as budget-capped; the risk report
            # is the backstop for insufficient_cash.
            continue
        price = spec.reference_price
        if not price or price <= 0:
            continue
        affordable = _round_down_lot(remaining_cash / price, spec.lot_size)
        if affordable < spec.topup_shares:
            spec.topup_shares = affordable
            spec.reasons.append(REASON_CASH_CAPPED)
            if spec.topup_shares <= 0:
                spec.actionable = False
        remaining_cash -= spec.topup_shares * price

    return specs


def _evaluate_item(
    *,
    item: Dict[str, Any],
    lot_size: int,
    account_synced: bool,
    active_symbols: set,
    price_resolver: Callable[[str], tuple],
) -> RemainderSpec:
    symbol = str(item.get("symbol") or "").strip()
    name = str(item.get("name") or "")
    rank = item.get("rank")
    rank_int = _to_int(rank, None) if rank is not None else None
    target_shares = _to_int(item.get("target_shares"))
    original_delta = _to_int(item.get("delta_shares"))
    estimated_price = _to_float(item.get("estimated_price"))
    account_current = item.get("account_current_shares")
    account_available = item.get("account_available_shares")

    spec = RemainderSpec(
        symbol=symbol,
        name=name,
        rank=rank_int,
        action=None,
        target_shares=target_shares,
        account_current_shares=account_current,
        account_available_shares=account_available,
        gap_shares=0,
        topup_shares=0,
        reference_price=None,
        reference_price_source="unavailable",
        estimated_price=estimated_price,
        estimated_amount=0.0,
        lot_size=lot_size,
        actionable=False,
    )

    # Only finish what the plan meant to trade, in the same direction.
    if original_delta == 0:
        spec.reasons.append(REASON_NOT_PLANNED_TRADE)
        return spec

    if not account_synced:
        spec.reasons.append(REASON_ACCOUNT_NOT_SYNCED)
        return spec

    # Absent position row on a synced account = 0 holdings (trustworthy).
    current = _to_int(account_current, 0)
    available = _to_int(account_available, current)

    if original_delta > 0:
        spec.action = "buy"
        gap = target_shares - current
    else:
        spec.action = "sell"
        gap = current - target_shares
    spec.gap_shares = max(0, gap)

    base_symbol = symbol.split(".", 1)[0]
    if symbol in active_symbols or base_symbol in active_symbols:
        spec.reasons.append(REASON_LIVE_SIGNAL_ACTIVE)
        return spec

    if spec.gap_shares <= 0:
        spec.reasons.append(REASON_ALREADY_AT_TARGET)
        return spec

    price, source = price_resolver(symbol)
    price = _to_float(price)
    if price is None and estimated_price is not None:
        price, source = estimated_price, "estimated_price"
    spec.reference_price = price
    spec.reference_price_source = str(source or "unavailable")
    if price is None or price <= 0:
        spec.reasons.append(REASON_MISSING_PRICE)
        return spec

    if spec.action == "sell":
        topup = min(spec.gap_shares, max(0, available))
        # Sells go out in whole-lot remainders where possible, but a residual
        # odd lot (e.g. after a partial fill) must still be fully sold.
        topup_lots = _round_down_lot(topup, lot_size)
        topup = topup_lots if topup_lots > 0 else topup
        if available < spec.gap_shares:
            spec.reasons.append(REASON_AVAILABLE_BELOW_GAP)
        spec.topup_shares = topup
    else:
        topup = spec.gap_shares
        # by_budget: cap to the planned target cost minus current position value
        # so a price rise since plan time cannot overspend the original budget.
        if estimated_price and estimated_price > 0:
            planned_target_budget = target_shares * estimated_price
            current_value = current * price
            remaining_budget = max(0.0, planned_target_budget - current_value)
            budget_shares = _round_down_lot(remaining_budget / price, lot_size)
            if budget_shares < topup:
                topup = budget_shares
                spec.reasons.append(REASON_BUDGET_CAPPED)
        else:
            spec.reasons.append(REASON_ESTIMATED_PRICE_MISSING)
            topup = _round_down_lot(topup, lot_size)
        spec.topup_shares = max(0, topup)

    spec.estimated_amount = spec.topup_shares * price
    spec.actionable = spec.topup_shares > 0
    if not spec.actionable and REASON_BUDGET_CAPPED in spec.reasons:
        # Budget fully consumed already.
        pass
    return spec


def build_topup_signal_doc(
    *,
    plan: Dict[str, Any],
    account: Dict[str, Any],
    spec: RemainderSpec,
    now: datetime,
    user_id: str,
    reason: str,
    replaces_order_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Construct a ``plan_topup`` live signal document for one remainder spec."""

    if not spec.actionable or spec.topup_shares <= 0 or spec.action not in {"buy", "sell"}:
        raise ValueError("spec is not actionable")

    risk_limits = account.get("risk_limits") or {}
    max_slippage_bps = int(
        risk_limits.get("max_slippage_bps") or account.get("max_slippage_bps") or 100
    )
    reference_price = spec.reference_price
    price_floor = None
    price_ceiling = None
    if reference_price and reference_price > 0:
        if spec.action == "sell":
            price_floor = _protected_limit_price(reference_price, max_slippage_bps, action="sell")
        else:
            price_ceiling = _protected_limit_price(reference_price, max_slippage_bps, action="buy")
    effective_limit_price = price_floor if spec.action == "sell" else price_ceiling

    execution_priority = int(spec.rank if spec.rank is not None else 1000)
    if spec.action == "sell":
        execution_priority -= 10000

    order_id = f"live-topup-{uuid.uuid4().hex[:20]}"
    signal = LiveTradeSignal(
        order_id=order_id,
        user_id=str(user_id),
        securities_account_id=str(account["_id"]),
        account_id=str(account.get("account_id") or ""),
        broker=str(account.get("broker") or ""),
        source=LIVE_SIGNAL_SOURCE_PLAN_TOPUP,
        plan_id=str(plan["plan_id"]),
        strategy_template_id=str(plan["strategy_template_id"]),
        symbol=spec.symbol,
        name=spec.name,
        action=spec.action,
        size=int(spec.topup_shares),
        price=effective_limit_price if effective_limit_price else reference_price,
        order_type="limit" if (effective_limit_price or reference_price) else "market",
        execution_phase=spec.action,
        execution_priority=execution_priority,
        plan_item_rank=spec.rank,
        reference_price=reference_price,
        reference_price_source=spec.reference_price_source,
        max_slippage_bps=max_slippage_bps,
        fee_model=account.get("fee_model") or {},
        price_floor=price_floor,
        price_ceiling=price_ceiling,
        effective_limit_price=effective_limit_price,
        valid_until=now + DEFAULT_VALID_FOR,
        status="pending",
        created_at=now,
    )
    doc = signal.to_mongo()
    doc["created_by_user_id"] = str(user_id)
    doc["topup_reason"] = (reason or "")[:2000]
    doc["replaces_order_ids"] = list(replaces_order_ids or [])
    doc["topup_baseline"] = {
        "target_shares": spec.target_shares,
        "account_current_shares": spec.account_current_shares,
        "gap_shares": spec.gap_shares,
        "topup_shares": spec.topup_shares,
        "budget_capped": REASON_BUDGET_CAPPED in spec.reasons,
        "cash_capped": REASON_CASH_CAPPED in spec.reasons,
        "reference_price": spec.reference_price,
        "reference_price_source": spec.reference_price_source,
    }
    doc["is_executable"] = True
    doc["mode"] = "live"
    return doc
