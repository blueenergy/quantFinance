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
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException

from stock_data_access.board_lots import (  # pyright: ignore[reportMissingImports]
    is_star_market,
    normalize_board_target_shares,
)

from auth import get_current_user
from services.live_trade_signals import (
    active_signal_query_for_order_ids,
    build_portfolio_plan_live_signal,
    summarize_signal_statuses,
)
from services.portfolio_plan_market import PRICE_SOURCE_DAILY_CLOSE
from services.portfolio_record_kind import RECORD_KIND_TRADE_PLAN

import routers.portfolio_plans as _pp
from . import LiveRebalanceRequest
from ._shared import (
    _business_today_yyyymmdd,
    _int_value,
    _jsonable,
    _numeric,
    _symbol_key,
)


router = APIRouter()


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
                "warnings": sorted(
                    {
                        warning
                        for warning in (
                            ["priced_from_stale_close"]
                            if str(signal.get("reference_price_source") or "") == PRICE_SOURCE_DAILY_CLOSE
                            else []
                        )
                    }
                ),
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

    holdings = _pp._lineage_live_holdings(
        trading_collections, finance_collections, data_collections, plan, user_id
    )
    current_by_key: Dict[str, Dict[str, Any]] = {}
    for row in holdings:
        current_by_key[_symbol_key(row.get("symbol"))] = row
    securities_account_id = str(account.get("_id") or request.securities_account_id)
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
        if delta < 0 and account_positions_synced:
            if account_current_shares <= 0 or account_available_shares <= 0:
                # The broker account has synced and says this symbol is not
                # sellable. The lineage ledger may be stale after external
                # clean-up, so never generate a sell for shares the account
                # does not currently hold.
                continue
            sell_qty = abs(delta)
            capped_sell_qty = min(sell_qty, account_available_shares)
            if capped_sell_qty <= 0:
                continue
            if capped_sell_qty < sell_qty:
                delta = -capped_sell_qty
                target_shares = current_shares + delta
                blockers.append("account_position_capped")
        if delta == 0:
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
            }
        )

    if not items:
        raise HTTPException(status_code=400, detail="targets produced no executable changes after lot-size normalization")

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
        "status": "approved",
        "approved_by": user_id,
        "approved_at": now,
        "anchor_plan_id": plan_id,
        "summary": {
            "buy_count": sum(1 for item in items if item["action"] == "buy"),
            "sell_count": sum(1 for item in items if item["action"] == "sell"),
            "hold_count": 0,
            "skip_count": 0,
            "estimated_buy_amount": estimated_buy,
            "estimated_sell_amount": estimated_sell,
            "manual_action": manual_action,
        },
        "created_at": now,
        "updated_at": now,
    }

    signals: List[Dict[str, Any]] = []
    skipped_items: List[Dict[str, Any]] = []
    for item in items:
        try:
            signal = build_portfolio_plan_live_signal(plan=plan_doc, item=item, account=account, now=now)
        except Exception as exc:  # noqa: BLE001
            skipped_items.append({"symbol": item.get("symbol"), "reason": str(exc)})
            continue
        if signal is not None:
            signals.append(signal.to_mongo())

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
        status="success",
        message="manual live rebalance signals published",
        payload={
            "anchor_plan_id": plan_id,
            "manual_action": manual_action,
            "signal_count": len(signals),
            "inserted_count": inserted_count,
            "existing_count": len(existing_signals),
            "publish_duration_ms": publish_duration_ms,
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
