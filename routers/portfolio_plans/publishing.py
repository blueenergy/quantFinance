"""Publishing / live-rebalance domain for portfolio plans.

Mechanical extraction only, no behavior change. The sub-router is mounted by
``portfolio_plans.__init__`` onto the existing ``/portfolio-plans`` router.

This module owns the live-publish risk report and the batch-partition logic that
decides which live signals may be published. The heavy lifting still leans on
infra helpers that live in the package module (account snapshots, volatility /
guard loaders, sizing-basis resolvers, position snapshots); those are resolved
through ``_pp`` at call time so existing tests can keep monkeypatching the
package namespace (e.g. ``_build_live_publish_risk_report``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
import logging
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException

from stock_data_access.board_lots import (  # pyright: ignore[reportMissingImports]
    is_star_market,
    normalize_board_target_shares,
)

from auth import get_current_user
from services.live_trade_signals import (
    ACTIVE_SIGNAL_STATUSES,
    active_signal_query_for_order_ids,
    build_portfolio_plan_live_signal,
    stamp_t0_correlation,
    summarize_signal_statuses,
)
from services.portfolio_lineage_state import mark_lineage_live
from services.portfolio_risk_snapshots import capture_plan_risk_snapshot
from services.account_sellable import LEGACY_MODE_OR_CHAIN, sellable_shares
from services.portfolio_plan_market import (
    PRICE_SOURCE_DAILY_CLOSE,
    reprice_plan_items_for_publish,
)
from services.portfolio_plan_remainder import (
    build_remainder_specs,
    build_topup_signal_doc,
)
from services.portfolio_record_kind import (
    PLAN_TYPE_REBALANCE,
    RECORD_KIND_TRADE_PLAN,
    TRADE_PLAN_PLAN_TYPES,
)

import routers.portfolio_plans as _pp
from . import (
    LiveRebalanceRequest,
    PublishLiveSignalsRequest,
    ReplanRemainderRequest,
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
log = logging.getLogger(__name__)


def _build_live_publish_risk_report(
    *,
    finance_collections: Dict[str, Any],
    data_collections: Dict[str, Any],
    account: Dict[str, Any],
    signals: List[Dict[str, Any]],
    user_id: str,
    trading_collections: Optional[Dict[str, Any]] = None,
    plan: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    account_snapshot = _pp._latest_account_snapshot(finance_collections, account, user_id)
    available_cash = _numeric(account_snapshot.get("available_cash") or account_snapshot.get("cash"))
    total_assets = _numeric(account_snapshot.get("total_assets") or account_snapshot.get("total_asset"))
    risk_limits = account.get("risk_limits") or {}
    max_order_amount = _numeric(risk_limits.get("max_order_amount") or account.get("max_order_amount"), float("inf"))
    max_daily_amount = _numeric(risk_limits.get("max_daily_amount") or account.get("max_daily_amount"), float("inf"))
    max_position_pct = _numeric(risk_limits.get("max_position_pct") or account.get("max_position_pct"), 0.0)
    daily_used_amount = _pp._daily_live_signal_amount(finance_collections, account, user_id)
    market_docs = _pp._load_stock_guard_docs(data_collections, [signal["symbol"] for signal in signals])
    buy_symbols = [signal["symbol"] for signal in signals if signal.get("action") == "buy" and signal.get("symbol")]
    vol_features_by_symbol = _pp._load_volatility_features_by_symbol(data_collections, buy_symbols)
    positions_by_key = _pp._position_snapshots_by_symbol(
        finance_collections,
        signals=signals,
        user_id=user_id,
    )
    lineage_positions_by_symbol, lineage_positions_synced = (
        _pp._lineage_positions_by_symbol(trading_collections, plan=plan, user_id=user_id)
        if _pp._live_sell_sizing_basis() == "lineage"
        else ({}, False)
    )

    cash_reserved = 0.0
    day_reserved = 0.0
    items = []
    for signal in signals:
        amount = abs(int(signal.get("size", 0) or 0)) * _numeric(signal.get("price"))
        blockers: List[str] = []
        sellable_meta: Dict[str, Any] = {}
        if not _pp._account_live_enabled(account):
            blockers.append("live_trading_disabled")
        if not account.get("broker") or not account.get("account_id"):
            blockers.append("account_binding_missing")
        if max_order_amount != float("inf") and amount > max_order_amount:
            blockers.append("max_order_amount_exceeded")
        if max_daily_amount != float("inf") and daily_used_amount + day_reserved + amount > max_daily_amount:
            blockers.append("max_daily_amount_exceeded")
        if signal.get("action") == "buy":
            blockers.extend(_pp._buy_blockers_from_market_doc(signal, market_docs.get(signal["symbol"], {})))
            blockers.extend(_pp._volatility_buy_blockers(vol_features_by_symbol.get(signal.get("symbol"))))
            if available_cash and cash_reserved + amount > available_cash:
                blockers.append("insufficient_cash")
            if total_assets > 0 and max_position_pct > 0:
                position = positions_by_key.get(
                    (str(signal.get("securities_account_id") or ""), str(signal.get("symbol") or ""))
                ) or {}
                current_market_value = _numeric(position.get("market_value"))
                if (current_market_value + amount) / total_assets * 100 > max_position_pct:
                    blockers.append("max_position_pct_exceeded")
            cash_reserved += amount
        elif signal.get("action") == "sell":
            if lineage_positions_synced:
                lineage_shares = lineage_positions_by_symbol.get(_symbol_key(signal.get("symbol")), 0)
                if int(signal.get("size", 0) or 0) > lineage_shares:
                    blockers.append("insufficient_lineage_position")
            position = positions_by_key.get(
                (str(signal.get("securities_account_id") or ""), str(signal.get("symbol") or ""))
            ) or {}
            sellable = sellable_shares(
                position,
                legacy_mode=LEGACY_MODE_OR_CHAIN,
                log_context="publish_risk",
            )
            if int(signal.get("size", 0) or 0) > sellable.legacy_shares:
                blockers.append("insufficient_available_position")
            sellable_meta = sellable.observability()
        day_reserved += amount
        item_warnings = sorted(
            {
                warning
                for warning in (
                    ["priced_from_stale_close"]
                    if str(signal.get("reference_price_source") or "") == PRICE_SOURCE_DAILY_CLOSE
                    else []
                )
            }
        )
        if sellable_meta.get("sellable_diverges"):
            item_warnings.append("sellable_quantity_diverges")
        items.append(
            {
                "order_id": signal.get("order_id"),
                "symbol": signal.get("symbol"),
                "action": signal.get("action"),
                "size": signal.get("size"),
                "price": signal.get("price"),
                "execution_phase": signal.get("execution_phase"),
                "execution_priority": signal.get("execution_priority"),
                "plan_item_rank": signal.get("plan_item_rank"),
                "reference_price": signal.get("reference_price"),
                "reference_price_source": signal.get("reference_price_source"),
                "max_slippage_bps": signal.get("max_slippage_bps"),
                "price_floor": signal.get("price_floor"),
                "price_ceiling": signal.get("price_ceiling"),
                "effective_limit_price": signal.get("effective_limit_price"),
                "valid_until": signal.get("valid_until"),
                "estimated_amount": amount,
                "blockers": sorted(set(blockers)),
                "warnings": item_warnings,
                **sellable_meta,
            }
        )

    return {
        "account": {
            "securities_account_id": str(account.get("_id")),
            "broker": account.get("broker"),
            "account_id": account.get("account_id"),
            "live_trading_enabled": _pp._account_live_enabled(account),
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


# Blockers intrinsic to a single symbol: the order can never publish in its
# current form (no price, halted/ST/limit-up, extreme volatility, single-order
# caps, or an unsellable position). Dropping such a signal only frees budget for
# the rest, so partial publish can batch-remove all of them safely.
_SYMBOL_INTRINSIC_BLOCKERS = frozenset(
    {
        "missing_price",
        "st_stock",
        "suspended",
        "limit_up",
        "extreme_volatility",
        "extreme_atr_volatility",
        "abnormal_intraday_range",
        "max_order_amount_exceeded",
        "max_position_pct_exceeded",
        "insufficient_lineage_position",
        "insufficient_available_position",
    }
)

# Cumulative budget blockers: whether a signal trips these depends on the other
# signals in the batch (running cash / daily-amount totals). They must be
# resolved by dropping the single lowest-priority offender and recomputing, not
# by batch-removing every flagged signal, so higher-ranked orders keep their
# claim on the shared budget.
_BUDGET_BLOCKERS = frozenset({"insufficient_cash", "max_daily_amount_exceeded"})

# Account-level blockers apply to every signal (live trading disabled or no
# broker binding). Partial publish is meaningless here — nothing can go out — so
# callers hard-block instead of silently publishing nothing.
_ACCOUNT_LEVEL_BLOCKERS = frozenset({"live_trading_disabled", "account_binding_missing"})


def _risk_report_has_account_level_block(risk_report: Dict[str, Any]) -> bool:
    return any(
        set(item.get("blockers") or []) & _ACCOUNT_LEVEL_BLOCKERS
        for item in risk_report.get("items", [])
    )


def _partition_publishable_live_signals(
    *,
    finance_collections: Dict[str, Any],
    data_collections: Dict[str, Any],
    account: Dict[str, Any],
    signals: List[Dict[str, Any]],
    user_id: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    """Split *signals* into a publishable subset and a skipped list.

    Symbol-intrinsic blockers (see ``_SYMBOL_INTRINSIC_BLOCKERS``) are removed in
    a batch because freeing their budget can only clear other blockers. The
    remaining cumulative budget blockers are resolved by dropping the single
    lowest-priority offender (last in rank order) and recomputing, so a lone
    expensive tail order yields to the rest of the book instead of over-skipping
    everything the running cash total happened to flag.

    Returns ``(publishable_signals, skipped_report_items, display_risk_report)``
    where ``display_risk_report`` merges the surviving (passing) items with the
    skipped (blocked) items in the original signal order, so a preview table can
    show exactly what will publish vs be skipped.
    """
    order_index = {signal.get("order_id"): idx for idx, signal in enumerate(signals)}
    survivors = list(signals)
    skipped_items: List[Dict[str, Any]] = []

    def _build(current: List[Dict[str, Any]]) -> Dict[str, Any]:
        return _pp._build_live_publish_risk_report(
            finance_collections=finance_collections,
            data_collections=data_collections,
            account=account,
            signals=current,
            user_id=user_id,
        )

    guard = len(signals) * 2 + 5
    survivor_report = _build(survivors)
    while guard > 0:
        guard -= 1
        blocked = [item for item in survivor_report["items"] if item.get("blockers")]
        if not blocked:
            break
        intrinsic = [
            item for item in blocked if set(item.get("blockers") or []) & _SYMBOL_INTRINSIC_BLOCKERS
        ]
        if intrinsic:
            dropped = intrinsic
        else:
            # Only cumulative budget blockers remain: drop the lowest-priority
            # offender so the recompute can clear the rest of the book.
            dropped = [blocked[-1]]
        drop_ids = {item.get("order_id") for item in dropped}
        skipped_items.extend(dropped)
        survivors = [s for s in survivors if s.get("order_id") not in drop_ids]
        if not survivors:
            survivor_report = _build([])
            break
        survivor_report = _build(survivors)

    display_items = list(survivor_report["items"]) + skipped_items
    display_items.sort(key=lambda item: order_index.get(item.get("order_id"), len(signals)))
    display_report = {
        **survivor_report,
        "items": display_items,
        "blocked_count": len(skipped_items),
    }
    return survivors, skipped_items, display_report


LIVE_ACTION_HARD_BLOCKERS = {"live_trading_disabled", "account_binding_missing"}


def _normalize_live_target_shares(
    current_shares: int,
    requested_target: int,
    lot_size: int,
    symbol: Optional[str] = None,
    price: Optional[float] = None,
    available_cash: Optional[float] = None,
) -> Tuple[int, List[str]]:
    """Snap a manual target to tradable lots; clearing to zero keeps a full exit.

    Mirrors tools.portfolio_trading for non-STAR boards. STAR market (科创板)
    symbols delegate to the shared ``normalize_board_target_shares`` so the
    200-share single-order minimum is honored (bump/skip/full-liquidate).
    """
    if symbol is not None and is_star_market(symbol):
        return normalize_board_target_shares(
            current_shares,
            requested_target,
            price,
            lot_size,
            symbol,
            available_cash=available_cash,
        )
    current = max(0, int(current_shares or 0))
    requested = max(0, int(requested_target or 0))
    lot = max(1, int(lot_size or 1))
    blockers: List[str] = []
    if requested == current:
        return current, blockers
    if requested == 0:
        return 0, blockers
    delta = requested - current
    if delta > 0:
        rounded_delta = (delta // lot) * lot
        if rounded_delta <= 0:
            blockers.append("below_lot_size")
            return current, blockers
        return current + rounded_delta, blockers
    sell_qty = (abs(delta) // lot) * lot
    if sell_qty <= 0:
        blockers.append("below_lot_size")
        return current, blockers
    return current - sell_qty, blockers


def _dropped_target_record(
    symbol: str,
    reason_code: str,
    reason: str,
    *,
    current_shares: int,
    requested_target_shares: int,
    account_current_shares: Optional[int] = None,
    account_available_shares: Optional[int] = None,
    blockers: Optional[List[str]] = None,
    sellable_observability: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Describe a requested target that produced no order.

    A dropped reduction may be a stop-loss that never reached the broker, so
    callers must be able to tell it apart from a target that was already met.
    """
    record = {
        "symbol": symbol,
        "reason_code": reason_code,
        "reason": reason,
        "current_shares": current_shares,
        "requested_target_shares": requested_target_shares,
        "account_current_shares": account_current_shares,
        "account_available_shares": account_available_shares,
        "blockers": list(blockers or []),
    }
    if sellable_observability:
        record.update(sellable_observability)
    return record


def _live_signal_is_blocked(item: Dict[str, Any]) -> bool:
    """Whether a discretionary live order should abort publishing.

    Sells abort only on hard account-config blockers; buys abort on any blocker.
    """
    blockers = item.get("blockers") or []
    if not blockers:
        return False
    if str(item.get("action")) == "sell":
        return any(blocker in LIVE_ACTION_HARD_BLOCKERS for blocker in blockers)
    return True


@router.post("/plans/{plan_id}/live-rebalance")
async def live_rebalance_portfolio(
    plan_id: str,
    request: LiveRebalanceRequest,
    current_user: dict = Depends(get_current_user),
):
    """One-click intraday LIVE position change (reduce / clear / add) for a lineage.

    A discretionary, human-driven rebalance: reads the lineage's live holdings
    (not the paper snapshot), computes per-symbol deltas from ``targets``, creates
    a manual (origin=manual) plan for audit, and publishes buy/sell live signals
    straight into ``trade_signals`` synchronously. The quantTrader loop (poll ~1s)
    picks them up and submits to the broker within the session, so a discretionary
    "trade now" no longer waits for the next-day open cron. Liquidation is just
    every held symbol mapped to 0.
    """
    trading_collections = _pp.get_trading_db_collections()
    finance_collections = _pp.get_db_collections()
    data_collections = _pp.get_data_db()
    user_id = _pp._current_user_id(current_user)
    publish_started = time.perf_counter()

    plan = _pp._require_plan(trading_collections, plan_id, current_user)
    if not request.targets:
        raise HTTPException(status_code=400, detail="targets must include at least one symbol")
    requested_plan_type = str(request.plan_type or PLAN_TYPE_REBALANCE).strip()
    if requested_plan_type not in TRADE_PLAN_PLAN_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported plan_type: {requested_plan_type!r}",
        )
    account = _pp._require_securities_account(finance_collections, request.securities_account_id, user_id)
    if not _pp._account_live_enabled(account):
        raise HTTPException(
            status_code=409,
            detail="securities account is not enabled for live trading",
        )

    strategy_template_id = str(plan.get("strategy_template_id") or "")
    params_hash = str(plan.get("params_hash") or "")
    if not strategy_template_id:
        raise HTTPException(status_code=400, detail="plan is missing strategy_template_id")

    # Resolved here rather than after the loop below: an "account"-basis read
    # needs the account up front. Callers that derived their absolute targets
    # from a broker-side position (intraday T+0) must be read back the same
    # way, or the expected_current_shares guard 409s on every single leg.
    securities_account_id = str(account.get("_id") or request.securities_account_id)
    if request.basis == "account":
        holdings = _pp._account_level_live_holdings(
            finance_collections,
            data_collections,
            user_id=user_id,
            securities_account_id=securities_account_id,
        )
    else:
        holdings = _pp._lineage_live_holdings(
            trading_collections, finance_collections, data_collections, plan, user_id
        )
    current_by_key: Dict[str, Dict[str, Any]] = {}
    for row in holdings:
        current_by_key[_symbol_key(row.get("symbol"))] = row

    # Reject targets computed from a holdings read that is no longer current.
    # Absolute targets against a moved basis trade the wrong quantity, e.g. an
    # intraday T+0 leg filling between the caller's read and this one.
    stale_targets: List[Dict[str, Any]] = []
    for symbol, expected in (request.expected_current_shares or {}).items():
        actual_shares = _int_value((current_by_key.get(_symbol_key(symbol)) or {}).get("shares"))
        if _int_value(expected) != actual_shares:
            stale_targets.append(
                {
                    "symbol": symbol,
                    "expected_current_shares": _int_value(expected),
                    "actual_current_shares": actual_shares,
                }
            )
    if stale_targets:
        log.warning(
            "live-rebalance stale basis anchor=%s account=%s %s",
            plan_id,
            request.securities_account_id,
            ",".join(
                f"{row['symbol']}:{row['expected_current_shares']}!={row['actual_current_shares']}"
                for row in stale_targets
            ),
        )
        raise HTTPException(
            status_code=409,
            detail={
                "message": "live holdings changed since targets were computed",
                "stale_targets": stale_targets,
            },
        )

    account_positions_by_key, account_positions_synced = _pp._account_positions_by_symbol(
        finance_collections,
        user_id=user_id,
        securities_account_id=securities_account_id,
    )

    # Resolve a price for every symbol we may trade (held + newly-added targets).
    target_symbols = {str(s) for s in request.targets.keys() if s}
    all_symbols = sorted(target_symbols | {str(row.get("symbol")) for row in holdings})
    price_map, _realtime_docs = _pp._load_fresh_market_prices(data_collections, all_symbols)

    params_snapshot = plan.get("params_snapshot") or {}
    lot_size = _int_value(params_snapshot.get("lot_size"), 100) or 100

    now = datetime.utcnow()
    base_date = _business_today_yyyymmdd()
    stamp = now.strftime("%Y%m%d%H%M%S")
    new_plan_id = f"{strategy_template_id}_{(params_hash or 'na')[:8]}_{user_id}_liverb_{stamp}{uuid.uuid4().hex[:6]}"

    items: List[Dict[str, Any]] = []
    manual_exclude_symbols: List[str] = []
    dropped_targets: List[Dict[str, Any]] = []
    estimated_buy = 0.0
    estimated_sell = 0.0
    rank = 0
    for symbol in sorted(target_symbols):
        symbol_key = _symbol_key(symbol)
        held = current_by_key.get(symbol_key) or {}
        current_shares = _int_value(held.get("shares"))
        target_shares, blockers = _normalize_live_target_shares(
            current_shares,
            request.targets[symbol],
            lot_size,
            symbol=symbol,
            price=_numeric(price_map.get(symbol)) or _numeric(price_map.get(symbol_key)),
        )
        delta = target_shares - current_shares
        account_pos = account_positions_by_key.get(symbol_key) or {"current": 0, "available": 0}
        account_current_shares = account_pos["current"] if account_positions_synced else None
        account_available_shares = account_pos["available"] if account_positions_synced else None
        sellable_meta = _pp._sellable_observability_from_bucket(account_pos) if account_positions_synced else {}
        if delta < 0 and account_positions_synced:
            if account_current_shares <= 0 or account_available_shares <= 0:
                # The broker account has synced and says this symbol is not
                # sellable. The lineage ledger may be stale after external
                # clean-up, so never generate a sell for shares the account
                # does not currently hold.
                dropped_targets.append(
                    _dropped_target_record(
                        symbol,
                        "account_position_not_sellable",
                        "broker account reports no sellable shares for this symbol",
                        current_shares=current_shares,
                        requested_target_shares=_int_value(request.targets[symbol]),
                        account_current_shares=account_current_shares,
                        account_available_shares=account_available_shares,
                        blockers=blockers,
                        sellable_observability=sellable_meta,
                    )
                )
                continue
            sell_qty = abs(delta)
            capped_sell_qty = min(sell_qty, account_available_shares)
            if capped_sell_qty <= 0:
                dropped_targets.append(
                    _dropped_target_record(
                        symbol,
                        "sell_capped_to_zero",
                        "available broker quantity left nothing to sell",
                        current_shares=current_shares,
                        requested_target_shares=_int_value(request.targets[symbol]),
                        account_current_shares=account_current_shares,
                        account_available_shares=account_available_shares,
                        blockers=blockers,
                        sellable_observability=sellable_meta,
                    )
                )
                continue
            if capped_sell_qty < sell_qty:
                delta = -capped_sell_qty
                target_shares = current_shares + delta
                blockers.append("account_position_capped")
        if delta == 0:
            if blockers:
                dropped_targets.append(
                    _dropped_target_record(
                        symbol,
                        "normalized_to_no_change",
                        "requested change did not survive lot-size normalization",
                        current_shares=current_shares,
                        requested_target_shares=_int_value(request.targets[symbol]),
                        account_current_shares=account_current_shares,
                        account_available_shares=account_available_shares,
                        blockers=blockers,
                        sellable_observability=sellable_meta,
                    )
                )
            elif symbol_key not in current_by_key:
                # The basis holds no position for this symbol at all, so every
                # absolute target collapses to a zero delta. That is what a
                # target derived from the *broker* book looks like when those
                # shares never entered the ledger — a purely manual holding,
                # say. Without this record the publish returned an empty
                # dropped_targets list and read as "already in place" instead
                # of "I cannot see this symbol".
                dropped_targets.append(
                    _dropped_target_record(
                        symbol,
                        "not_in_basis",
                        "symbol has no live position in the basis this publish reads",
                        current_shares=current_shares,
                        requested_target_shares=_int_value(request.targets[symbol]),
                        account_current_shares=account_current_shares,
                        account_available_shares=account_available_shares,
                        blockers=blockers,
                        sellable_observability=sellable_meta,
                    )
                )
            continue
        rank += 1
        price = (
            _numeric(price_map.get(symbol))
            or _numeric(price_map.get(_symbol_key(symbol)))
            or _numeric(held.get("last_price"))
            or None
        )
        item_blockers = list(blockers)
        if not price:
            item_blockers.append("missing_price")
        if delta > 0:
            action = "buy"
            estimated_buy += abs(delta) * float(price or 0)
        else:
            action = "sell"
            estimated_sell += abs(delta) * float(price or 0)
        if target_shares < current_shares and request.exclude_after:
            manual_exclude_symbols.append(symbol)
        items.append(
            {
                "plan_id": new_plan_id,
                "strategy_template_id": strategy_template_id,
                "user_id": user_id,
                "params_hash": params_hash,
                "base_date": base_date,
                "execute_date": base_date,
                "rank": rank,
                "symbol": symbol,
                "name": held.get("name", ""),
                "action": action,
                "current_shares": current_shares,
                "target_shares": target_shares,
                "delta_shares": delta,
                "drift_delta_shares": delta,
                "account_current_shares": account_current_shares,
                "account_available_shares": account_available_shares,
                "estimated_price": price,
                "estimated_amount": abs(delta) * float(price or 0) if price else None,
                "blockers": item_blockers,
                "created_at": now,
                **sellable_meta,
            }
        )

    capped_symbols = sorted(
        {item["symbol"] for item in items if "account_position_capped" in (item.get("blockers") or [])}
    )
    if dropped_targets or capped_symbols:
        log.warning(
            "live-rebalance anchor=%s account=%s dropped=%s capped=%s",
            plan_id,
            securities_account_id,
            ",".join(f"{row['symbol']}:{row['reason_code']}" for row in dropped_targets) or "-",
            ",".join(capped_symbols) or "-",
        )

    if not items:
        reason_summary = ", ".join(f"{row['symbol']}({row['reason_code']})" for row in dropped_targets)
        raise HTTPException(
            status_code=400,
            detail={
                "message": (
                    f"targets produced no executable changes: {reason_summary}"
                    if reason_summary
                    else "targets produced no executable changes"
                ),
                "dropped_targets": dropped_targets,
            },
        )

    is_liquidation = all(item["action"] == "sell" and item["target_shares"] == 0 for item in items)
    manual_action = "liquidate" if is_liquidation else "rebalance"

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
        "plan_type": requested_plan_type,
        "record_kind": RECORD_KIND_TRADE_PLAN,
        # origin stays "manual": downstream uses is_strategy_decision() /
        # strategy_decision_mongo_filter() to exclude discretionary plans from
        # cadence, trailing-stop, and supersede queries.
        "origin": "manual",
        "manual_action": manual_action,
        "is_liquidation": is_liquidation,
        "is_rebalance_day": requested_plan_type == PLAN_TYPE_REBALANCE,
        "monitor_can_trade": False,
        "trading_enabled": True,
        "executable": True,
        "non_executable_reason": None,
        "exclude_after": bool(request.exclude_after),
        "manual_exclude_symbols": sorted(set(manual_exclude_symbols)),
        "status": "approved",
        "approved_by": user_id,
        "approved_at": now,
        "anchor_plan_id": plan_id,
        "t0_signal_id": str(request.t0_signal_id or "").strip() or None,
        "summary": {
            "buy_count": sum(1 for item in items if item["action"] == "buy"),
            "sell_count": sum(1 for item in items if item["action"] == "sell"),
            "hold_count": 0,
            "skip_count": len(dropped_targets),
            "estimated_buy_amount": estimated_buy,
            "estimated_sell_amount": estimated_sell,
            "manual_action": manual_action,
        },
        "dropped_targets": dropped_targets,
        "capped_symbols": capped_symbols,
        "created_at": now,
        "updated_at": now,
    }

    signals: List[Dict[str, Any]] = []
    skipped_items: List[Dict[str, Any]] = list(dropped_targets)
    for item in items:
        try:
            signal = build_portfolio_plan_live_signal(plan=plan_doc, item=item, account=account, now=now)
        except Exception as exc:  # noqa: BLE001
            skipped_items.append(
                {
                    "symbol": item.get("symbol"),
                    "reason_code": "signal_build_failed",
                    "reason": str(exc),
                }
            )
            continue
        if signal is not None:
            signals.append(signal.to_mongo())

    t0_signal_id = str(request.t0_signal_id or "").strip()
    if t0_signal_id:
        stamp_t0_correlation(signals, t0_signal_id)

    if not signals:
        raise HTTPException(
            status_code=409,
            detail={"message": "no live signals could be built", "skipped_items": skipped_items},
        )

    if request.dry_run:
        risk_report = _pp._build_live_publish_risk_report(
            finance_collections=finance_collections,
            data_collections=data_collections,
            account=account,
            signals=signals,
            user_id=user_id,
        )
        blocked = [item for item in risk_report["items"] if _live_signal_is_blocked(item)]
    else:
        # Manual quick actions already present this report in dry-run preview.
        # Once a human confirms the trade, keep the hot path to hard checks above
        # (lot normalization, sellable broker quantity, price/order build) and the
        # signal writes below.
        risk_report = {
            "items": [],
            "blocked_count": 0,
            "skipped": True,
            "skip_reason": "manual_confirm_after_preview",
        }
        blocked = []

    order_ids = [signal["order_id"] for signal in signals]
    existing_signals = list(
        finance_collections["trade_signals"].find(active_signal_query_for_order_ids(user_id, order_ids))
    ) if order_ids else []
    existing_order_ids = {row.get("order_id") for row in existing_signals}
    new_signals = [signal for signal in signals if signal.get("order_id") not in existing_order_ids]

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
                    "signals": signals,
                    "new_signals": new_signals,
                    "existing_signals": existing_signals,
                    "skipped_items": skipped_items,
                    "dropped_targets": dropped_targets,
                    "capped_symbols": capped_symbols,
                    "risk_report": risk_report,
                    "manual_action": manual_action,
                    "changed_symbols": changed_symbols,
                    "blocked": bool(blocked),
                }
            ),
        }

    trading_collections["portfolio_trade_plans"].insert_one(dict(plan_doc))
    trading_collections["portfolio_trade_plan_items"].insert_many([dict(item) for item in items])

    inserted_count = 0
    if new_signals:
        finance_collections["trade_signals"].insert_many(new_signals)
        inserted_count = len(new_signals)
    publish_duration_ms = round((time.perf_counter() - publish_started) * 1000.0, 1)
    trading_collections["portfolio_trade_plans"].update_one(
        {"plan_id": new_plan_id, "user_id": user_id},
        {
            "$set": {
                "live_signals_published_at": now,
                "live_signal_count": len(signals),
                "live_signal_inserted_count": inserted_count,
                "publish_duration_ms": publish_duration_ms,
                "live_signal_status_summary": {
                    "pending": inserted_count,
                    **summarize_signal_statuses(existing_signals),
                },
            }
        },
    )
    _pp._append_plan_operation_log(
        trading_collections,
        plan_id=new_plan_id,
        user_id=user_id,
        operation_type="live_rebalance_publish",
        status="partial" if dropped_targets or capped_symbols else "success",
        message="manual live rebalance signals published",
        payload={
            "anchor_plan_id": plan_id,
            "manual_action": manual_action,
            "signal_count": len(signals),
            "inserted_count": inserted_count,
            "existing_count": len(existing_signals),
            "publish_duration_ms": publish_duration_ms,
            "dropped_targets": dropped_targets,
            "capped_symbols": capped_symbols,
        },
    )
    _pp.schedule_snapshot_rebuild(
        trading_collections,
        finance_collections,
        plan_doc,
        user_id,
        reason="live_rebalance_publish",
    )

    # Forward-looking exclusion only; pausing the lineage waits until the sells
    # actually settle (mirrors publish-live-signals, see Error 8 in design notes).
    final_shares_by_key = {_symbol_key(row.get("symbol")): _int_value(row.get("shares")) for row in holdings}
    for item in items:
        final_shares_by_key[_symbol_key(item["symbol"])] = _int_value(item["target_shares"])
    lineage_effects = _pp._apply_manual_lineage_effects_native(
        trading_collections,
        plan_doc,
        final_positions=[{"symbol": key, "shares": shares} for key, shares in final_shares_by_key.items()],
        acted_by=user_id,
        pause_on_empty=False,
    )

    return {
        "success": True,
        "data": _jsonable(
            {
                "dry_run": False,
                "plan_id": new_plan_id,
                "manual_action": manual_action,
                "signals": signals,
                "new_signals": new_signals,
                "existing_signals": existing_signals,
                "skipped_items": skipped_items,
                "dropped_targets": dropped_targets,
                "capped_symbols": capped_symbols,
                "risk_report": risk_report,
                "inserted_count": inserted_count,
                "existing_count": len(existing_signals),
                "publish_duration_ms": publish_duration_ms,
                "changed_symbols": changed_symbols,
                "blocked": bool(blocked),
                "lineage_effects": lineage_effects,
            }
        ),
    }


def _persist_repriced_plan_items(
    trading_collections: Dict[str, Any],
    *,
    plan_id: str,
    user_id: str,
    items: List[Dict[str, Any]],
    reprice_summary: Dict[str, Any],
    now: datetime,
) -> None:
    for item in items:
        symbol = str(item.get("symbol") or "")
        if not symbol:
            continue
        query = {"plan_id": plan_id, "user_id": user_id, "symbol": symbol}
        existing = trading_collections["portfolio_trade_plan_items"].find_one(query)
        set_fields = {
            "current_shares": _int_value(item.get("current_shares")),
            "target_shares": _int_value(item.get("target_shares")),
            "delta_shares": _int_value(item.get("delta_shares")),
            "action": item.get("action"),
            "estimated_price": item.get("estimated_price"),
            "estimated_amount": item.get("estimated_amount"),
            "price_source": item.get("price_source"),
            "price_as_of": item.get("price_as_of"),
            "blockers": item.get("blockers") or [],
            "warnings": item.get("warnings") or [],
            "estimated_price_at_approval": item.get("estimated_price"),
            "updated_at": now,
        }
        if existing and not existing.get("estimated_price_at_generation"):
            set_fields["estimated_price_at_generation"] = existing.get("estimated_price")
        trading_collections["portfolio_trade_plan_items"].update_one(query, {"$set": set_fields})
    trading_collections["portfolio_trade_plans"].update_one(
        {"plan_id": plan_id, "user_id": user_id},
        {
            "$set": {
                "repriced_at": now,
                "reprice_summary": reprice_summary,
                "updated_at": now,
            }
        },
    )


@router.post("/plans/{plan_id}/publish-live-signals")
@_offload
def publish_portfolio_plan_live_signals(
    plan_id: str,
    request: PublishLiveSignalsRequest,
    current_user: dict = Depends(get_current_user),
):
    _publish_started = time.perf_counter()
    trading_collections = _pp.get_trading_db_collections()
    finance_collections = _pp.get_db_collections()
    data_collections = _pp.get_data_db()
    user_id = _pp._current_user_id(current_user)
    plan = _pp._require_plan(trading_collections, plan_id, current_user)
    if plan.get("status") != "approved":
        raise HTTPException(status_code=400, detail="plan must be approved before publishing live signals")
    if plan.get("paper_executed_at") or trading_collections["portfolio_paper_executions"].find_one({"plan_id": plan_id, "user_id": user_id}):
        raise HTTPException(status_code=400, detail="plan already has paper executions; cannot publish live signals")
    account = _pp._require_securities_account(finance_collections, request.securities_account_id, user_id)
    items = list(
        trading_collections["portfolio_trade_plan_items"]
        .find({"plan_id": plan_id, "user_id": user_id})
        .sort("rank", 1)
    )
    # Carry the plan/lineage book holdings onto each item (generation snapshot plus
    # this lineage's own live fills) so repricing sizes deltas against what THIS
    # plan owns, not the shared broker account aggregate.
    items = _pp._attach_live_plan_item_context(
        finance_collections=finance_collections,
        plan=plan,
        items=items,
        user_id=user_id,
    )
    now = datetime.utcnow()
    account_snapshot = _pp._latest_account_snapshot(finance_collections, account, user_id)
    account_equity = _numeric(account_snapshot.get("total_assets") or account_snapshot.get("total_asset"))
    account_available_cash = _numeric(account_snapshot.get("available_cash") or account_snapshot.get("cash"))
    # Sizing budget comes from the plan's own book (see _resolve_publish_plan_capital);
    # the account snapshot above is kept only for broker-side execution risk checks.
    plan_capital = _pp._resolve_publish_plan_capital(plan)
    plan_equity = plan_capital.get("equity")
    plan_cash = plan_capital.get("cash")
    capital_blockers: List[str] = []
    if plan_equity is None:
        capital_blockers.append("missing_plan_equity")
        plan_equity = 0.0
    if plan_cash is None:
        capital_blockers.append("missing_plan_cash")
        plan_cash = 0.0
    securities_account_id = str(account.get("_id") or request.securities_account_id)
    account_positions_by_key, account_positions_synced = _pp._account_positions_by_symbol(
        finance_collections,
        user_id=user_id,
        securities_account_id=securities_account_id,
    )
    params_snapshot = plan.get("params_snapshot") or {}
    lot_size = _int_value(params_snapshot.get("lot_size"), 100) or 100
    symbols = sorted({str(item.get("symbol")) for item in items if item.get("symbol")})
    quotes = _pp._market_price_quotes(data_collections, symbols)
    repriced_items, reprice_summary = reprice_plan_items_for_publish(
        plan=plan,
        items=items,
        quotes=quotes,
        equity=plan_equity,
        lot_size=lot_size,
        account_positions_by_key=account_positions_by_key,
        account_positions_synced=account_positions_synced,
        normalize_live_target_shares=_normalize_live_target_shares,
        int_value=_int_value,
        numeric=_numeric,
        symbol_key=_symbol_key,
        # plan_cash is always a number (fallback to initial_capital), so the buy
        # cash cap is always active here -- even when the broker account is not
        # synced. This soft-caps buys to the plan budget and tags them with an
        # item-level ``insufficient_cash`` blocker. The broker account's real cash
        # is enforced separately (hard 409) in _build_live_publish_risk_report;
        # the two share the ``insufficient_cash`` label today (TODO: split into
        # plan_budget_capped vs account_cash_insufficient).
        available_cash=plan_cash,
    )
    blockers = set(reprice_summary.get("blockers") or [])
    blockers.update(capital_blockers)
    reprice_summary["blockers"] = sorted(blockers)
    reprice_summary["equity_used"] = float(plan_equity)
    reprice_summary["plan_equity_used"] = float(plan_equity)
    reprice_summary["plan_cash_used"] = float(plan_cash)
    reprice_summary["plan_equity_source"] = plan_capital.get("equity_source")
    reprice_summary["plan_cash_source"] = plan_capital.get("cash_source")
    reprice_summary["account_equity"] = account_equity
    reprice_summary["account_available_cash"] = account_available_cash
    reprice_summary["capital_basis"] = plan_capital.get("capital_basis")
    reprice_blocker_set = set(reprice_summary.get("blockers") or [])
    # Capital-baseline blockers make every size unreliable, so they hard-block
    # even under allow_partial. ``missing_price`` is per-symbol: under
    # allow_partial let it flow to the risk stage so only those symbols are
    # skipped, not the whole plan.
    hard_reprice_blockers = reprice_blocker_set & {"missing_plan_equity", "missing_plan_cash"}
    block_on_reprice = bool(hard_reprice_blockers) if request.allow_partial else bool(reprice_blocker_set)
    if block_on_reprice and not request.dry_run:
        message = "publish repricing blocked"
        if reprice_blocker_set == {"missing_price"}:
            message = "publish repricing blocked: missing usable prices"
        elif hard_reprice_blockers:
            message = "publish repricing blocked: missing plan capital baseline"
        raise HTTPException(
            status_code=409,
            detail={
                "message": message,
                "reprice_summary": reprice_summary,
            },
        )
    items = repriced_items
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

    if not signals:
        if request.dry_run:
            return {
                "success": True,
                "data": _jsonable(
                    {
                        "plan_id": plan_id,
                        "dry_run": True,
                        "signals": [],
                        "new_signals": [],
                        "existing_signals": [],
                        "skipped_items": skipped_items,
                        "skipped_by_risk": [],
                        "risk_report": {"items": [], "blocked_count": len(reprice_summary.get("blockers") or [])},
                        "reprice_summary": reprice_summary,
                        "inserted_count": 0,
                        "existing_count": 0,
                        "publishable_count": 0,
                        "allow_partial": request.allow_partial,
                        "partial": False,
                        "blocked": bool(reprice_summary.get("blockers")),
                    }
                ),
            }
        raise HTTPException(
            status_code=409,
            detail={"message": "no live signals could be built", "skipped_items": skipped_items},
        )

    order_ids = [signal["order_id"] for signal in signals]
    existing_signals = list(
        finance_collections["trade_signals"].find(active_signal_query_for_order_ids(user_id, order_ids))
    ) if order_ids else []
    existing_order_ids = {row.get("order_id") for row in existing_signals}
    new_signals = [signal for signal in signals if signal.get("order_id") not in existing_order_ids]
    risk_report = _pp._build_live_publish_risk_report(
        finance_collections=finance_collections,
        data_collections=data_collections,
        account=account,
        signals=new_signals,
        user_id=user_id,
        trading_collections=trading_collections,
        plan=plan,
    )
    risk_blocked = [item for item in risk_report["items"] if item["blockers"]]
    account_level_blocked = _risk_report_has_account_level_block(risk_report)
    # publish_new_signals is the subset actually written. Under all-or-nothing it
    # is either the full new_signals set or (on confirm) we 409 before writing.
    publish_new_signals = new_signals
    skipped_by_risk: List[Dict[str, Any]] = []

    if risk_blocked:
        if not request.allow_partial or account_level_blocked:
            # All-or-nothing: any risk blocker (or an account-level block that no
            # per-symbol skip could clear) fails the entire publish.
            if not request.dry_run:
                raise HTTPException(
                    status_code=409,
                    detail=_jsonable({"message": "live signal risk check failed", "risk_report": risk_report}),
                )
            publish_new_signals = []
        else:
            # Partial: publish the passing signals, skip only the blocked symbols.
            publish_new_signals, skipped_by_risk, risk_report = _partition_publishable_live_signals(
                finance_collections=finance_collections,
                data_collections=data_collections,
                account=account,
                signals=new_signals,
                user_id=user_id,
            )
            if not publish_new_signals and not request.dry_run:
                raise HTTPException(
                    status_code=409,
                    detail=_jsonable(
                        {
                            "message": "all live signals were blocked; nothing to publish",
                            "risk_report": risk_report,
                            "skipped_by_risk": skipped_by_risk,
                        }
                    ),
                )

    partial = bool(skipped_by_risk) and bool(publish_new_signals)
    # ``blocked`` means a confirm would publish nothing. Under all-or-nothing it
    # is any risk/reprice blocker; under allow_partial only a fully-empty publish
    # (or a hard account/capital block) counts, so a dry-run preview never shows
    # "ok" for a publish that would actually fail.
    if request.allow_partial:
        response_blocked = (
            bool(hard_reprice_blockers)
            or account_level_blocked
            or (bool(risk_blocked) and not publish_new_signals)
        )
    else:
        response_blocked = bool(risk_blocked) or bool(reprice_summary.get("blockers"))

    inserted_count = 0
    if not request.dry_run and publish_new_signals:
        _persist_repriced_plan_items(
            trading_collections,
            plan_id=plan_id,
            user_id=user_id,
            items=repriced_items,
            reprice_summary=reprice_summary,
            now=now,
        )
        _pp._append_plan_operation_log(
            trading_collections,
            plan_id=plan_id,
            user_id=user_id,
            operation_type="publish_reprice",
            status="success",
            message="plan repriced with latest market quotes before live publish",
            payload={
                "equity_used": reprice_summary.get("equity_used"),
                "plan_equity_used": reprice_summary.get("plan_equity_used"),
                "plan_cash_used": reprice_summary.get("plan_cash_used"),
                "account_available_cash": reprice_summary.get("account_available_cash"),
                "turnover_drift_pct": reprice_summary.get("turnover_drift_pct"),
                "stale_price_symbols": reprice_summary.get("stale_price_symbols"),
                "signal_count": len(publish_new_signals),
                "publish_duration_ms": round((time.perf_counter() - _publish_started) * 1000.0, 1),
            },
        )
        finance_collections["trade_signals"].insert_many(publish_new_signals)
        inserted_count = len(publish_new_signals)
        mark_lineage_live(
            trading_collections.get("portfolio_lineage_controls"),
            user_id=user_id,
            strategy_template_id=str(plan.get("strategy_template_id") or ""),
            params_hash=str(plan.get("params_hash") or ""),
            securities_account_id=securities_account_id,
        )
        trading_collections["portfolio_trade_plans"].update_one(
            {"plan_id": plan_id, "user_id": user_id},
            {
                "$set": {
                    "live_signals_published_at": now,
                    "live_signal_count": inserted_count + len(existing_signals),
                    "live_signal_inserted_count": inserted_count,
                    "live_signal_status_summary": {"pending": inserted_count, **summarize_signal_statuses(existing_signals)},
                }
            },
        )
        if plan.get("origin") == "manual":
            # Publishing live signals does NOT mean the orders filled. Only apply the
            # forward-looking exclusion here; pausing the lineage (which would stop
            # daily maintenance of a real book) must wait until fills are confirmed.
            # For a partial publish, symbols we skipped were NOT traded, so keep
            # them at their current book shares rather than their (unpublished)
            # target, otherwise the lineage would wrongly treat them as acted.
            # skipped_by_risk is empty for a full publish, so this reduces to the
            # original all-target behavior.
            skipped_symbol_keys = {_symbol_key(s.get("symbol")) for s in skipped_by_risk}
            final_positions = [
                {
                    "symbol": item.get("symbol"),
                    "shares": _int_value(item.get("current_shares"))
                    if _symbol_key(item.get("symbol")) in skipped_symbol_keys
                    else _int_value(item.get("target_shares")),
                }
                for item in items
            ]
            _pp._apply_manual_lineage_effects_native(
                trading_collections,
                plan,
                final_positions=final_positions,
                acted_by=user_id,
                pause_on_empty=False,
            )
        _pp.schedule_snapshot_rebuild(
            trading_collections,
            finance_collections,
            plan,
            user_id,
            reason="manual_lineage_apply",
        )
        if symbols:
            capture_plan_risk_snapshot(
                trading_collections,
                plan_id=plan_id,
                user_id=user_id,
                decision_point="published",
                symbols=symbols,
                analyzer_db=_pp.get_analyzer_db(),
            )

    return {
        "success": True,
        "data": _jsonable(
            {
                "plan_id": plan_id,
                "dry_run": request.dry_run,
                "signals": signals,
                "new_signals": new_signals,
                "publishable_signals": publish_new_signals,
                "existing_signals": existing_signals,
                "skipped_items": skipped_items,
                "skipped_by_risk": skipped_by_risk,
                "risk_report": risk_report,
                "reprice_summary": reprice_summary,
                "inserted_count": inserted_count,
                "existing_count": len(existing_signals),
                "publishable_count": len(publish_new_signals),
                "allow_partial": request.allow_partial,
                "partial": partial,
                "blocked": response_blocked,
            }
        ),
    }


@router.post("/plans/{plan_id}/replan-remainder")
@_offload
def replan_portfolio_plan_remainder(
    plan_id: str,
    request: ReplanRemainderRequest,
    current_user: dict = Depends(get_current_user),
):
    """Gap-fill a live plan whose execution fell short (cancelled / partial).

    Computes, against this plan's **lineage book** baseline, the remaining shares
    needed to finish what the plan intended to trade — capped by the planned
    per-position budget and plan-book available cash — and (after dry-run
    confirmation) publishes ``plan_topup`` signals. It never re-selects stocks;
    it only finishes the original selection.
    """
    trading_collections = _pp.get_trading_db_collections()
    finance_collections = _pp.get_db_collections()
    data_collections = _pp.get_data_db()
    user_id = _pp._current_user_id(current_user)

    plan = _pp._require_plan(trading_collections, plan_id, current_user)
    if plan.get("status") != "approved":
        raise HTTPException(status_code=400, detail="plan must be approved to reconcile remainder")
    if not plan.get("live_signals_published_at") and _int_value(plan.get("live_signal_count")) <= 0:
        raise HTTPException(status_code=400, detail="plan has no live signals to reconcile")

    account = _pp._require_securities_account(finance_collections, request.securities_account_id, user_id)
    account_snapshot = _pp._latest_account_snapshot(finance_collections, account, user_id)
    account_synced = bool(account_snapshot)
    broker_available_cash = _numeric(
        account_snapshot.get("available_cash") or account_snapshot.get("cash")
    ) if account_snapshot else None

    cash_snapshot = _pp._resolve_lineage_available_cash_for_remainder(
        trading_collections=trading_collections,
        finance_collections=finance_collections,
        data_collections=data_collections,
        plan=plan,
        user_id=user_id,
    )
    cash_snapshot["broker_available_cash"] = broker_available_cash
    plan_available_cash = cash_snapshot.get("plan_available_cash")
    cash_blockers = list(cash_snapshot.get("blockers") or [])

    items = list(
        trading_collections["portfolio_trade_plan_items"]
        .find({"plan_id": plan_id, "user_id": user_id})
        .sort("rank", 1)
    )
    enriched_items = _pp._attach_live_plan_item_context(
        finance_collections=finance_collections,
        plan=plan,
        items=items,
        user_id=user_id,
    )

    # Symbols with an in-flight (non-terminal) live signal for this plan: never
    # double-send while an order is still working.
    active_rows = finance_collections["trade_signals"].find(
        {
            "user_id": user_id,
            "plan_id": plan_id,
            "mode": "live",
            "status": {"$in": sorted(ACTIVE_SIGNAL_STATUSES)},
        },
        {"_id": 0, "symbol": 1},
    )
    active_symbols = {_symbol_key(row.get("symbol")) for row in active_rows if row.get("symbol")}

    # Cancelled / terminal originals per symbol, surfaced as replaces_order_ids.
    replaced_rows = finance_collections["trade_signals"].find(
        {
            "user_id": user_id,
            "plan_id": plan_id,
            "mode": "live",
            "status": {"$in": ["cancelled", "canceled", "partial_cancelled", "failed", "rejected"]},
        },
        {"_id": 0, "order_id": 1, "symbol": 1},
    )
    replaces_by_symbol: Dict[str, List[str]] = {}
    for row in replaced_rows:
        sym = _symbol_key(row.get("symbol"))
        if sym and row.get("order_id"):
            replaces_by_symbol.setdefault(sym, []).append(str(row.get("order_id")))

    specs = build_remainder_specs(
        plan=plan,
        enriched_items=enriched_items,
        available_cash=plan_available_cash,
        account_synced=account_synced,
        active_symbols=active_symbols,
        price_resolver=_pp.resolve_market_reference_price,
    )

    now = datetime.utcnow()
    signals: List[Dict[str, Any]] = []
    for spec in specs:
        if not spec.actionable:
            continue
        try:
            signals.append(
                build_topup_signal_doc(
                    plan=plan,
                    account=account,
                    spec=spec,
                    now=now,
                    user_id=user_id,
                    reason=request.reason,
                    replaces_order_ids=replaces_by_symbol.get(_symbol_key(spec.symbol), []),
                )
            )
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("Skipping remainder signal for %s: %s", spec.symbol, exc)

    risk_report = _pp._build_live_publish_risk_report(
        finance_collections=finance_collections,
        data_collections=data_collections,
        account=account,
        signals=signals,
        user_id=user_id,
    )
    blocked = [item for item in risk_report["items"] if item["blockers"]]
    account_level_blocked = _risk_report_has_account_level_block(risk_report)
    publish_signals = signals
    skipped_by_risk: List[Dict[str, Any]] = []

    # cash_blockers (e.g. missing_frozen_price) mean plan_available_cash may be
    # overestimated, so confirming could over-budget the top-up. These always
    # hard-block (even under allow_partial) because the budget itself is
    # unreliable; account-level blocks likewise can't be cleared by a skip.
    if cash_blockers or account_level_blocked or (blocked and not request.allow_partial):
        response_blocked = True
        if not request.dry_run:
            message = "remainder cash check failed" if cash_blockers else "remainder risk check failed"
            raise HTTPException(
                status_code=409,
                detail=_jsonable(
                    {
                        "message": message,
                        "risk_report": risk_report,
                        "cash_blockers": cash_blockers,
                    }
                ),
            )
    elif blocked:
        # Partial: publish the passing top-up signals, skip the blocked symbols.
        publish_signals, skipped_by_risk, risk_report = _partition_publishable_live_signals(
            finance_collections=finance_collections,
            data_collections=data_collections,
            account=account,
            signals=signals,
            user_id=user_id,
        )
        response_blocked = not publish_signals
        if not publish_signals and not request.dry_run:
            raise HTTPException(
                status_code=409,
                detail=_jsonable(
                    {
                        "message": "all remainder signals were blocked; nothing to publish",
                        "risk_report": risk_report,
                        "skipped_by_risk": skipped_by_risk,
                    }
                ),
            )
    else:
        response_blocked = False

    partial = bool(skipped_by_risk) and bool(publish_signals)

    inserted_count = 0
    if not request.dry_run and publish_signals:
        finance_collections["trade_signals"].insert_many(publish_signals)
        inserted_count = len(publish_signals)
        _append_remainder_audit(
            trading_collections,
            plan_id=plan_id,
            user_id=user_id,
            reviewer=str(current_user.get("username") or current_user.get("id") or ""),
            reason=request.reason,
            signals=publish_signals,
        )

    return {
        "success": True,
        "data": _jsonable(
            {
                "plan_id": plan_id,
                "dry_run": request.dry_run,
                "account_synced": account_synced,
                "available_cash": plan_available_cash,
                "plan_available_cash": plan_available_cash,
                "lineage_book_cash": cash_snapshot.get("lineage_book_cash"),
                "frozen_buy_cash": cash_snapshot.get("frozen_buy_cash"),
                "broker_available_cash": broker_available_cash,
                "cash_basis": cash_snapshot.get("cash_basis"),
                "missing_frozen_price_symbols": cash_snapshot.get("missing_frozen_price_symbols") or [],
                "cash_blockers": cash_blockers,
                "remainder": [spec.to_public() for spec in specs],
                "actionable_count": sum(1 for spec in specs if spec.actionable),
                "signals": signals,
                "publishable_signals": publish_signals,
                "skipped_by_risk": skipped_by_risk,
                "risk_report": risk_report,
                "inserted_count": inserted_count,
                "publishable_count": len(publish_signals),
                "allow_partial": request.allow_partial,
                "partial": partial,
                "blocked": response_blocked,
            }
        ),
    }


def _append_remainder_audit(
    trading_collections: Dict[str, Any],
    *,
    plan_id: str,
    user_id: str,
    reviewer: str,
    reason: str,
    signals: List[Dict[str, Any]],
) -> None:
    logs = trading_collections.get("portfolio_plan_operation_logs")
    if logs is None:
        return
    now = datetime.utcnow()
    logs.insert_one(
        {
            "log_id": str(uuid.uuid4()),
            "plan_id": plan_id or "",
            "user_id": user_id,
            "task_id": None,
            "operation_type": "plan_remainder_topup",
            "operation_symbol": None,
            "status": "success",
            "message": f"published {len(signals)} top-up signals",
            "payload": {
                "reviewer": reviewer,
                "reason": (reason or "")[:2000],
                "order_ids": [s.get("order_id") for s in signals],
                "symbols": [s.get("symbol") for s in signals],
            },
            "created_at": now,
            "updated_at": now,
        }
    )
