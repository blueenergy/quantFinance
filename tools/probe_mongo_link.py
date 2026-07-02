"""Diagnose the quant-api <-> MongoDB link.

Run inside the quant-api container:

    python tools/probe_mongo_link.py

It reports, from the app's own MongoClient:
  1. Which mongo node(s) the client is talking to.
  2. Raw round-trip latency (admin "ping") -- pure network RTT, no payload.
  3. Server-side load (serverStatus: connections, mem, global lock, network).
  4. Result-transfer cost vs server execution time, scaled by row count,
     so you can separate "server is slow" from "shipping rows is slow".

Interpretation:
  - If ping RTT is high (say > 5-10ms) the client is not on the same LAN as
    mongo; every query pays that per round-trip.
  - If server executionTimeMillis is small but list(cursor) wall time is large
    and grows with row count, the bottleneck is transfer/latency, not the query.
  - Compare scalar vs wide projection to see per-field payload cost.
"""

from __future__ import annotations

import statistics
import time
from typing import Any, Callable, List

from db import db as finance_db  # finance database handle


def _client():
    return finance_db.client


def _time_ms(fn: Callable[[], Any]) -> float:
    start = time.perf_counter()
    fn()
    return (time.perf_counter() - start) * 1000.0


def _stats(samples: List[float]) -> str:
    samples = sorted(samples)
    p95 = samples[min(len(samples) - 1, int(len(samples) * 0.95))]
    return (
        f"min={samples[0]:.1f} median={statistics.median(samples):.1f} "
        f"p95={p95:.1f} max={samples[-1]:.1f} ms  (n={len(samples)})"
    )


def probe_topology() -> None:
    client = _client()
    print("== topology ==")
    try:
        print("address:", client.address)
    except Exception as exc:
        print("address: <unknown>", exc)
    try:
        print("nodes:", client.nodes)
    except Exception:
        pass
    try:
        hello = client.admin.command("hello")
        print(
            "hello: setName=%s primary=%s me=%s maxWireVersion=%s"
            % (hello.get("setName"), hello.get("primary"), hello.get("me"), hello.get("maxWireVersion"))
        )
    except Exception as exc:
        print("hello failed:", exc)


def probe_ping(rounds: int = 30) -> None:
    client = _client()
    print("\n== raw round-trip (admin ping) ==")
    # warm one round-trip (open socket / auth), then measure
    client.admin.command("ping")
    samples = [_time_ms(lambda: client.admin.command("ping")) for _ in range(rounds)]
    print("ping RTT:", _stats(samples))


def probe_server_status() -> None:
    client = _client()
    print("\n== server status ==")
    try:
        ss = client.admin.command("serverStatus")
    except Exception as exc:
        print("serverStatus failed:", exc)
        return
    conns = ss.get("connections", {})
    print("connections: current=%s available=%s active=%s" % (
        conns.get("current"), conns.get("available"), conns.get("active")))
    mem = ss.get("mem", {})
    print("mem(MB): resident=%s virtual=%s" % (mem.get("resident"), mem.get("virtual")))
    gl = ss.get("globalLock", {}).get("currentQueue", {})
    print("globalLock.currentQueue:", gl)
    net = ss.get("network", {})
    print("network: bytesIn=%s bytesOut=%s numRequests=%s" % (
        net.get("bytesIn"), net.get("bytesOut"), net.get("numRequests")))
    print("uptime(s):", ss.get("uptime"))


def _find_score_date() -> str:
    row = finance_db["stock_scores"].find_one({}, sort=[("score_date", -1)], projection={"_id": 0, "score_date": 1})
    return str(row["score_date"]) if row and row.get("score_date") else ""


def probe_transfer() -> None:
    ss = finance_db["stock_scores"]
    score_date = _find_score_date()
    print("\n== transfer cost (stock_scores, score_date=%s) ==" % score_date)
    if not score_date:
        print("no stock_scores rows; skipping")
        return
    base = {"score_date": score_date}
    scalar_proj = {"_id": 0, "symbol": 1}
    wide_proj = {"_id": 0, "symbol": 1, "name": 1, "industry": 1, "composite_score": 1}

    total = ss.count_documents(base)
    print("docs for score_date:", total)

    # server-side execution time via explain (executes the query, reports ms)
    try:
        ex = finance_db.command({
            "explain": {"find": "stock_scores", "filter": base, "projection": scalar_proj},
            "verbosity": "executionStats",
        })
        es = ex.get("executionStats", {})
        print("server executionStats: nReturned=%s docsExamined=%s ms=%s" % (
            es.get("nReturned"), es.get("totalDocsExamined"), es.get("executionTimeMillis")))
    except Exception as exc:
        print("explain failed:", exc)

    for limit in (1, 50, 200, 1000):
        eff = min(limit, total)
        scalar = [_time_ms(lambda: len(list(ss.find(base, scalar_proj).limit(limit)))) for _ in range(3)]
        wide = [_time_ms(lambda: len(list(ss.find(base, wide_proj).limit(limit)))) for _ in range(3)]
        print(
            f"rows={eff:<5d} scalar[{min(scalar):.0f}/{statistics.median(scalar):.0f}/{max(scalar):.0f}] "
            f"wide[{min(wide):.0f}/{statistics.median(wide):.0f}/{max(wide):.0f}]  (min/med/max ms)"
        )


def main() -> None:
    probe_topology()
    probe_ping()
    probe_server_status()
    probe_transfer()
    print("\nDone. Share this whole output.")


if __name__ == "__main__":
    main()
