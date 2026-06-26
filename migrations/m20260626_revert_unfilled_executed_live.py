"""Migration: revert ``executed_live`` plans that never actually filled.

A short-lived bug in ``_maybe_stamp_plan_executed_live`` flipped a plan from
``approved`` to ``executed_live`` whenever all of its live signals reached a
terminal status and the execution phase was ``done`` — *without* requiring an
actual fill. So a plan whose live signals were all cancelled / rejected /
failed (zero fills) was wrongly shown as "实盘已执行". The read path is invoked
on the plan-detail endpoint, so any such plan that was opened after the deploy
got persisted with the wrong status.

``executed_live`` was never written by anything other than that read-path
stamp, so this script can safely scan all ``executed_live`` plans and revert
the ones with zero live fills back to ``approved`` (and clear
``live_executed_at``). Plans with at least one fill are left untouched.

Safety:
  - Dry-run by default; prints the plans it would revert and why.
  - ``--apply`` performs the writes, after backing up the affected plans to a
    timestamped collection ``portfolio_trade_plans_backup_executed_live_<stamp>``.

Usage:
    cd quantFinance && python -m migrations.m20260626_revert_unfilled_executed_live          # dry-run
    cd quantFinance && python -m migrations.m20260626_revert_unfilled_executed_live --apply  # write
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

from dotenv import load_dotenv
from pymongo import MongoClient

COLLECTION = "portfolio_trade_plans"


def _filled_qty(row) -> int:
    """Mirror routers.portfolio_plans._signal_filled_qty."""
    filled = int(row.get("filled_qty", row.get("filled_size", 0)) or 0)
    if filled == 0 and str(row.get("status") or "").lower() == "filled":
        return abs(int(row.get("size", 0) or 0))
    return abs(filled)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write changes (default is dry-run).")
    args = parser.parse_args()

    load_dotenv()
    mongo_url = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    trading_db_name = os.getenv("TRADING_DB_NAME", "quant_trading")
    finance_db_name = os.getenv("MONGO_DB", "finance")

    client = MongoClient(mongo_url, serverSelectionTimeoutMS=5000)
    plans = client[trading_db_name][COLLECTION]
    signals = client[finance_db_name]["trade_signals"]

    candidates = list(plans.find({"status": "executed_live"}))
    print(f"[Migration] {trading_db_name}.{COLLECTION}: {len(candidates)} executed_live plans")

    to_revert = []
    for plan in candidates:
        plan_id = plan.get("plan_id")
        user_id = plan.get("user_id")
        live_signals = list(
            signals.find(
                {"user_id": user_id, "plan_id": plan_id, "mode": "live"},
                {"_id": 0, "status": 1, "filled_qty": 1, "filled_size": 1, "size": 1},
            )
        )
        total_filled = sum(_filled_qty(row) for row in live_signals)
        if total_filled <= 0:
            to_revert.append(plan)
            print(
                f"    REVERT plan_id={plan_id!r} user_id={user_id!r} "
                f"signals={len(live_signals)} filled_qty=0 -> approved"
            )

    print(
        f"[Migration] {len(to_revert)} plan(s) to revert "
        f"(of {len(candidates)} executed_live); the rest had real fills."
    )

    if not args.apply:
        print("[Migration] DRY-RUN — no writes. Re-run with --apply to commit.")
        return 0

    if not to_revert:
        print("[Migration] Nothing to revert.")
        return 0

    stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    backup_name = f"{COLLECTION}_backup_executed_live_{stamp}"
    client[trading_db_name][backup_name].insert_many([dict(p) for p in to_revert])
    print(f"[Migration] Backed up {len(to_revert)} docs to {trading_db_name}.{backup_name}")

    for plan in to_revert:
        plans.update_one(
            {"_id": plan["_id"]},
            {"$set": {"status": "approved"}, "$unset": {"live_executed_at": ""}},
        )
    print(f"[Migration] Reverted {len(to_revert)} plan(s) to approved.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
