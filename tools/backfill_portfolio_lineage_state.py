#!/usr/bin/env python3
"""Backfill portfolio_lineage_controls summary fields and execution_venue."""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from typing import Any, Dict, List, Tuple

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from db import get_db_collections, get_trading_db_collections  # noqa: E402
from routers.portfolio_plans import (  # noqa: E402
    PAPER_ACTIVE_STATUSES,
    _account_id_from_filled_signals,
    _portfolio_param_summary,
    _resilient_record_kind,
)
from services.portfolio_lineage_state import (  # noqa: E402
    build_summary_from_lineage_plans,
    upsert_lineage_summary,
)
from services.portfolio_record_kind import RECORD_KIND_TRADE_PLAN  # noqa: E402


def _group_plans(plans: List[Dict[str, Any]]) -> Dict[Tuple[str, str, str], List[Dict[str, Any]]]:
    groups: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = defaultdict(list)
    for plan in plans:
        user_id = str(plan.get("user_id") or "")
        stid = plan.get("strategy_template_id")
        phash = plan.get("params_hash")
        if not user_id or not stid or not phash:
            continue
        groups[(user_id, str(stid), str(phash))].append(plan)
    return groups


def backfill(*, user_id: str | None = None, dry_run: bool = False) -> int:
    trading = get_trading_db_collections()
    finance = get_db_collections()
    controls = trading.get("portfolio_lineage_controls")
    query: Dict[str, Any] = {"params_hash": {"$exists": True, "$nin": [None, ""]}}
    if user_id:
        query["user_id"] = user_id
    plans = list(
        trading["portfolio_trade_plans"].find(
            query,
            {
                "_id": 0,
                "plan_id": 1,
                "user_id": 1,
                "strategy_template_id": 1,
                "params_hash": 1,
                "base_date": 1,
                "strategy_name": 1,
                "status": 1,
                "plan_type": 1,
                "record_kind": 1,
                "params_snapshot": 1,
            },
        )
    )
    groups = _group_plans(plans)
    plan_group_by_id: Dict[str, Tuple[str, str, str]] = {}
    for key, rows in groups.items():
        for row in rows:
            if row.get("plan_id"):
                plan_group_by_id[str(row["plan_id"])] = key

    plan_ids = sorted(plan_group_by_id)
    signals_by_group: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = defaultdict(list)
    executions_by_group: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = defaultdict(list)
    if plan_ids:
        signals = list(
            finance["trade_signals"].find(
                {"plan_id": {"$in": plan_ids}, "mode": "live"},
                {"_id": 0, "plan_id": 1, "order_id": 1, "securities_account_id": 1, "user_id": 1},
            )
        )
        order_group_by_id: Dict[str, Tuple[str, str, str]] = {}
        for signal in signals:
            key = plan_group_by_id.get(str(signal.get("plan_id") or ""))
            if not key:
                continue
            if user_id and key[0] != user_id:
                continue
            signals_by_group[key].append(signal)
            if signal.get("order_id"):
                order_group_by_id[str(signal["order_id"])] = key
        order_ids = sorted(order_group_by_id)
        if order_ids:
            executions = list(
                finance["trade_executions"].find(
                    {"order_id": {"$in": order_ids}},
                    {"_id": 0, "order_id": 1, "filled_size": 1, "quantity": 1, "filled_qty": 1},
                )
            )
            for execution in executions:
                key = order_group_by_id.get(str(execution.get("order_id") or ""))
                if key:
                    executions_by_group[key].append(execution)

    updated = 0
    for key, all_lineage in groups.items():
        uid, strategy_template_id, params_hash = key
        if user_id and uid != user_id:
            continue
        has_live = bool(signals_by_group.get(key))
        has_paper = any(row.get("status") in PAPER_ACTIVE_STATUSES for row in all_lineage)
        if not has_live and not has_paper:
            continue
        signals = signals_by_group.get(key, [])
        executions = executions_by_group.get(key, [])
        sec_id = ""
        if has_live:
            sec_id = _account_id_from_filled_signals(signals, executions)
            if not sec_id:
                for signal in reversed(signals):
                    sec_id = str(signal.get("securities_account_id") or "")
                    if sec_id:
                        break
        existing_control = {}
        if controls is not None:
            existing_control = (
                controls.find_one(
                    {
                        "user_id": uid,
                        "strategy_template_id": strategy_template_id,
                        "params_hash": params_hash,
                    },
                    {"_id": 0},
                )
                or {}
            )
        summary = build_summary_from_lineage_plans(
            all_lineage,
            strategy_template_id=strategy_template_id,
            params_hash=params_hash,
            param_summary_fn=_portfolio_param_summary,
            record_kind_fn=_resilient_record_kind,
            trade_plan_kind=RECORD_KIND_TRADE_PLAN,
            has_live_signals=has_live,
            securities_account_id=sec_id,
            lineage_control=existing_control,
        )
        if dry_run:
            print(f"[dry-run] {uid} {strategy_template_id}:{params_hash[:8]} -> {summary.get('execution_venue')}")
        else:
            upsert_lineage_summary(
                controls,
                user_id=uid,
                strategy_template_id=strategy_template_id,
                params_hash=params_hash,
                summary=summary,
            )
        updated += 1
    return updated


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", default=None, help="Limit backfill to one user")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without writing")
    args = parser.parse_args()
    count = backfill(user_id=args.user_id, dry_run=args.dry_run)
    print(f"processed {count} lineage group(s)")


if __name__ == "__main__":
    main()
