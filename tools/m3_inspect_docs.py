"""Inspect raw prod docs for M3.1 metric-quality design."""
import json
import time
from datetime import datetime, timezone, timedelta

from db import get_db_collections, get_trading_db_collections

CN = timezone(timedelta(hours=8))
finance = get_db_collections()
trading = get_trading_db_collections()

since = time.time() - 5 * 86400.0
q = {"mode": "live", "timestamp": {"$gte": since}}
sigs = list(finance["trade_signals"].find(q).sort("timestamp", -1).limit(6))


def show(label, d, keys):
    print(f"-- {label}")
    for k in keys:
        v = d.get(k)
        if isinstance(v, datetime):
            v = f"{v.isoformat()} (dt)"
        print(f"     {k:<26} = {v!r}")


for s in sigs:
    oid = s.get("order_id")
    print(f"\n===== order_id={oid} symbol={s.get('symbol')} action={s.get('action')} status={s.get('status')}")
    show("signal", s, ["timestamp", "created_at", "first_seen_at", "first_seen_by",
                        "submitted_at", "executed_at", "updated_at", "status",
                        "strategy_template_id", "params_hash"])
    ex = list(finance["trade_executions"].find({"order_id": oid}))
    for e in ex:
        show(f"execution(status={e.get('status')})", e,
             ["timestamp", "ingested_at", "ingest_source", "updated_at", "filled_size", "status"])

# snapshot docs for the lineage
keys = {(s.get("strategy_template_id"), s.get("params_hash")) for s in sigs}
print("\n===== snapshots for lineages =====")
for stid, ph in keys:
    if not stid or not ph:
        continue
    snaps = list(trading["portfolio_live_snapshots"].find(
        {"strategy_template_id": stid, "params_hash": ph}))
    print(f"lineage stid={stid} ph={ph}: {len(snaps)} snapshot docs")
    for sn in snaps:
        show("snapshot", sn, ["updated_at", "build_ms", "last_rebuild_reason",
                              "last_rebuild_trigger_order_id", "watermark"])
