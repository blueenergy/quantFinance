"""Ledger / reconcile domain endpoints for portfolio plans.

Mechanical extraction only, no behavior change. The sub-router is mounted by
``portfolio_plans.__init__`` onto the existing ``/portfolio-plans`` router.

The reconcile service diffs the account-level system ledger against the broker's
synced positions. Helpers that still live in the package module (account-id
resolvers, market-price loaders, snapshot overlays, shared holdings builders) are
resolved through ``_pp`` at call time so existing tests can keep monkeypatching
the package namespace.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends

from auth import get_current_user
from services.portfolio_live_snapshot import overlay_account_positions

import routers.portfolio_plans as _pp
from . import _offload
from ._shared import _int_value, _jsonable, _symbol_key


router = APIRouter()


# Live signal statuses that mean an order is still working at the broker and has
# not finished settling. A divergence on such a symbol is expected (in-flight),
# not a manual broker-side drift, so it must not raise the reconcile banner.
_INFLIGHT_SIGNAL_STATUSES = frozenset(
    {"pending", "retry_pending", "submitted", "partial_filled", "cancel_requested"}
)


def _account_level_live_holdings(
    finance_collections: Dict[str, Any],
    data_collections: Dict[str, Any],
    *,
    user_id: str,
    securities_account_id: str,
) -> List[Dict[str, Any]]:
    """Net live positions for the whole securities account (all live plans)."""
    execution_projection = {
        "_id": 0,
        "order_id": 1,
        "user_id": 1,
        "securities_account_id": 1,
        "symbol": 1,
        "name": 1,
        "stock_name": 1,
        "side": 1,
        "action": 1,
        "status": 1,
        "execution_status": 1,
        "filled_size": 1,
        "filled_qty": 1,
        "quantity": 1,
        "size": 1,
        "filled_price": 1,
        "traded_price": 1,
        "avg_price": 1,
        "price": 1,
        "target_price": 1,
        "total_fee": 1,
        "commission": 1,
        "fee": 1,
        "timestamp": 1,
        "created_at": 1,
    }
    executions = list(
        finance_collections["trade_executions"]
        .find(
            {"user_id": user_id, "securities_account_id": securities_account_id},
            execution_projection,
        )
        .sort("timestamp", 1)
    )
    if executions:
        return _pp._holdings_from_executions(executions, data_collections)

    # Legacy execution rows may lack securities_account_id. Fall back to the
    # signal join only when the direct account-scoped execution query is empty.
    signals = list(
        finance_collections["trade_signals"].find(
            {
                "user_id": user_id,
                "mode": "live",
                "securities_account_id": securities_account_id,
            },
            {"_id": 0, "order_id": 1},
        )
    )
    order_ids = [row.get("order_id") for row in signals if row.get("order_id")]
    if not order_ids:
        return []
    executions = list(
        finance_collections["trade_executions"]
        .find({"user_id": user_id, "order_id": {"$in": order_ids}}, execution_projection)
        .sort("timestamp", 1)
    )
    return _pp._holdings_from_executions(executions, data_collections)


def _settling_symbol_keys(
    finance_collections: Dict[str, Any],
    *,
    user_id: str,
    securities_account_id: str,
    account_synced_at: Optional[float],
) -> set:
    """Symbol keys whose ledger/broker gap is explained by in-flight settlement.

    A divergence is "settling" (not a real manual drift) when either:
      * a live signal for the symbol is still working at the broker, or
      * the symbol has a fill newer than the broker's last position sync, so the
        broker snapshot is simply stale and will catch up on the next sync.

    These should be hidden from the reconcile banner to avoid confusing users
    right after they trigger an in-app add/reduce.
    """
    signals_col = finance_collections.get("trade_signals")
    if signals_col is None:
        return set()

    signals = list(
        signals_col.find(
            {
                "user_id": user_id,
                "mode": "live",
                "securities_account_id": securities_account_id,
            },
            {"_id": 0, "order_id": 1, "symbol": 1, "status": 1},
        )
    )
    if not signals:
        return set()

    settling: set = set()
    order_ids: List[Any] = []
    for sig in signals:
        oid = sig.get("order_id")
        if oid:
            order_ids.append(oid)
        status = str(sig.get("status") or "").strip().lower()
        if status in _INFLIGHT_SIGNAL_STATUSES:
            key = _symbol_key(sig.get("symbol"))
            if key:
                settling.add(key)

    executions_col = finance_collections.get("trade_executions")
    if executions_col is not None and isinstance(account_synced_at, (int, float)):
        direct_query = {
            "user_id": user_id,
            "securities_account_id": securities_account_id,
            "timestamp": {"$gt": account_synced_at},
        }
        direct_rows = list(executions_col.find(direct_query, {"_id": 0, "symbol": 1, "timestamp": 1}))
        fallback_rows = []
        if not direct_rows and order_ids:
            fallback_rows = list(
                executions_col.find(
                    {"user_id": user_id, "order_id": {"$in": order_ids}},
                    {"_id": 0, "symbol": 1, "timestamp": 1},
                )
            )
        for ex in [*direct_rows, *fallback_rows]:
            ts = ex.get("timestamp")
            if isinstance(ts, (int, float)) and ts > account_synced_at:
                key = _symbol_key(ex.get("symbol"))
                if key:
                    settling.add(key)

    return settling


def _reconcile_live_holdings(
    trading_collections: Dict[str, Any],
    finance_collections: Dict[str, Any],
    data_collections: Dict[str, Any],
    plan: Dict[str, Any],
    user_id: str,
    *,
    snapshot: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Diff the account-level system ledger against broker-synced positions.

    The ledger aggregates every live fill under the plan's ``securities_account_id``
    (not just this plan's lineage) so shared accounts with multiple strategies do
    not produce false mismatches. Divergence usually means a manual broker-side
    trade was not recorded, so we surface it for immediate re-sync.

    Symbols whose gap is still settling (working orders or fills newer than the
    broker's last position sync) are reported separately and excluded from the
    ``in_sync`` verdict, so an in-app add/reduce does not flash a false mismatch
    during the fill -> position-sync window.
    """
    lineage_control = _pp._get_lineage_control_doc(
        trading_collections,
        str(plan.get("strategy_template_id") or ""),
        str(plan.get("params_hash") or ""),
        user_id,
    )
    account_id = str(lineage_control.get("securities_account_id") or "")
    if not account_id and finance_collections.get("trade_signals") is not None:
        account_id = _pp._lineage_live_account_id_from_signals(
            trading_collections, finance_collections, plan, user_id
        )
    if not account_id:
        account_id = _pp._lineage_live_account_id_from_fills(
            trading_collections, finance_collections, plan, user_id
        )
    if not account_id:
        return {"applicable": False, "in_sync": True, "diffs": [], "securities_account_id": ""}

    # The materialized snapshot's account_positions is a cache that is rebuilt
    # asynchronously after a fill. Trusting it here would flash a false mismatch
    # right after an in-app manual rebalance: the broker sync already reflects the
    # new shares while the stale snapshot ledger does not, and the settling window
    # closes once the broker sync timestamp passes the fill. Only use the snapshot
    # fast path when it is fresh; otherwise rebuild the ledger from the
    # authoritative trade_executions so freshly recorded fills are counted.
    use_snapshot = bool(
        snapshot
        and (snapshot.get("account_positions") or snapshot.get("positions"))
        and not _pp.probe_stale(finance_collections, snapshot)
    )
    if use_snapshot:
        account_symbols = sorted(
            {
                str(row.get("symbol"))
                for row in (snapshot.get("account_positions") or snapshot.get("positions") or [])
                if row.get("symbol")
            }
        )
        prices, realtime_docs = _pp._load_fresh_market_prices(data_collections, account_symbols)
        ledger = overlay_account_positions(snapshot, prices, realtime_docs=realtime_docs)
    else:
        ledger = _pp._account_level_live_holdings(
            finance_collections,
            data_collections,
            user_id=user_id,
            securities_account_id=account_id,
        )

    ledger_by_key: Dict[str, Dict[str, Any]] = {
        _symbol_key(row.get("symbol")): row for row in ledger if row.get("symbol")
    }

    account_rows = list(
        finance_collections["trader_positions"].find(
            {"user_id": user_id, "securities_account_id": account_id},
            {"_id": 0},
        )
    )
    account_by_key: Dict[str, Dict[str, Any]] = {}
    synced_at: Optional[float] = None
    for row in account_rows:
        key = _symbol_key(row.get("symbol"))
        if not key:
            continue
        account_by_key[key] = row
        ts = row.get("synced_at")
        if isinstance(ts, (int, float)):
            synced_at = ts if synced_at is None else max(synced_at, ts)

    settling_keys = _pp._settling_symbol_keys(
        finance_collections,
        user_id=user_id,
        securities_account_id=account_id,
        account_synced_at=synced_at,
    )

    diffs: List[Dict[str, Any]] = []
    settling_diffs: List[Dict[str, Any]] = []
    for key in sorted({*ledger_by_key, *account_by_key}):
        ledger_row = ledger_by_key.get(key) or {}
        account_row = account_by_key.get(key)
        ledger_shares = _int_value(ledger_row.get("shares"))
        # Missing from the broker sync entirely -> treat as unknown, not zero, so
        # we don't raise a false mismatch before the account has ever synced.
        if account_row is None and not account_rows:
            continue
        account_shares = _pp._account_position_qty(account_row) if account_row is not None else 0
        if ledger_shares == account_shares:
            continue
        symbol = ledger_row.get("symbol") or (account_row or {}).get("symbol") or key
        entry = {
            "symbol": symbol,
            "name": ledger_row.get("name") or (account_row or {}).get("name") or "",
            "ledger_shares": ledger_shares,
            "account_shares": account_shares,
            "diff": account_shares - ledger_shares,
        }
        if key in settling_keys:
            entry["settling"] = True
            settling_diffs.append(entry)
        else:
            diffs.append(entry)

    return {
        "applicable": True,
        "in_sync": not diffs,
        "securities_account_id": account_id,
        "account_synced_at": synced_at,
        "diffs": diffs,
        "settling_diffs": settling_diffs,
    }


@router.get("/plans/{plan_id}/holdings-reconcile")
@_offload
def reconcile_portfolio_holdings(
    plan_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Compare the system ledger holdings with the broker's synced positions.

    Live-only: returns per-symbol diffs so the overview can warn the moment the
    book drifts from the real miniQMT account.
    """
    collections = _pp.get_trading_db_collections()
    finance_collections = _pp.get_db_collections()
    data_collections = _pp.get_data_db()
    plan = _pp._require_plan(collections, plan_id, current_user)
    user_id = _pp._current_user_id(current_user)
    try:
        payload = _pp._reconcile_live_holdings(
            collections, finance_collections, data_collections, plan, user_id
        )
    except Exception:
        payload = {"applicable": False, "in_sync": True, "diffs": [], "securities_account_id": ""}
    return {"success": True, "data": _jsonable(payload)}
