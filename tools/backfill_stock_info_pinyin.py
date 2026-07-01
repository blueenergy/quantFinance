#!/usr/bin/env python3
"""Backfill pinyin / pinyin_full into quant_data.stock_info for fast abbreviation search.

Usage:
  python tools/backfill_stock_info_pinyin.py --dry-run --limit 20
  python tools/backfill_stock_info_pinyin.py --only-missing
  python tools/backfill_stock_info_pinyin.py --all
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from typing import Any, Dict, Iterable, List

from dotenv import load_dotenv
from pymongo import MongoClient, UpdateOne

load_dotenv()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from utils.pinyin_utils import stock_name_pinyin_fields

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DATA_DB_NAME = os.getenv("DATA_DB_NAME", "quant_data")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill stock_info pinyin search fields")
    parser.add_argument("--dry-run", action="store_true", help="Print summary without writing")
    parser.add_argument("--limit", type=int, default=0, help="Limit processed rows for smoke tests")
    parser.add_argument(
        "--only-missing",
        dest="only_missing",
        action="store_true",
        help="Only update docs missing pinyin fields (default)",
    )
    parser.add_argument(
        "--all",
        dest="only_missing",
        action="store_false",
        help="Recompute pinyin for every stock with a name",
    )
    parser.set_defaults(only_missing=True)
    return parser.parse_args()


def _iter_stock_docs(stock_col: Any, *, only_missing: bool) -> Iterable[Dict[str, Any]]:
    query: Dict[str, Any] = {"name": {"$exists": True, "$ne": ""}}
    if only_missing:
        query["$or"] = [
            {"pinyin": {"$exists": False}},
            {"pinyin": ""},
            {"pinyin_full": {"$exists": False}},
            {"pinyin_full": ""},
        ]
    projection = {"_id": 1, "symbol": 1, "name": 1, "stock_name": 1, "pinyin": 1, "pinyin_full": 1}
    return stock_col.find(query, projection)


def build_stock_info_pinyin_ops(
    docs: Iterable[Dict[str, Any]], *, now: datetime, limit: int = 0
) -> List[UpdateOne]:
    ops: List[UpdateOne] = []
    for doc in docs:
        name = str(doc.get("name") or doc.get("stock_name") or "").strip()
        if not name:
            continue
        fields = stock_name_pinyin_fields(name)
        if not fields.get("pinyin"):
            continue
        payload = {
            **fields,
            "pinyin_updated_at": now,
        }
        ops.append(UpdateOne({"_id": doc["_id"]}, {"$set": payload}))
        if limit and len(ops) >= limit:
            break
    return ops


def main() -> int:
    args = parse_args()
    now = datetime.now()

    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    try:
        client.admin.command("ping")
    except Exception as exc:
        print(f"MongoDB connection failed: {exc}", file=sys.stderr)
        return 1

    stock_col = client[DATA_DB_NAME]["stock_info"]
    stock_col.create_index("pinyin", background=True, sparse=True)
    stock_col.create_index("pinyin_full", background=True, sparse=True)
    stock_col.create_index("name", background=True, sparse=True)

    docs = _iter_stock_docs(stock_col, only_missing=args.only_missing)
    ops = build_stock_info_pinyin_ops(docs, now=now, limit=args.limit)

    mode = "only-missing" if args.only_missing else "all"
    print(f"stock_info pinyin backfill ({mode})")
    print(f"  database: {DATA_DB_NAME}")
    print(f"  candidate updates: {len(ops)}")
    if args.dry_run:
        for op in ops[:5]:
            symbol = op._filter.get("_id")
            print(f"  sample _id={symbol}")
        print("  dry-run: no writes")
        return 0
    if not ops:
        print("  nothing to update")
        return 0

    result = stock_col.bulk_write(ops, ordered=False)
    changed = int(result.modified_count or 0)
    print("  done")
    print(f"  matched: {result.matched_count}")
    print(f"  modified: {result.modified_count}")
    print(f"  changed total: {changed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
