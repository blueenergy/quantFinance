"""Verify M3.1 rollup fixes against real prod data.

The deployed container still runs the OLD trade_latency_metrics. This script
reuses the container's stable helpers but applies the NEW derivation (fill-gated
executed_at + snapshot trigger_order_id attribution) so we can confirm the fix
on real docs before deploying.
"""
import time
from datetime import datetime, timezone, timedelta

from db import get_db_collections, get_trading_db_collections
from services.trade_latency_metrics import (
    _to_epoch, _duration_ms, _signal_query, _latest_execution_by_order,
    _snapshot_key, _latest_snapshot_by_lineage, STAGE_DEFINITIONS,
)

CN = timezone(timedelta(hours=8))
FILL_STATUSES = {"filled", "partially_filled", "partial", "partial_filled"}


def _is_fill(ex):
    if str(ex.get("status") or "").lower() in FILL_STATUSES:
        return True
    try:
        return int(ex.get("filled_size") or 0) > 0
    except (TypeError, ValueError):
        return False


finance = get_db_collections()
trading = get_trading_db_collections()
since = time.time() - 5 * 86400.0
uid = finance["trade_signals"].distinct("user_id", {"mode": "live", "timestamp": {"$gte": since}})[0]

SIG_PROJ = {"_id": 0, "order_id": 1, "plan_id": 1, "strategy_template_id": 1, "params_hash": 1,
            "symbol": 1, "action": 1, "status": 1, "timestamp": 1, "created_at": 1,
            "first_seen_at": 1, "submitted_at": 1, "executed_at": 1}
signals = list(finance["trade_signals"].find(_signal_query(user_id=uid, since=since), SIG_PROJ)
               .sort("timestamp", -1).limit(2000))
oids = [s["order_id"] for s in signals if s.get("order_id")]
execs = list(finance["trade_executions"].find(
    {"user_id": uid, "order_id": {"$in": oids}},
    {"_id": 0, "order_id": 1, "status": 1, "timestamp": 1, "ingested_at": 1, "filled_size": 1}))
ex_by_order = _latest_execution_by_order(execs)
snaps = list(trading["portfolio_live_snapshots"].find(
    {"user_id": uid},
    {"_id": 0, "strategy_template_id": 1, "params_hash": 1, "updated_at": 1,
     "last_rebuild_reason": 1, "last_rebuild_trigger_order_id": 1}))
snap_by = _latest_snapshot_by_lineage(snaps)


def fmt(ep):
    return datetime.fromtimestamp(ep, CN).strftime("%m-%d %H:%M:%S") if ep else "-"


stage_vals = {n: [] for n, _s, _e in STAGE_DEFINITIONS}
attributed = 0
print(f"user={uid} orders={len(signals)}")
print("publish_at | sym | act | status | seen sub exec ingest snap | snap?")
for s in signals:
    oid = s["order_id"]
    ex = ex_by_order.get(oid) or {}
    snap = snap_by.get(_snapshot_key(s)) or {}
    snap_attr = bool(snap and str(snap.get("last_rebuild_trigger_order_id") or "") == oid)
    if snap_attr:
        attributed += 1
    ex_filled = _is_fill(ex)
    executed_at = _to_epoch(s.get("executed_at"))
    if executed_at is None and ex_filled:
        executed_at = _to_epoch(ex.get("timestamp"))
    tsm = {
        "publish_at": _to_epoch(s.get("created_at")) or _to_epoch(s.get("timestamp")),
        "first_seen_at": _to_epoch(s.get("first_seen_at")),
        "submitted_at": _to_epoch(s.get("submitted_at")),
        "executed_at": executed_at,
        "ingested_at": _to_epoch(ex.get("ingested_at")) if ex_filled else None,
        "snapshot_updated_at": _to_epoch(snap.get("updated_at")) if snap_attr else None,
    }
    d = {}
    for n, sk, ek in STAGE_DEFINITIONS:
        v = _duration_ms(tsm.get(sk), tsm.get(ek))
        d[n] = v
        if v is not None:
            stage_vals[n].append(v)

    def g(k):
        return "-" if d.get(k) is None else int(d[k])
    print(f"{fmt(tsm['publish_at'])} | {s.get('symbol'):<9} | {s.get('action'):<4} | {s.get('status'):<16} | "
          f"{g('publish_to_first_seen_ms'):>5} {g('first_seen_to_submitted_ms'):>5} "
          f"{g('submitted_to_executed_ms'):>6} {g('executed_to_ingested_ms'):>6} {g('ingested_to_snapshot_ms'):>8} | "
          f"{'Y' if snap_attr else '-'}")

print(f"\nsnapshot-attributed orders: {attributed}")
print("stage counts (non-null samples):")
for n, _s, _e in STAGE_DEFINITIONS:
    vals = sorted(stage_vals[n])
    p50 = vals[len(vals) // 2] if vals else None
    print(f"  {n:<28} n={len(vals):>2}  p50={p50}  max={vals[-1] if vals else None}")
