"""Migration: normalize ``portfolio_trade_plans`` to the canonical format.

One-time normalization so application code can assume a clean shape instead
of growing fallbacks on dirty data. It does three things, all idempotent:

  1. Stamp authoritative ``record_kind`` on every document
     (``trade_plan`` / ``observation`` / ``ledger_event``) via the shared
     ``services.portfolio_record_kind.classify_record``.
  2. Backfill missing ``origin`` -> ``"strategy"`` (all legacy origin-less
     rows are strategy-generated; manual rows always carry origin already).
  3. Fix legacy external-manual ledger rows whose ``plan_type`` is
     ``"rebalance"`` to ``"manual"`` (current code already writes "manual").

Safety:
  - Dry-run by default; prints the full change plan and per-kind counts.
  - ``--apply`` performs the writes, after copying the collection to a
    timestamped backup ``portfolio_trade_plans_backup_record_kind_<stamp>``.
  - Documents that cannot be classified are reported and left untouched;
    the script exits non-zero so they get manual attention.

Usage:
    cd quantFinance && python -m migrations.m20260626_portfolio_record_kind          # dry-run
    cd quantFinance && python -m migrations.m20260626_portfolio_record_kind --apply  # write
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from datetime import datetime

from dotenv import load_dotenv
from pymongo import MongoClient

from services.portfolio_record_kind import (
    LEDGER_MANUAL_ACTIONS,
    VALID_RECORD_KINDS,
    classify_record,
)

COLLECTION = "portfolio_trade_plans"


def _compute_changes(docs):
    """Return (planned, unclassifiable).

    planned: list of (doc, set_fields) for docs needing a write.
    unclassifiable: list of docs that classify_record rejects.
    """
    planned = []
    unclassifiable = []
    for doc in docs:
        try:
            kind = classify_record(doc)
        except ValueError:
            unclassifiable.append(doc)
            continue

        set_fields = {}
        if doc.get("record_kind") != kind:
            set_fields["record_kind"] = kind
        if kind == "ledger_event":
            if doc.get("origin") != "manual":
                set_fields["origin"] = "manual"
            if "_extman_" in str(doc.get("plan_id") or "") and not doc.get("manual_action"):
                set_fields["manual_action"] = "external_manual"
            if doc.get("status") != "filled":
                set_fields["status"] = "filled"
        elif not doc.get("origin"):
            set_fields["origin"] = "strategy"

        # Legacy ledger rows carried plan_type="rebalance"; current code writes
        # "manual". Normalize so the data is internally consistent.
        if kind == "ledger_event" and doc.get("plan_type") == "rebalance":
            set_fields["plan_type"] = "manual"

        if set_fields:
            planned.append((doc, set_fields))
    return planned, unclassifiable


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write changes (default is dry-run).",
    )
    args = parser.parse_args()

    load_dotenv()
    mongo_url = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    trading_db_name = os.getenv("TRADING_DB_NAME", "quant_trading")

    client = MongoClient(mongo_url, serverSelectionTimeoutMS=5000)
    coll = client[trading_db_name][COLLECTION]

    docs = list(coll.find({}))
    total = len(docs)
    print(f"[Migration] {trading_db_name}.{COLLECTION}: {total} documents")

    planned, unclassifiable = _compute_changes(docs)

    # Report target distribution (post-migration record_kind for all docs).
    kind_counter = Counter()
    for doc in docs:
        try:
            kind_counter[classify_record(doc)] += 1
        except ValueError:
            kind_counter["<unclassifiable>"] += 1
    print("[Migration] target record_kind distribution:")
    for kind in sorted(kind_counter):
        print(f"    {kind:<16} {kind_counter[kind]}")

    origin_backfill = sum(1 for _, sf in planned if "origin" in sf)
    plan_type_fix = sum(1 for _, sf in planned if "plan_type" in sf)
    kind_stamp = sum(1 for _, sf in planned if "record_kind" in sf)
    print(
        f"[Migration] changes: record_kind stamp={kind_stamp}, "
        f"origin backfill={origin_backfill}, plan_type fix={plan_type_fix}, "
        f"docs touched={len(planned)}"
    )

    if unclassifiable:
        print(f"[Migration] WARNING: {len(unclassifiable)} unclassifiable documents:")
        for doc in unclassifiable:
            print(
                f"    plan_id={doc.get('plan_id')!r} "
                f"plan_type={doc.get('plan_type')!r} "
                f"manual_action={doc.get('manual_action')!r}"
            )

    if not args.apply:
        print("[Migration] DRY-RUN — no writes. Re-run with --apply to commit.")
        return 1 if unclassifiable else 0

    if unclassifiable:
        print("[Migration] ABORT: refusing to apply while documents are unclassifiable.")
        return 1

    if not planned:
        print("[Migration] Nothing to change — already normalized.")
        return 0

    # Backup before mutating.
    stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    backup_name = f"{COLLECTION}_backup_record_kind_{stamp}"
    if docs:
        client[trading_db_name][backup_name].insert_many([dict(d) for d in docs])
        print(f"[Migration] Backed up {len(docs)} docs to {trading_db_name}.{backup_name}")

    for doc, set_fields in planned:
        coll.update_one({"_id": doc["_id"]}, {"$set": set_fields})
    print(f"[Migration] Applied changes to {len(planned)} documents")

    # Verify: every document now carries a valid record_kind.
    missing = coll.count_documents({"record_kind": {"$nin": sorted(VALID_RECORD_KINDS)}})
    if missing:
        print(f"[Migration] ERROR: {missing} documents still lack a valid record_kind")
        return 1

    print("[Migration] Done. All documents carry a valid record_kind.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
