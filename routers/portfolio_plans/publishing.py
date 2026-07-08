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

from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter

import routers.portfolio_plans as _pp
from services.portfolio_plan_market import PRICE_SOURCE_DAILY_CLOSE
from ._shared import _numeric, _symbol_key


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
