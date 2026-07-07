"""Materialized live lineage snapshots (structural holdings/trades + read-time prices)."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from pymongo import ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

from services.portfolio_lineage_live import (
    _numeric,
    _round_or_none,
    _symbol_key,
    build_lineage_live_positions,
    build_lineage_live_trades,
    merge_lineage_live_executions,
)
from services.portfolio_lineage_live_bundle import (
    compute_execution_watermark,
    fetch_account_level_executions,
    fetch_lineage_live_bundle,
)
from services.portfolio_lineage_state import lineage_key
from services.trade_ledger import group_executions_by_order

log = logging.getLogger(__name__)


def ensure_live_snapshot_indexes(
    trading_collections: Dict[str, Any],
    finance_collections: Dict[str, Any],
) -> None:
    snapshots = trading_collections.get("portfolio_live_snapshots")
    if snapshots is not None:
        snapshots.create_index(
            [("user_id", ASCENDING), ("strategy_template_id", ASCENDING), ("params_hash", ASCENDING)],
            unique=True,
            name="portfolio_live_snapshots_lineage_unique",
        )
        snapshots.create_index(
            [("updated_at", DESCENDING)],
            name="live_snapshot_updated_desc",
        )
    executions = finance_collections.get("trade_executions")
    if executions is not None:
        executions.create_index(
            [("user_id", ASCENDING), ("securities_account_id", ASCENDING), ("updated_at", DESCENDING)],
            name="trade_exec_user_account_updated_desc",
        )


def get_live_snapshot(
    trading_collections: Dict[str, Any],
    *,
    user_id: str,
    strategy_template_id: str,
    params_hash: str,
) -> Optional[Dict[str, Any]]:
    coll = trading_collections.get("portfolio_live_snapshots")
    if coll is None:
        return None
    return coll.find_one(
        lineage_key(user_id, strategy_template_id, params_hash),
        {"_id": 0},
    )


def _lineage_initial_capital(plan: Dict[str, Any]) -> float:
    params = plan.get("params_snapshot") or {}
    summary = plan.get("summary") or {}
    return _numeric(
        params.get("initial_capital", plan.get("initial_capital", summary.get("baseline_equity"))),
        1_000_000.0,
    )


def _structural_positions_from_grouped(
    grouped_executions: List[Dict[str, Any]],
    name_map: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    payload = build_lineage_live_positions(grouped_executions, {}, name_map=name_map)
    structural: List[Dict[str, Any]] = []
    for row in payload.get("positions") or []:
        shares = int(row.get("shares") or 0)
        avg_cost = _numeric(row.get("avg_cost"))
        structural.append(
            {
                "symbol": row.get("symbol"),
                "name": row.get("name") or "",
                "shares": shares,
                "avg_cost": round(avg_cost, 4),
                "cost_basis": round(avg_cost * shares, 2) if shares > 0 else 0.0,
                "realized_pnl": round(_numeric(row.get("realized_pnl")), 2),
            }
        )
    return structural


def _structural_account_positions(
    executions: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    grouped = group_executions_by_order(executions)
    return _structural_positions_from_grouped(grouped)


def _build_name_map(signals: List[Dict[str, Any]]) -> Dict[str, str]:
    name_map: Dict[str, str] = {}
    for row in signals:
        symbol = str(row.get("symbol") or "")
        if not symbol:
            continue
        name = str(row.get("name") or row.get("stock_name") or "")
        if name:
            name_map[symbol] = name
            name_map.setdefault(_symbol_key(symbol), name)
    return name_map


def rebuild_live_snapshot(
    trading_collections: Dict[str, Any],
    finance_collections: Dict[str, Any],
    plan: Dict[str, Any],
    user_id: str,
    *,
    anchor_plan_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Replay lineage executions once and upsert the structural snapshot."""

    coll = trading_collections.get("portfolio_live_snapshots")
    if coll is None:
        return None

    started = time.perf_counter()
    strategy_template_id = str(plan.get("strategy_template_id") or "")
    params_hash = str(plan.get("params_hash") or "")
    if not strategy_template_id or not params_hash:
        return None

    bundle = fetch_lineage_live_bundle(trading_collections, finance_collections, plan, user_id)
    lineage_ids = bundle["lineage_ids"]
    signals = bundle["signals"]
    grouped = bundle["grouped_executions"]
    plan_meta = bundle["plan_meta"]
    securities_account_id = bundle["securities_account_id"]
    execution_venue = bundle["execution_venue"]

    name_map = _build_name_map(signals)
    merged = merge_lineage_live_executions(signals, grouped, plan_meta)
    trades = build_lineage_live_trades(merged, {}, name_map=name_map)
    positions = _structural_positions_from_grouped(grouped, name_map=name_map)

    account_executions = fetch_account_level_executions(
        finance_collections,
        user_id=user_id,
        securities_account_id=securities_account_id,
    )
    account_positions = _structural_account_positions(account_executions)

    initial_capital = _lineage_initial_capital(plan)
    total_realized = sum(_numeric(row.get("realized_pnl")) for row in positions)
    total_cost_basis = sum(_numeric(row.get("cost_basis")) for row in positions if int(row.get("shares") or 0) > 0)
    cash = initial_capital + total_realized - total_cost_basis

    now = datetime.utcnow()
    doc = {
        **lineage_key(user_id, strategy_template_id, params_hash),
        "lineage_plan_ids": lineage_ids,
        "anchor_plan_id": anchor_plan_id or str(plan.get("plan_id") or ""),
        "securities_account_id": securities_account_id,
        "execution_venue": execution_venue,
        "positions": positions,
        "account_positions": account_positions,
        "trades": trades,
        "book_equity": {
            "initial_capital": round(initial_capital, 2),
            "cash": round(cash, 2),
            "realized_pnl": round(total_realized, 2),
            "execution_count": len(grouped),
        },
        "watermark": compute_execution_watermark(signals, bundle["executions"]),
        "status": "completed",
        "updated_at": now,
        "build_ms": round((time.perf_counter() - started) * 1000, 1),
    }
    coll.update_one(
        lineage_key(user_id, strategy_template_id, params_hash),
        {"$set": doc},
        upsert=True,
    )
    return doc


def rebuild_live_snapshot_for_plan(
    trading_collections: Dict[str, Any],
    finance_collections: Dict[str, Any],
    plan: Dict[str, Any],
    user_id: str,
) -> Optional[Dict[str, Any]]:
    return rebuild_live_snapshot(
        trading_collections,
        finance_collections,
        plan,
        user_id,
        anchor_plan_id=str(plan.get("plan_id") or ""),
    )


# --- S4-B: single lease-guarded snapshot rebuild path -----------------------
# All ad-hoc `threading.Thread(rebuild_live_snapshot_for_plan)` triggers
# (stale read, publish, post-execution) funnel through the two helpers below so
# concurrent rebuilds of the *same* snapshot (across threads and API workers)
# de-duplicate via a best-effort Mongo lease on the `job_status` collection,
# mirroring `scheduler._acquire_scheduler_job_lock`.

_SNAPSHOT_REBUILD_LEASE_TTL = 120


def _snapshot_rebuild_lock_key(plan: Dict[str, Any], user_id: str) -> str:
    return "%s:%s:%s" % (
        user_id,
        str(plan.get("strategy_template_id") or ""),
        str(plan.get("params_hash") or ""),
    )


def _acquire_snapshot_rebuild_lease(
    finance_collections: Dict[str, Any],
    lock_key: str,
    ttl_seconds: int = _SNAPSHOT_REBUILD_LEASE_TTL,
) -> Optional[str]:
    """Best-effort per-snapshot lease.

    Returns:
        - an owner token (truthy str) when the lease was acquired -> run + release.
        - ``""`` when the lease is currently held elsewhere -> skip the rebuild.
        - ``None`` when no lease backend is available -> run without a lease.
    """
    coll = None
    try:
        coll = finance_collections.get("job_status")  # type: ignore[union-attr]
    except Exception:
        coll = None
    if coll is None:
        return None
    now = time.time()
    owner = f"snapshot_rebuild:{uuid.uuid4().hex}"
    lock_id = f"snapshot_rebuild_lock:{lock_key}"
    try:
        result = coll.update_one(
            {
                "_id": lock_id,
                "$or": [
                    {"locked_until": {"$lte": now}},
                    {"locked_until": {"$exists": False}},
                ],
            },
            {
                "$set": {
                    "job_id": lock_id,
                    "locked_by": owner,
                    "locked_until": now + ttl_seconds,
                    "updated_at": now,
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
    except DuplicateKeyError:
        # Another worker won the first upsert race for this lock.
        return ""
    except Exception:
        # Lease infra hiccup -> don't block the rebuild.
        return None
    if getattr(result, "upserted_id", None) or getattr(result, "modified_count", 0):
        return owner
    return ""


def _release_snapshot_rebuild_lease(
    finance_collections: Dict[str, Any],
    lock_key: str,
    owner: str,
) -> None:
    if not owner:
        return
    try:
        coll = finance_collections.get("job_status")  # type: ignore[union-attr]
    except Exception:
        coll = None
    if coll is None:
        return
    try:
        coll.update_one(
            {"_id": f"snapshot_rebuild_lock:{lock_key}", "locked_by": owner},
            {"$set": {"locked_until": time.time(), "updated_at": time.time()}},
        )
    except Exception:
        pass


def rebuild_live_snapshot_for_plan_guarded(
    trading_collections: Dict[str, Any],
    finance_collections: Dict[str, Any],
    plan: Dict[str, Any],
    user_id: str,
) -> Optional[Dict[str, Any]]:
    """Rebuild guarded by a per-snapshot Mongo lease (synchronous).

    Skips (returns ``None``) when another rebuild for the same snapshot is
    already in flight.
    """
    lock_key = _snapshot_rebuild_lock_key(plan, user_id)
    lease = _acquire_snapshot_rebuild_lease(finance_collections, lock_key)
    if lease == "":
        log.debug("snapshot rebuild already in progress for %s; skipping", lock_key)
        return None
    try:
        return rebuild_live_snapshot_for_plan(
            trading_collections, finance_collections, plan, user_id
        )
    finally:
        if lease:
            _release_snapshot_rebuild_lease(finance_collections, lock_key, lease)


def schedule_snapshot_rebuild(
    trading_collections: Dict[str, Any],
    finance_collections: Dict[str, Any],
    plan: Dict[str, Any],
    user_id: str,
    *,
    reason: str = "",
) -> threading.Thread:
    """Fire-and-forget background rebuild funneled through the guarded path."""
    lock_key = _snapshot_rebuild_lock_key(plan, user_id)

    def _run() -> None:
        try:
            rebuild_live_snapshot_for_plan_guarded(
                trading_collections, finance_collections, plan, user_id
            )
        except Exception:
            log.exception(
                "async snapshot rebuild failed for %s (reason=%s)",
                lock_key,
                reason or "-",
            )

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread


def probe_stale(
    finance_collections: Dict[str, Any],
    snapshot: Dict[str, Any],
) -> bool:
    """Return True when executions changed since the snapshot watermark."""

    if not snapshot:
        return True
    watermark = snapshot.get("watermark") or {}
    account_id = str(snapshot.get("securities_account_id") or "")
    user_id = str(snapshot.get("user_id") or "")
    if not account_id or not user_id:
        return watermark.get("execution_count", 0) == 0 and watermark.get("signal_count", 0) == 0

    executions = finance_collections.get("trade_executions")
    if executions is None:
        return False

    query = {"user_id": user_id, "securities_account_id": account_id}

    # Primary freshness signal: the numeric execution timestamp. The trader
    # upload always writes it, so sorting by it reliably surfaces the newest
    # fill. We must NOT sort by updated_at here: it is optional and historically
    # absent on most executions, so an updated_at sort returns an arbitrary old
    # doc and the snapshot is wrongly judged fresh (holdings never refresh).
    latest_ts = executions.find_one(
        query,
        {"_id": 0, "timestamp": 1},
        sort=[("timestamp", DESCENDING)],
    )
    if latest_ts is None:
        return watermark.get("execution_count", 0) > 0

    ts = latest_ts.get("timestamp")
    max_ts = float(watermark.get("max_execution_ts") or 0)
    if isinstance(ts, (int, float)) and float(ts) > max_ts:
        return True

    # Secondary: an in-place correction (e.g. fee/status fix) may bump
    # updated_at without advancing timestamp. Only weigh docs that carry it.
    latest_updated_doc = executions.find_one(
        {**query, "updated_at": {"$exists": True, "$ne": ""}},
        {"_id": 0, "updated_at": 1},
        sort=[("updated_at", DESCENDING)],
    )
    if latest_updated_doc is not None:
        latest_updated = str(latest_updated_doc.get("updated_at") or "")
        stored_updated = str(watermark.get("last_execution_updated_at") or "")
        if latest_updated and latest_updated > stored_updated:
            return True

    # Both signals compared above. We deliberately skip a full-account
    # count_documents here: it scanned the whole account on every snapshot read
    # and only guarded against deletions, which the EOD rebuild reconciles.
    return False


def overlay_realtime(
    snapshot: Dict[str, Any],
    price_map: Dict[str, float],
    realtime_docs: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Apply market prices to structural positions/trades for API responses."""

    positions: List[Dict[str, Any]] = []
    total_market_value = 0.0
    total_realized_pnl = 0.0
    total_unrealized_pnl = 0.0
    total_cost_basis = 0.0
    latest_update = ""

    for row in snapshot.get("positions") or []:
        symbol = str(row.get("symbol") or "")
        if not symbol:
            continue
        shares = int(row.get("shares") or 0)
        avg_cost = _numeric(row.get("avg_cost"))
        realized = _numeric(row.get("realized_pnl"))
        total_realized_pnl += realized

        price = price_map.get(symbol) or price_map.get(_symbol_key(symbol)) or avg_cost
        if shares > 0 and price <= 0:
            price = avg_cost
        value = shares * price
        unrealized = (price - avg_cost) * shares if shares > 0 else 0.0
        unrealized_pct = price / avg_cost - 1 if shares > 0 and avg_cost > 0 and price > 0 else None
        total_market_value += value
        total_unrealized_pnl += unrealized
        if shares > 0:
            total_cost_basis += avg_cost * shares

        rt_doc = (realtime_docs or {}).get(symbol) or (realtime_docs or {}).get(_symbol_key(symbol)) or {}
        update_time = str(
            rt_doc.get("updated_at")
            or rt_doc.get("realtime_trade_time")
            or rt_doc.get("snapshot_time")
            or ""
        )
        if update_time:
            latest_update = max(latest_update, update_time)

        in_market = symbol in price_map or _symbol_key(symbol) in price_map
        price_source = "market" if in_market and price > 0 else "avg_cost"
        if shares != 0 or realized != 0:
            positions.append(
                {
                    "symbol": symbol,
                    "name": row.get("name") or "",
                    "shares": shares,
                    "avg_cost": round(avg_cost, 4),
                    "last_price": round(price, 4) if price else 0.0,
                    "last_price_source": price_source,
                    "market_value": round(value, 2),
                    "realized_pnl": round(realized, 2),
                    "unrealized_pnl": round(unrealized, 2),
                    "unrealized_pnl_pct": round(unrealized_pct, 6) if unrealized_pct is not None else None,
                    **({"realtime_doc": rt_doc} if rt_doc else {}),
                }
            )

    holding_count = sum(1 for p in positions if (p.get("shares") or 0) > 0)
    summary = {
        "total_market_value": round(total_market_value, 2),
        "total_realized_pnl": round(total_realized_pnl, 2),
        "total_unrealized_pnl": round(total_unrealized_pnl, 2),
        "total_cost_basis": round(total_cost_basis, 2),
        "holding_count": holding_count,
        "position_row_count": len(positions),
    }

    trades_out: List[Dict[str, Any]] = []
    for trade in snapshot.get("trades") or []:
        row = dict(trade)
        if row.get("status") != "open":
            trades_out.append(row)
            continue
        symbol = str(row.get("symbol") or "")
        buy_price = _numeric(row.get("buy_price"))
        quantity = _numeric(row.get("quantity"))
        buy_fee = _numeric(row.get("fee"))
        mark_price = price_map.get(symbol) or price_map.get(_symbol_key(symbol)) or 0.0
        holding_return = mark_price / buy_price - 1 if mark_price > 0 and buy_price > 0 else None
        net_pnl = (mark_price - buy_price) * quantity - buy_fee if mark_price > 0 else None
        row["holding_return"] = _round_or_none(holding_return, 6)
        row["net_pnl"] = _round_or_none(net_pnl, 2)
        trades_out.append(row)

    book = dict(snapshot.get("book_equity") or {})
    market_value = _numeric(summary.get("total_market_value"))
    cash = _numeric(book.get("cash"))
    equity = cash + market_value
    current_book_equity = {
        "equity": round(equity, 2),
        "cash": round(cash, 2),
        "market_value": round(market_value, 2),
        "initial_capital": _numeric(book.get("initial_capital")),
        "lineage_plan_ids": snapshot.get("lineage_plan_ids") or [],
        "execution_count": book.get("execution_count"),
        "latest_price_update": latest_update,
        "source": "portfolio_live_snapshots",
        "realized_pnl": round(_numeric(summary.get("total_realized_pnl")), 2),
        "unrealized_pnl": round(_numeric(summary.get("total_unrealized_pnl")), 2),
    }

    return {
        "positions": positions,
        "summary": summary,
        "latest_price_update": latest_update,
        "trades": trades_out,
        "current_book_equity": current_book_equity,
    }


def overlay_account_positions(
    snapshot: Dict[str, Any],
    price_map: Dict[str, float],
    realtime_docs: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Account-wide holdings with read-time prices (for reconcile ledger side)."""

    structural = snapshot.get("account_positions") or snapshot.get("positions") or []
    grouped_like: List[Dict[str, Any]] = []
    for row in structural:
        symbol = str(row.get("symbol") or "")
        shares = int(row.get("shares") or 0)
        avg_cost = _numeric(row.get("avg_cost"))
        if not symbol or shares <= 0:
            continue
        grouped_like.append(
            {
                "symbol": symbol,
                "name": row.get("name") or "",
                "side": "BUY",
                "filled_size": shares,
                "filled_price": avg_cost,
            }
        )
    payload = build_lineage_live_positions(grouped_like, price_map, realtime_docs=realtime_docs)
    return [row for row in (payload.get("positions") or []) if int(row.get("shares") or 0) > 0]


def list_live_lineage_keys(finance_collections: Dict[str, Any]) -> List[Dict[str, str]]:
    """Distinct lineage keys that have at least one live signal."""

    rows = finance_collections["trade_signals"].aggregate(
        [
            {"$match": {"mode": "live", "plan_id": {"$exists": True, "$ne": None}}},
            {
                "$group": {
                    "_id": {
                        "user_id": "$user_id",
                        "plan_id": "$plan_id",
                    }
                }
            },
        ]
    )
    return [
        {"user_id": str(row["_id"]["user_id"]), "plan_id": str(row["_id"]["plan_id"])}
        for row in rows
        if row.get("_id", {}).get("user_id") and row.get("_id", {}).get("plan_id")
    ]


def _position_qty(row: Dict[str, Any]) -> int:
    for field in ("shares", "qty", "quantity", "volume", "current_volume", "total_volume"):
        if row.get(field) is not None:
            return int(_numeric(row.get(field)))
    return 0


def _positions_by_symbol_key(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for row in rows:
        key = _symbol_key(row.get("symbol"))
        if not key:
            continue
        out[key] = out.get(key, 0) + max(0, _position_qty(row))
    return out


def _diff_position_maps(
    *,
    ledger: Dict[str, int],
    broker: Dict[str, int],
) -> List[Dict[str, Any]]:
    diffs: List[Dict[str, Any]] = []
    for key in sorted({*ledger, *broker}):
        ledger_shares = int(ledger.get(key) or 0)
        broker_shares = int(broker.get(key) or 0)
        if ledger_shares == broker_shares:
            continue
        diffs.append(
            {
                "symbol_key": key,
                "ledger_shares": ledger_shares,
                "broker_shares": broker_shares,
                "diff": broker_shares - ledger_shares,
            }
        )
    return diffs


def reconcile_live_snapshots_with_broker_positions(
    trading_collections: Dict[str, Any],
    finance_collections: Dict[str, Any],
    *,
    user_id: Optional[str] = None,
    rebuild_on_diff: bool = True,
) -> Dict[str, Any]:
    """Compare materialized account positions with broker-synced positions.

    If a snapshot is stale, rebuilding from executions often resolves the drift.
    If a mismatch remains after rebuild, it is likely a real broker-side/manual
    trade that needs operator attention; the job records those residual diffs.
    """

    snapshots = trading_collections.get("portfolio_live_snapshots")
    trader_positions = finance_collections.get("trader_positions")
    if snapshots is None or trader_positions is None:
        return {"checked": 0, "rebuilt": 0, "resolved": 0, "mismatched": 0, "skipped": 0, "diffs": []}

    query: Dict[str, Any] = {}
    if user_id:
        query["user_id"] = user_id

    checked = 0
    rebuilt = 0
    resolved = 0
    mismatched = 0
    skipped = 0
    residual_diffs: List[Dict[str, Any]] = []

    # Materialize before iterating: the loop rebuilds snapshots (an upsert on
    # this same collection), and mutating a live cursor's collection can make
    # pymongo skip or re-yield documents.
    for snapshot in list(snapshots.find(query)):
        uid = str(snapshot.get("user_id") or "")
        account_id = str(snapshot.get("securities_account_id") or "")
        if not uid or not account_id:
            skipped += 1
            continue

        broker_rows = list(
            trader_positions.find(
                {"user_id": uid, "securities_account_id": account_id},
                {
                    "_id": 0,
                    "symbol": 1,
                    "qty": 1,
                    "quantity": 1,
                    "volume": 1,
                    "shares": 1,
                    "current_volume": 1,
                    "total_volume": 1,
                },
            )
        )
        if not broker_rows:
            skipped += 1
            continue

        checked += 1
        ledger = _positions_by_symbol_key(snapshot.get("account_positions") or snapshot.get("positions") or [])
        broker = _positions_by_symbol_key(broker_rows)
        diffs = _diff_position_maps(ledger=ledger, broker=broker)
        if not diffs:
            continue

        final_diffs = diffs
        plan = None
        if rebuild_on_diff:
            plan_id = str(snapshot.get("anchor_plan_id") or "")
            if not plan_id:
                plan_ids = snapshot.get("lineage_plan_ids") or []
                plan_id = str(plan_ids[0]) if plan_ids else ""
            if plan_id:
                plan = trading_collections["portfolio_trade_plans"].find_one({"user_id": uid, "plan_id": plan_id})
            if plan:
                rebuilt_doc = rebuild_live_snapshot_for_plan(trading_collections, finance_collections, plan, uid)
                if rebuilt_doc:
                    rebuilt += 1
                    ledger = _positions_by_symbol_key(
                        rebuilt_doc.get("account_positions") or rebuilt_doc.get("positions") or []
                    )
                    final_diffs = _diff_position_maps(ledger=ledger, broker=broker)
                    if not final_diffs:
                        resolved += 1
                        continue

        mismatched += 1
        residual_diffs.append(
            {
                "user_id": uid,
                "securities_account_id": account_id,
                "anchor_plan_id": snapshot.get("anchor_plan_id"),
                "diffs": final_diffs,
            }
        )

    result = {
        "checked": checked,
        "rebuilt": rebuilt,
        "resolved": resolved,
        "mismatched": mismatched,
        "skipped": skipped,
        "diffs": residual_diffs,
    }
    status_coll = finance_collections.get("job_status")
    if status_coll is not None:
        status_coll.update_one(
            {"_id": "portfolio_live_snapshot_reconcile"},
            {
                "$set": {
                    **result,
                    "status": "warning" if mismatched else "ok",
                    "updated_at": datetime.utcnow(),
                }
            },
            upsert=True,
        )
    if mismatched:
        log.warning("Live snapshot broker reconcile found residual mismatches: %s", residual_diffs)
    return result


def rebuild_all_live_snapshots(
    trading_collections: Dict[str, Any],
    finance_collections: Dict[str, Any],
    *,
    user_id: Optional[str] = None,
) -> Dict[str, int]:
    """Rebuild snapshots for every lineage that has live signals."""

    keys = list_live_lineage_keys(finance_collections)
    seen: set[Tuple[str, str, str]] = set()
    written = 0
    skipped = 0
    for entry in keys:
        uid = entry["user_id"]
        if user_id and uid != user_id:
            continue
        plan = trading_collections["portfolio_trade_plans"].find_one(
            {"user_id": uid, "plan_id": entry["plan_id"]},
            {"_id": 0},
        )
        if not plan:
            skipped += 1
            continue
        stid = str(plan.get("strategy_template_id") or "")
        phash = str(plan.get("params_hash") or "")
        if not stid or not phash:
            skipped += 1
            continue
        dedupe_key = (uid, stid, phash)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        result = rebuild_live_snapshot(trading_collections, finance_collections, plan, uid)
        if result:
            written += 1
        else:
            skipped += 1
    return {"written": written, "skipped": skipped, "lineages": len(seen)}
