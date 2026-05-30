#!/usr/bin/env python3
"""Backfill portfolio plan context onto historical live trade executions.

The script is intentionally dry-run by default. It only copies metadata from
matching trade_signals, using (user_id, order_id) as the join key.
"""

from __future__ import annotations

import argparse
import os
from typing import Any, Dict, Optional

from dotenv import load_dotenv
from pymongo import MongoClient


CONTEXT_FIELDS = ("plan_id", "strategy_id", "source", "securities_account_id", "account_id", "broker")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill plan context onto trade_executions.")
    parser.add_argument("--user-id", help="Only process one user_id")
    parser.add_argument("--plan-id", help="Only process executions for one portfolio plan")
    parser.add_argument("--limit", type=int, default=0, help="Maximum execution rows to scan; 0 means no limit")
    parser.add_argument("--apply", action="store_true", help="Write updates. Defaults to dry-run.")
    return parser.parse_args()


def _db():
    load_dotenv()
    client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
    return client[os.getenv("MONGO_DB", "finance")]


def _execution_query(user_id: Optional[str]) -> Dict[str, Any]:
    query: Dict[str, Any] = {"order_id": {"$exists": True, "$ne": None}}
    if user_id:
        query["user_id"] = user_id
    return query


def main() -> int:
    args = _parse_args()
    db = _db()
    signals = db["trade_signals"]
    executions = db["trade_executions"]

    cursor = executions.find(_execution_query(args.user_id)).sort("timestamp", -1)
    if args.limit > 0:
        cursor = cursor.limit(args.limit)

    scanned = matched = updated = already_tagged = unmatched = skipped_plan = 0
    for execution in cursor:
        scanned += 1
        order_id = execution.get("order_id")
        user_id = execution.get("user_id")
        signal_query = {"order_id": order_id, "user_id": user_id, "mode": "live"}
        if args.plan_id:
            signal_query["plan_id"] = args.plan_id
        signal = signals.find_one(signal_query)
        if not signal:
            unmatched += 1
            continue

        if args.plan_id and signal.get("plan_id") != args.plan_id:
            skipped_plan += 1
            continue

        matched += 1
        update = {
            field: signal.get(field)
            for field in CONTEXT_FIELDS
            if signal.get(field) is not None and execution.get(field) != signal.get(field)
        }
        if not update:
            already_tagged += 1
            continue

        print(f"{'UPDATE' if args.apply else 'DRY-RUN'} execution={execution.get('_id')} order_id={order_id} set={update}")
        if args.apply:
            result = executions.update_one({"_id": execution["_id"]}, {"$set": update})
            updated += result.modified_count

    print("")
    print("Summary")
    print(f"  scanned: {scanned}")
    print(f"  matched_signals: {matched}")
    print(f"  already_tagged: {already_tagged}")
    print(f"  unmatched_signals: {unmatched}")
    print(f"  skipped_plan: {skipped_plan}")
    print(f"  updated: {updated}")
    print(f"  mode: {'apply' if args.apply else 'dry-run'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
