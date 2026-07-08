"""Inspect cancel_requested docs + their executions for M3.1."""
import time
from datetime import datetime

from db import get_db_collections

finance = get_db_collections()
since = time.time() - 5 * 86400.0
sigs = list(finance["trade_signals"].find(
    {"mode": "live", "timestamp": {"$gte": since},
     "status": {"$in": ["cancel_requested", "cancelled", "canceled"]}}
).sort("timestamp", -1).limit(6))

print(f"cancel-ish signals: {len(sigs)}")
for s in sigs:
    oid = s.get("order_id")
    print(f"\n=== {oid} {s.get('symbol')} {s.get('action')} status={s.get('status')}")
    for k in ["timestamp", "submitted_at", "executed_at", "updated_at", "filled_size", "filled_qty"]:
        print(f"   signal.{k:<14} = {s.get(k)!r}")
    ex = list(finance["trade_executions"].find({"order_id": oid}))
    print(f"   executions: {len(ex)}")
    for e in ex:
        print(f"     ex status={e.get('status')} ts={e.get('timestamp')!r} "
              f"ingested_at={e.get('ingested_at')!r} filled_size={e.get('filled_size')!r} "
              f"src={e.get('ingest_source')!r}")
