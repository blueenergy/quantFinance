#!/usr/bin/env python3
"""Check MongoDB indexes needed by portfolio plan APIs.

Default mode is read-only. Use ``--apply`` to create missing indexes.
The script loads configuration from /app/.env first (for containers), then
from the local .env files used in development.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from dotenv import load_dotenv
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.collection import Collection


IndexKey = List[Tuple[str, int]]


@dataclass(frozen=True)
class ExpectedIndex:
    db_attr: str
    collection: str
    keys: IndexKey
    name: str
    reason: str
    unique: bool = False


def _load_env() -> None:
    for env_path in (
        Path("/app/.env"),
        Path(__file__).resolve().parents[1] / ".env",
        Path.cwd() / ".env",
    ):
        if env_path.exists():
            load_dotenv(env_path, override=False)


def _expected_indexes() -> List[ExpectedIndex]:
    return [
        # quant_trading: plan list/detail and lineage lookups.
        ExpectedIndex(
            "trading",
            "portfolio_trade_plans",
            [("plan_id", ASCENDING)],
            "plan_id_1",
            "plan detail lookup by plan_id",
            unique=True,
        ),
        ExpectedIndex(
            "trading",
            "portfolio_trade_plans",
            [("user_id", ASCENDING), ("plan_id", ASCENDING)],
            "user_id_1_plan_id_1",
            "plan detail lookup scoped to current user",
        ),
        ExpectedIndex(
            "trading",
            "portfolio_trade_plans",
            [
                ("user_id", ASCENDING),
                ("strategy_template_id", ASCENDING),
                ("params_hash", ASCENDING),
                ("base_date", ASCENDING),
            ],
            "user_strategy_params_base_date_1",
            "lineage lookup and same-day plan dedupe",
            unique=True,
        ),
        ExpectedIndex(
            "trading",
            "portfolio_trade_plans",
            [("user_id", ASCENDING), ("base_date", DESCENDING)],
            "user_id_1_base_date_-1",
            "portfolio plan list sorted by base_date",
        ),
        ExpectedIndex(
            "trading",
            "portfolio_trade_plan_items",
            [("plan_id", ASCENDING), ("user_id", ASCENDING), ("rank", ASCENDING)],
            "plan_user_rank_1",
            "plan detail items sorted by rank",
        ),
        ExpectedIndex(
            "trading",
            "portfolio_plan_reviews",
            [("plan_id", ASCENDING), ("user_id", ASCENDING), ("created_at", DESCENDING)],
            "plan_user_reviews_created_desc",
            "plan detail review history",
        ),
        ExpectedIndex(
            "trading",
            "portfolio_paper_executions",
            [("plan_id", ASCENDING), ("user_id", ASCENDING), ("created_at", DESCENDING)],
            "plan_user_paper_exec_created_desc",
            "plan paper execution status and executions table",
        ),
        ExpectedIndex(
            "trading",
            "portfolio_plan_generation_tasks",
            [("user_id", ASCENDING), ("created_at", DESCENDING)],
            "user_generation_tasks_created_desc",
            "generation task list",
        ),
        ExpectedIndex(
            "trading",
            "portfolio_worker_status",
            [("worker_type", ASCENDING), ("last_seen_at", DESCENDING)],
            "worker_type_last_seen_desc",
            "portfolio worker status panel",
        ),
        ExpectedIndex(
            "trading",
            "portfolio_paper_equity",
            [
                ("user_id", ASCENDING),
                ("strategy_template_id", ASCENDING),
                ("params_hash", ASCENDING),
                ("date", ASCENDING),
            ],
            "lineage_equity_user_strategy_params_date",
            "lineage paper equity query by params_hash",
        ),
        ExpectedIndex(
            "trading",
            "portfolio_paper_equity",
            [("user_id", ASCENDING), ("plan_id", ASCENDING), ("date", ASCENDING)],
            "lineage_equity_user_plan_date",
            "lineage paper equity query by plan ids",
        ),
        ExpectedIndex(
            "trading",
            "portfolio_paper_positions",
            [
                ("user_id", ASCENDING),
                ("strategy_template_id", ASCENDING),
                ("params_hash", ASCENDING),
                ("as_of_date", DESCENDING),
            ],
            "lineage_positions_user_strategy_params_date",
            "lineage position snapshot query by params_hash",
        ),
        ExpectedIndex(
            "trading",
            "portfolio_paper_positions",
            [("user_id", ASCENDING), ("plan_id", ASCENDING), ("as_of_date", DESCENDING)],
            "lineage_positions_user_plan_date",
            "lineage position snapshot query by plan ids",
        ),
        # finance: live signal/execution context used by plan detail.
        ExpectedIndex(
            "finance",
            "trade_signals",
            [
                ("user_id", ASCENDING),
                ("plan_id", ASCENDING),
                ("mode", ASCENDING),
                ("source", ASCENDING),
                ("symbol", ASCENDING),
            ],
            "plan_live_signals_user_plan_mode_source_symbol",
            "plan detail live signal lookup",
        ),
        ExpectedIndex(
            "finance",
            "trade_signals",
            [
                ("user_id", ASCENDING),
                ("mode", ASCENDING),
                ("securities_account_id", ASCENDING),
                ("created_at", DESCENDING),
            ],
            "live_signals_user_mode_account_created_desc",
            "trader live signal list by account sorted by created_at",
        ),
        ExpectedIndex(
            "finance",
            "trade_signals",
            [("user_id", ASCENDING), ("order_id", ASCENDING)],
            "unique_live_order_id_per_user",
            "live signal lookup and duplicate protection",
            unique=True,
        ),
        ExpectedIndex(
            "finance",
            "trade_executions",
            [("user_id", ASCENDING), ("order_id", ASCENDING)],
            "trade_exec_user_order",
            "plan detail execution lookup by order ids",
        ),
        ExpectedIndex(
            "finance",
            "trade_executions",
            [("user_id", ASCENDING), ("securities_account_id", ASCENDING), ("timestamp", DESCENDING)],
            "trade_exec_user_account_timestamp_desc",
            "trader execution list by account sorted by timestamp",
        ),
        ExpectedIndex(
            "finance",
            "trade_executions",
            [
                ("user_id", ASCENDING),
                ("securities_account_id", ASCENDING),
                ("status", ASCENDING),
                ("timestamp", DESCENDING),
            ],
            "trade_exec_user_account_status_timestamp_desc",
            "trader execution list by account and status sorted by timestamp",
        ),
        ExpectedIndex(
            "finance",
            "trader_positions",
            [("user_id", ASCENDING), ("securities_account_id", ASCENDING), ("symbol", ASCENDING)],
            "trader_positions_user_account_symbol",
            "plan detail account position lookup",
        ),
        ExpectedIndex(
            "finance",
            "trader_accounts",
            [("user_id", ASCENDING), ("securities_account_id", ASCENDING), ("synced_at", DESCENDING)],
            "trader_accounts_user_account_synced_desc",
            "latest account snapshot lookup",
        ),
        # quant_data: execution price checks.
        ExpectedIndex(
            "data",
            "volume_price",
            [("symbol", ASCENDING), ("trade_date", ASCENDING)],
            "volume_price_symbol_trade_date",
            "open/close price lookup for plan execution status",
        ),
        ExpectedIndex(
            "data",
            "volume_price_realtime_latest",
            [("symbol", ASCENDING)],
            "realtime_latest_symbol",
            "realtime equity and price lookups",
        ),
    ]


def _index_matches(existing_keys: IndexKey, expected_keys: IndexKey) -> bool:
    """Return true when an existing index covers the expected prefix."""
    return existing_keys[: len(expected_keys)] == expected_keys


def _format_keys(keys: Iterable[Tuple[str, int]]) -> str:
    return ", ".join(f"{field}:{direction}" for field, direction in keys)


def _collection_indexes(collection: Collection) -> Dict[str, Dict[str, Any]]:
    try:
        return collection.index_information()
    except Exception as exc:  # noqa: BLE001
        return {"__error__": {"error": str(exc), "key": []}}


def _find_covering_index(indexes: Dict[str, Dict[str, Any]], spec: ExpectedIndex) -> Optional[str]:
    for index_name, index_info in indexes.items():
        existing_keys = list(index_info.get("key") or [])
        if not _index_matches(existing_keys, spec.keys):
            continue
        if spec.unique and not index_info.get("unique"):
            continue
        return index_name
    return None


def _create_index(collection: Collection, spec: ExpectedIndex) -> str:
    return collection.create_index(spec.keys, name=spec.name, unique=spec.unique, background=True)


def _mongo_client() -> MongoClient:
    mongo_uri = os.getenv("MONGO_URI")
    if not mongo_uri:
        raise SystemExit("MONGO_URI is not configured. Check /app/.env or local .env.")
    return MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)


def _db_names() -> Dict[str, str]:
    return {
        "finance": os.getenv("MONGO_DB", "finance"),
        "trading": os.getenv("TRADING_DB_NAME", "quant_trading"),
        "data": os.getenv("DATA_DB_NAME", "quant_data"),
    }


def _check_indexes(*, apply: bool = False) -> Dict[str, Any]:
    _load_env()
    client = _mongo_client()
    db_names = _db_names()
    specs = _expected_indexes()
    missing: List[Dict[str, Any]] = []
    present: List[Dict[str, Any]] = []
    created: List[Dict[str, Any]] = []

    try:
        client.admin.command("ping")
        for spec in specs:
            db_name = db_names[spec.db_attr]
            collection = client[db_name][spec.collection]
            indexes = _collection_indexes(collection)
            if "__error__" in indexes:
                missing.append(
                    {
                        "db": db_name,
                        "collection": spec.collection,
                        "name": spec.name,
                        "keys": spec.keys,
                        "reason": spec.reason,
                        "error": indexes["__error__"]["error"],
                    }
                )
                continue
            covering = _find_covering_index(indexes, spec)
            row = {
                "db": db_name,
                "collection": spec.collection,
                "name": spec.name,
                "keys": spec.keys,
                "unique": spec.unique,
                "reason": spec.reason,
            }
            if covering:
                present.append({**row, "covering_index": covering})
                continue
            missing.append(row)
            if apply:
                created_name = _create_index(collection, spec)
                created.append({**row, "created_index": created_name})
    finally:
        client.close()

    return {
        "db_names": db_names,
        "checked": len(specs),
        "present_count": len(present),
        "missing_count": len(missing),
        "created_count": len(created),
        "present": present,
        "missing": missing,
        "created": created,
    }


def _print_report(result: Dict[str, Any]) -> None:
    print("Portfolio plan index check")
    print(f"Databases: {result['db_names']}")
    print(
        f"Checked={result['checked']} present={result['present_count']} "
        f"missing={result['missing_count']} created={result['created_count']}"
    )
    print()
    if result["missing"]:
        print("Missing indexes:")
        for row in result["missing"]:
            unique = " unique" if row.get("unique") else ""
            print(
                f"- {row['db']}.{row['collection']} {row['name']}{unique}: "
                f"{_format_keys(row['keys'])}"
            )
            print(f"  reason: {row['reason']}")
            if row.get("error"):
                print(f"  error: {row['error']}")
    else:
        print("All expected indexes are present.")
    if result["created"]:
        print()
        print("Created indexes:")
        for row in result["created"]:
            print(f"- {row['db']}.{row['collection']} {row['created_index']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Check portfolio plan MongoDB indexes.")
    parser.add_argument("--apply", action="store_true", help="Create missing indexes.")
    parser.add_argument("--json", action="store_true", help="Print JSON report.")
    args = parser.parse_args()

    result = _check_indexes(apply=args.apply)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        _print_report(result)
    return 1 if result["missing"] and not args.apply else 0


if __name__ == "__main__":
    raise SystemExit(main())
