"""M3 baseline: run the live latency rollup against prod Mongo (inside quant-api).

Piped into `docker exec -i quant-api python -`. Uses the container's own
db getters + MONGO_URI, so no cross-host Mongo round-trips from outside.
"""

import sys
import time
from datetime import datetime, timezone, timedelta

from db import get_db_collections, get_trading_db_collections
from services.trade_latency_metrics import build_trade_latency_rollup

CN = timezone(timedelta(hours=8))
LOOKBACK_DAYS = float(sys.argv[1]) if len(sys.argv) > 1 else 4.0

now = time.time()
since = now - LOOKBACK_DAYS * 86400.0

finance = get_db_collections()
trading = get_trading_db_collections()

users = finance["trade_signals"].distinct(
    "user_id", {"mode": "live", "timestamp": {"$gte": since}}
)
print(f"== M3 latency baseline ==  lookback={LOOKBACK_DAYS}d  live users={len(users)}")

STAGES = [
    "publish_to_first_seen_ms",
    "first_seen_to_submitted_ms",
    "submitted_to_executed_ms",
    "executed_to_ingested_ms",
    "ingested_to_snapshot_ms",
    "publish_to_ingested_ms",
    "publish_to_snapshot_ms",
]


def fmt_ts(ep):
    if not ep:
        return "-"
    return datetime.fromtimestamp(ep, CN).strftime("%m-%d %H:%M:%S")


for uid in users:
    r = build_trade_latency_rollup(
        finance, trading, user_id=uid, since=since, limit=2000, include_orders=True
    )
    oc = r["order_count"]
    if not oc:
        continue
    print(f"\n### user_id={uid}  orders={oc}  executions={r['execution_count']}  snapshots={r['snapshot_count']}")
    print(f"{'stage':<28} {'n':>4} {'miss':>5} {'p50':>9} {'p95':>9} {'p99':>9} {'max':>9}")
    for st in STAGES:
        s = r["stages"][st]
        print(f"{st:<28} {s['count']:>4} {s['missing_count']:>5} "
              f"{str(s['p50']):>9} {str(s['p95']):>9} {str(s['p99']):>9} {str(s['max']):>9}")

    rows = sorted(r["orders"], key=lambda x: (x["timestamps"].get("publish_at") or 0))
    print("\n  per-order (ms):  publish_at | sym | act | status | seen sub exec ingest snap | e2e_ingest e2e_snap | snap? | src")
    for o in rows:
        d = o["durations_ms"]
        ts = o["timestamps"]
        def g(k):
            v = d.get(k)
            return "-" if v is None else int(v)
        print(f"  {fmt_ts(ts.get('publish_at'))} | {o['symbol']:<9} | {o['action']:<4} | {o['status']:<16} | "
              f"{g('publish_to_first_seen_ms'):>6} {g('first_seen_to_submitted_ms'):>6} "
              f"{g('submitted_to_executed_ms'):>7} {g('executed_to_ingested_ms'):>7} {g('ingested_to_snapshot_ms'):>8} | "
              f"{g('publish_to_ingested_ms'):>7} {g('publish_to_snapshot_ms'):>8} | "
              f"{'Y' if o.get('snapshot_attributed') else '-':>5} | "
              f"{o.get('first_seen_by','')}/{o.get('ingest_source','')}")

print("\n== done ==")
