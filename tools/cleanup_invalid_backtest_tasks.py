#!/usr/bin/env python3
"""Mark legacy malformed pending backtest tasks as failed.

Run offline (cron / manual), not on the worker poll hot path:

  python tools/cleanup_invalid_backtest_tasks.py
  python tools/cleanup_invalid_backtest_tasks.py --dry-run
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

load_dotenv()

from db import get_db_collections  # noqa: E402

INVALID_PENDING_FILTER = {
    "status": "pending",
    "$or": [
        {"start_date": {"$exists": False}},
        {"end_date": {"$exists": False}},
        {"start_date": {"$in": ["", None]}},
        {"end_date": {"$in": ["", None]}},
    ],
}

FAIL_UPDATE = {
    "$set": {
        "status": "failed",
        "error_message": "Invalid backtest task: start_date/end_date are required",
        "completed_at": datetime.now(),
    }
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Quarantine malformed pending backtest tasks")
    parser.add_argument("--dry-run", action="store_true", help="Count only, do not update")
    args = parser.parse_args()

    col = get_db_collections()["backtest_tasks"]
    count = col.count_documents(INVALID_PENDING_FILTER)
    print(f"Malformed pending backtest tasks: {count}")

    if count == 0:
        return 0

    if args.dry_run:
        print("Dry run — no changes applied.")
        return 0

    result = col.update_many(INVALID_PENDING_FILTER, FAIL_UPDATE)
    print(f"Marked failed: {result.modified_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
