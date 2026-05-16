"""
Rebuild Fire Eye user preference profiles from historical focus feedback.

Usage:
  python tools/rebuild_market_insight_user_preferences.py --dry-run
  python tools/rebuild_market_insight_user_preferences.py --apply
  python tools/rebuild_market_insight_user_preferences.py --user-id u1 --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys


ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from services.market_insight_engine import rebuild_market_insight_user_preferences  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Rebuild market insight user preference profiles.")
    parser.add_argument("--user-id", help="Only rebuild preferences for one user.")
    parser.add_argument("--dry-run", action="store_true", help="Compute the rebuild result without writing to MongoDB.")
    parser.add_argument("--apply", action="store_true", help="Write rebuilt preferences to MongoDB.")
    args = parser.parse_args()

    if args.dry_run and args.apply:
        parser.error("--dry-run and --apply cannot be used together")
    if not args.dry_run and not args.apply:
        parser.error("choose either --dry-run or --apply")

    result = rebuild_market_insight_user_preferences(user_id=args.user_id, dry_run=args.dry_run)
    summary = {
        "success": result.get("success"),
        "dry_run": result.get("dry_run"),
        "user_id": result.get("user_id"),
        "processed_feedback_count": result.get("processed_feedback_count"),
        "skipped_feedback_count": result.get("skipped_feedback_count"),
        "preference_count": result.get("count"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
