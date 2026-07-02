#!/usr/bin/env python3
"""Backfill portfolio_live_snapshots for all live lineages."""

from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from db import get_db_collections, get_trading_db_collections  # noqa: E402
from services.portfolio_live_snapshot import (  # noqa: E402
    ensure_live_snapshot_indexes,
    rebuild_all_live_snapshots,
)


def backfill(*, user_id: str | None = None, dry_run: bool = False) -> dict:
    trading = get_trading_db_collections()
    finance = get_db_collections()
    ensure_live_snapshot_indexes(trading, finance)
    if dry_run:
        from services.portfolio_live_snapshot import list_live_lineage_keys

        keys = list_live_lineage_keys(finance)
        if user_id:
            keys = [row for row in keys if row.get("user_id") == user_id]
        print(f"[dry-run] would rebuild {len(keys)} lineage anchor plan(s)")
        return {"written": 0, "skipped": 0, "lineages": len(keys), "dry_run": True}
    return rebuild_all_live_snapshots(trading, finance, user_id=user_id)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", default=None, help="Limit backfill to one user")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without writing")
    args = parser.parse_args()
    result = backfill(user_id=args.user_id, dry_run=args.dry_run)
    print(result)


if __name__ == "__main__":
    main()
