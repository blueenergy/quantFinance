#!/usr/bin/env python3
"""Deduplicate repeated trade execution rows.

Default mode is read-only. Use ``--apply`` to delete duplicates.

The duplicate key matches the idempotent writer used by quantTrader:

    user_id + order_id + status + (broker_order_id or qmt_order_id) + filled_size

For each duplicate group the script keeps the newest document by
``timestamp``/``_id`` and deletes the rest.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from bson import ObjectId
from dotenv import load_dotenv
from pymongo import MongoClient


def _load_env() -> None:
    for env_path in (
        Path("/app/.env"),
        Path(__file__).resolve().parents[1] / ".env",
        Path.cwd() / ".env",
    ):
        if env_path.exists():
            load_dotenv(env_path, override=False)


def _mongo_client() -> MongoClient:
    mongo_uri = os.getenv("MONGO_URI")
    if not mongo_uri:
        raise SystemExit("MONGO_URI is not configured. Check /app/.env or local .env.")
    return MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)


def _match_query(args: argparse.Namespace) -> Dict[str, Any]:
    query: Dict[str, Any] = {
        "order_id": {"$exists": True, "$nin": [None, ""]},
    }
    if args.user_id:
        query["user_id"] = args.user_id
    if args.securities_account_id:
        query["securities_account_id"] = args.securities_account_id
    if args.mode:
        query["mode"] = args.mode
    if args.since_ts is not None or args.until_ts is not None:
        ts_query: Dict[str, Any] = {}
        if args.since_ts is not None:
            ts_query["$gte"] = args.since_ts
        if args.until_ts is not None:
            ts_query["$lte"] = args.until_ts
        query["timestamp"] = ts_query
    return query


def _duplicate_pipeline(match_query: Dict[str, Any], limit_groups: Optional[int]) -> List[Dict[str, Any]]:
    pipeline: List[Dict[str, Any]] = [
        {"$match": match_query},
        {"$sort": {"timestamp": -1, "_id": -1}},
        {
            "$group": {
                "_id": {
                    "user_id": "$user_id",
                    "order_id": "$order_id",
                    "status": {"$ifNull": ["$status", "filled"]},
                    "broker_order_id": {"$ifNull": ["$broker_order_id", "$qmt_order_id"]},
                    "filled_size": "$filled_size",
                },
                "keep_id": {"$first": "$_id"},
                "ids": {"$push": "$_id"},
                "count": {"$sum": 1},
                "latest_timestamp": {"$first": "$timestamp"},
            }
        },
        {"$match": {"count": {"$gt": 1}}},
        {"$sort": {"count": -1}},
    ]
    if limit_groups:
        pipeline.append({"$limit": limit_groups})
    return pipeline


def _batched(values: List[ObjectId], batch_size: int) -> Iterable[List[ObjectId]]:
    for index in range(0, len(values), batch_size):
        yield values[index : index + batch_size]


def _group_duplicate_ids(group: Dict[str, Any]) -> List[ObjectId]:
    keep_id = group["keep_id"]
    return [doc_id for doc_id in group["ids"] if doc_id != keep_id]


def main() -> int:
    parser = argparse.ArgumentParser(description="Deduplicate finance.trade_executions rows.")
    parser.add_argument("--apply", action="store_true", help="Delete duplicates. Without this flag the script is read-only.")
    parser.add_argument("--user-id", help="Restrict cleanup to one user_id.")
    parser.add_argument("--securities-account-id", help="Restrict cleanup to one securities_account_id.")
    parser.add_argument("--mode", help="Restrict cleanup to one execution mode, e.g. live.")
    parser.add_argument("--since-ts", type=float, help="Only consider executions with timestamp >= this Unix timestamp.")
    parser.add_argument("--until-ts", type=float, help="Only consider executions with timestamp <= this Unix timestamp.")
    parser.add_argument("--limit-groups", type=int, help="Only process the largest N duplicate groups.")
    parser.add_argument("--batch-size", type=int, default=1000, help="Delete batch size when --apply is set.")
    parser.add_argument("--sample", type=int, default=10, help="Number of duplicate groups to print.")
    args = parser.parse_args()

    if args.batch_size <= 0:
        raise SystemExit("--batch-size must be positive")

    _load_env()
    client = _mongo_client()
    db_name = os.getenv("MONGO_DB", "finance")
    collection = client[db_name]["trade_executions"]

    match_query = _match_query(args)
    total_matching = collection.count_documents(match_query)
    print(f"Database: {db_name}")
    print(f"Collection: trade_executions")
    print(f"Mode: {'APPLY' if args.apply else 'DRY-RUN'}")
    print(f"Matching rows: {total_matching}")
    print(f"Match query: {match_query}")

    groups = list(collection.aggregate(_duplicate_pipeline(match_query, args.limit_groups), allowDiskUse=True))
    duplicate_rows = sum(group["count"] - 1 for group in groups)
    print(f"Duplicate groups: {len(groups)}")
    print(f"Rows to delete: {duplicate_rows}")

    for index, group in enumerate(groups[: max(args.sample, 0)], start=1):
        print(
            f"Sample #{index}: key={group['_id']} count={group['count']} "
            f"keep_id={group['keep_id']} latest_timestamp={group.get('latest_timestamp')}"
        )

    if not args.apply:
        print("Dry-run only. Re-run with --apply to delete duplicates.")
        return 0

    deleted = 0
    for group in groups:
        duplicate_ids = _group_duplicate_ids(group)
        for batch in _batched(duplicate_ids, args.batch_size):
            result = collection.delete_many({"_id": {"$in": batch}})
            deleted += result.deleted_count

    print(f"Deleted rows: {deleted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
