#!/usr/bin/env python3
"""Reconcile portfolio_live_snapshots against broker-synced positions."""

from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from db import get_db_collections, get_trading_db_collections  # noqa: E402
from services.portfolio_live_snapshot import (  # noqa: E402
    reconcile_live_snapshots_with_broker_positions,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", default=None, help="Limit reconcile to one user")
    parser.add_argument(
        "--no-rebuild",
        action="store_true",
        help="Only report diffs; do not rebuild stale snapshots",
    )
    args = parser.parse_args()

    result = reconcile_live_snapshots_with_broker_positions(
        get_trading_db_collections(),
        get_db_collections(),
        user_id=args.user_id,
        rebuild_on_diff=not args.no_rebuild,
    )
    print(result)


if __name__ == "__main__":
    main()
