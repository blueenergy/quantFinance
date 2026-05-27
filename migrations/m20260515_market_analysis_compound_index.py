"""
Migration: market_analysis_cache compound (analysis_date, time_slot) unique index.

Run once before/after deploying the multi-slot market analysis feature.

Steps (idempotent):
  1. Backfill `time_slot = "legacy"` on all docs missing the field
  2. Drop the old single-field unique index `analysis_date_1` (if present)
  3. Create the new compound unique index (analysis_date, time_slot)

Usage:
    cd quantFinance && python -m migrations.m20260515_market_analysis_compound_index
"""
from __future__ import annotations

import os
import sys

from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import OperationFailure


def main() -> int:
    load_dotenv()

    mongo_url = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    analyzer_db_name = os.getenv("ANALYZER_DB_NAME", "quant_analyzer")

    client = MongoClient(mongo_url, serverSelectionTimeoutMS=5000)
    coll = client[analyzer_db_name]["market_analysis_cache"]

    # 1) Backfill time_slot=legacy for documents missing the field
    result = coll.update_many(
        {"time_slot": {"$exists": False}},
        {"$set": {"time_slot": "legacy"}},
    )
    print(f"[Migration] Backfilled time_slot=legacy on {result.modified_count} documents")

    # 2) Drop old unique index if present (name = analysis_date_1)
    existing = coll.index_information()
    legacy_index_name = "analysis_date_1"
    if legacy_index_name in existing:
        try:
            coll.drop_index(legacy_index_name)
            print(f"[Migration] Dropped legacy index '{legacy_index_name}'")
        except OperationFailure as exc:
            print(f"[Migration] Failed to drop legacy index: {exc}")
    else:
        print(f"[Migration] Legacy index '{legacy_index_name}' not present — skipping drop")

    # 3) Create new compound unique index
    new_index_name = "analysis_date_time_slot_unique"
    if new_index_name in coll.index_information():
        print(f"[Migration] Compound index '{new_index_name}' already exists")
    else:
        try:
            coll.create_index(
                [("analysis_date", 1), ("time_slot", 1)],
                unique=True,
                name=new_index_name,
            )
            print(f"[Migration] Created compound unique index '{new_index_name}'")
        except OperationFailure as exc:
            print(f"[Migration] Failed to create compound index: {exc}")
            return 1

    print("[Migration] Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
