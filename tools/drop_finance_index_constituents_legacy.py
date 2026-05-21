#!/usr/bin/env python3
"""Drop legacy finance.index_constituents if it exists (canonical data is quant_data only).

Loads quantFinance/.env like other tools. Default is dry-run.

Usage:
  python quantFinance/tools/drop_finance_index_constituents_legacy.py
  python quantFinance/tools/drop_finance_index_constituents_legacy.py --drop --i-am-sure
"""
from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv
from pymongo import MongoClient

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
load_dotenv(os.path.join(_ROOT, ".env"), override=False)
load_dotenv(override=False)


def main() -> int:
    p = argparse.ArgumentParser(description="Remove legacy finance.index_constituents collection")
    p.add_argument(
        "--drop",
        action="store_true",
        help="Actually drop the collection (default: only print stats)",
    )
    p.add_argument(
        "--i-am-sure",
        action="store_true",
        help="Required with --drop to avoid accidental deletion",
    )
    args = p.parse_args()

    uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    finance_db = os.getenv("MONGO_DB", "finance")
    data_db = os.getenv("DATA_DB_NAME", "quant_data")

    client = MongoClient(uri)
    fin = client[finance_db]
    qd = client[data_db]

    legacy = fin["index_constituents"]
    canonical = qd["index_constituents"]

    n_legacy = legacy.estimated_document_count()
    n_canonical = canonical.estimated_document_count()

    print(f"MONGO_URI host: {uri.split('@')[-1] if '@' in uri else uri}")
    print(f"finance.{legacy.name} estimated_document_count={n_legacy}")
    print(f"{data_db}.{canonical.name} estimated_document_count={n_canonical}")

    if n_legacy == 0:
        print("Nothing to do (legacy collection empty or missing).")
        return 0

    if not args.drop:
        print("Dry-run only. Re-run with --drop --i-am-sure to drop finance.index_constituents.")
        return 0

    if not args.i_am_sure:
        print("Refusing to drop without --i-am-sure", file=sys.stderr)
        return 2

    legacy.drop()
    print(f"Dropped {finance_db}.index_constituents")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
