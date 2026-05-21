"""
Recompute Fire Eye focus reviews for historical market insight events.

What this script does
---------------------
Fire Eye ("火眼金睛") writes intraday events to ``market_insight_events``.
Post-market validation later writes one review per focus event to
``market_insight_focus_reviews`` and aggregates historical quality stats into
``market_insight_global_quality``.

This script rebuilds those historical focus reviews with the *current* review
logic in ``services.market_insight_engine``. Use it after changing review
criteria such as:

- what counts as confirmed / faded / unconfirmed;
- event-specific validation rules for sector diffusion, early diffusion, or
  counter-trend diffusion;
- the review horizon used by ``review_market_insight_focus``.

It discovers all historical trade dates from ``market_insight_events``, reruns
``review_market_insight_focus`` for each date, and upserts reviews by
``focus_key``. Existing review documents for the same ``focus_key`` are
overwritten with the newly computed status and metrics. At the end, it rebuilds
``market_insight_global_quality`` once.

Safety model
------------
The default mode is explicit: choose either ``--dry-run`` or ``--apply``.

- ``--dry-run`` only prints which trade dates would be recomputed. It does not
  write to MongoDB.
- ``--apply`` writes recalculated reviews and rebuilds global quality stats.
- The script does not delete old documents. It overwrites matching ``focus_key``
  reviews. If future code changes the ``focus_key`` format, old reviews may
  remain alongside newly keyed reviews and should be handled by a migration.

Typical usage
-------------
Run from the ``quantFinance`` project root:

  python tools/recompute_market_insight_focus_reviews.py --dry-run
  python tools/recompute_market_insight_focus_reviews.py --apply

Limit the recompute to a date range:

  python tools/recompute_market_insight_focus_reviews.py --dry-run --start-date 20260501 --end-date 20260520
  python tools/recompute_market_insight_focus_reviews.py --apply --start-date 20260501 --end-date 20260520

Use a non-default validation horizon:

  python tools/recompute_market_insight_focus_reviews.py --apply --horizon-minutes 120

Output
------
In ``--dry-run`` mode, the script prints a JSON summary containing the selected
trade dates. In ``--apply`` mode, it prints one JSON line per recomputed trade
date and then a final summary including ``total_written`` and
``quality_count``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Iterable, List, Optional


ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from db import get_db_collections  # noqa: E402
from services.market_insight_engine import (  # noqa: E402
    rebuild_market_insight_global_quality,
    review_market_insight_focus,
)


def _compact_date(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text.replace("-", "")[:8]


def _date_in_range(date_value: str, start_date: Optional[str], end_date: Optional[str]) -> bool:
    if start_date and date_value < start_date:
        return False
    if end_date and date_value > end_date:
        return False
    return True


def _load_trade_dates(start_date: Optional[str], end_date: Optional[str]) -> List[str]:
    events = get_db_collections()["market_insight_events"]
    try:
        raw_dates: Iterable[Any] = events.distinct("trade_date")
    except Exception:
        raw_dates = [doc.get("trade_date") for doc in events.find({}, {"trade_date": 1})]

    dates = sorted(
        {
            date
            for raw in raw_dates
            if (date := _compact_date(raw)) and _date_in_range(date, start_date, end_date)
        }
    )
    return dates


def main() -> int:
    parser = argparse.ArgumentParser(description="Recompute Fire Eye focus reviews for historical dates.")
    parser.add_argument("--start-date", help="Inclusive start date, YYYYMMDD or YYYY-MM-DD.")
    parser.add_argument("--end-date", help="Inclusive end date, YYYYMMDD or YYYY-MM-DD.")
    parser.add_argument("--horizon-minutes", type=int, default=60, help="Review horizon in minutes. Default: 60.")
    parser.add_argument("--dry-run", action="store_true", help="Only print dates that would be recomputed.")
    parser.add_argument("--apply", action="store_true", help="Actually recompute reviews and rebuild quality stats.")
    args = parser.parse_args()

    if args.dry_run and args.apply:
        parser.error("--dry-run and --apply cannot be used together")
    if not args.dry_run and not args.apply:
        parser.error("choose either --dry-run or --apply")
    if args.horizon_minutes < 15 or args.horizon_minutes > 240:
        parser.error("--horizon-minutes must be between 15 and 240")

    start_date = _compact_date(args.start_date)
    end_date = _compact_date(args.end_date)
    dates = _load_trade_dates(start_date, end_date)

    if args.dry_run:
        print(
            json.dumps(
                {
                    "success": True,
                    "dry_run": True,
                    "trade_date_count": len(dates),
                    "trade_dates": dates,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    per_date = []
    total_written = 0
    for trade_date in dates:
        result = review_market_insight_focus(
            trade_date=trade_date,
            horizon_minutes=args.horizon_minutes,
            rebuild_quality=False,
        )
        written = int(result.get("written") or 0)
        total_written += written
        per_date.append({"trade_date": trade_date, "written": written})
        print(json.dumps(per_date[-1], ensure_ascii=False))

    quality = rebuild_market_insight_global_quality()
    summary = {
        "success": True,
        "dry_run": False,
        "horizon_minutes": args.horizon_minutes,
        "trade_date_count": len(dates),
        "total_written": total_written,
        "quality_count": quality.get("count"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
