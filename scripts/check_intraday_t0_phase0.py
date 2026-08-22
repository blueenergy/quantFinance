#!/usr/bin/env python3
"""Intraday T+0 health check CLI (see llm-wiki projects/trading/intraday-t0-rollout.md).

Modes:
  liveness  — dev/CI: Mongo, indexes, engine smoke (no live market data required)
  readiness — prod pre-flight: liveness + today's realtime quotes / spectrum

Usage (from quantFinance repo root):
  python scripts/check_intraday_t0_phase0.py
  python scripts/check_intraday_t0_phase0.py --mode readiness --trade-date 20260615
  python scripts/check_intraday_t0_phase0.py --mode readiness --user-id <uuid> --json

Exit code 0 when overall is pass or warn; 1 when overall is fail.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def main() -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    parser = argparse.ArgumentParser(description="Intraday T+0 health check")
    parser.add_argument(
        "--mode",
        choices=("liveness", "readiness"),
        default="liveness",
        help="liveness=dev/CI, readiness=prod pre-flight (default: liveness)",
    )
    parser.add_argument("--trade-date", help="YYYYMMDD for readiness (default: today)")
    parser.add_argument("--user-id", help="Optional user id to scope signal/position checks")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    args = parser.parse_args()

    from services.intraday_t0_health import run_liveness_checks, run_readiness_checks

    if args.mode == "liveness":
        report = run_liveness_checks()
    else:
        report = run_readiness_checks(trade_date=args.trade_date, user_id=args.user_id)

    payload = report.to_dict()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"Intraday T+0 health — mode={report.mode} trade_date={report.trade_date}")
        print("=" * 60)
        for check in report.checks:
            tag = check.status.upper()
            req = "" if check.required else " (optional)"
            print(f"[{tag}] {check.name}{req}: {check.detail}")
        print("=" * 60)
        print(f"Overall: {report.overall.upper()}")

    return 0 if report.overall in {"pass", "warn"} else 1


if __name__ == "__main__":
    sys.exit(main())
