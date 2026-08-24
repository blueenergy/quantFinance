"""
Create essential MongoDB indexes for performance.

Indexes:
- stock_scores: (symbol, score_date desc)
- stock_scores: (score_date desc, composite_score desc)
- stock_scores: (composite_score desc)
- stock_info: (symbol)
- trade_signals: (user_id, plan_id), (user_id, order_id), (user_id, timestamp)
- trade_executions: (user_id, plan_id), (user_id, order_id), (user_id, timestamp)

Run:
    python tools/create_indexes.py
"""

from typing import List, Tuple, Dict, Any

try:
    from db import get_db_collections
except Exception as e:
    raise RuntimeError("Failed to import get_db_collections from db.py") from e


def _existing_index_keys(collection) -> List[List[Tuple[str, int]]]:
    keys: List[List[Tuple[str, int]]] = []
    for idx in collection.list_indexes():
        key_doc = idx.get('key') or {}
        # Convert to list of (field, direction) preserving order
        keys.append(list(key_doc.items()))
    return keys


def _ensure_collection_indexes(
    collection,
    indexes: List[Tuple[List[Tuple[str, int]], dict]],
    existing: List[List[Tuple[str, int]]],
) -> None:
    def has_key(target: List[Tuple[str, int]]) -> bool:
        return any(existing_item == target for existing_item in existing)

    for keys, opts in indexes:
        if not has_key(keys):
            collection.create_index(keys, **opts)
            existing.append(keys)


def ensure_indexes():
    db = get_db_collections()
    stock_scores = db["stock_scores"]
    stock_info = db["stock_info"]
    trade_signals = db["trade_signals"]
    trade_executions = db["trade_executions"]

    # Define indexes: (keys, options)
    score_indexes: List[Tuple[List[Tuple[str, int]], dict]] = [
        # One score per symbol per day, enforced by the database. Writers upsert
        # on exactly this filter, but nothing stopped a second pass from
        # inserting a parallel copy: 20260421 carried two scores for every
        # symbol for four months. Ascending because the descending twin below
        # answers the mixed-direction ranking sort, and two indexes cannot share
        # a key pattern with conflicting options.
        ([("symbol", 1), ("score_date", 1)], {"name": "symbol_scoreDate_uniq", "unique": True}),
        # Latest-per-symbol queries
        ([("symbol", 1), ("score_date", -1)], {"name": "symbol_scoreDate_desc"}),
        # Date-filtered ranking queries
        ([("score_date", -1), ("composite_score", -1)], {"name": "date_composite_desc"}),
        # Sort-only by composite
        ([("composite_score", -1)], {"name": "composite_desc"}),
    ]

    info_indexes: List[Tuple[List[Tuple[str, int]], dict]] = [
        ([("symbol", 1)], {"name": "symbol_unique", "unique": False}),
    ]

    trade_signal_indexes: List[Tuple[List[Tuple[str, int]], dict]] = [
        ([("user_id", 1), ("plan_id", 1)], {"name": "user_plan"}),
        ([("user_id", 1), ("order_id", 1)], {"name": "user_order_id"}),
        ([("user_id", 1), ("timestamp", -1)], {"name": "user_timestamp_desc"}),
    ]

    trade_execution_indexes: List[Tuple[List[Tuple[str, int]], dict]] = [
        ([("user_id", 1), ("plan_id", 1)], {"name": "user_plan"}),
        ([("user_id", 1), ("order_id", 1)], {"name": "user_order_id"}),
        ([("user_id", 1), ("timestamp", -1)], {"name": "user_timestamp_desc"}),
    ]

    # Create indexes idempotently, skipping if same key exists with any name
    existing_scores = _existing_index_keys(stock_scores)
    existing_info = _existing_index_keys(stock_info)
    existing_signals = _existing_index_keys(trade_signals)
    existing_executions = _existing_index_keys(trade_executions)

    _ensure_collection_indexes(stock_scores, score_indexes, existing_scores)
    _ensure_collection_indexes(stock_info, info_indexes, existing_info)
    _ensure_collection_indexes(trade_signals, trade_signal_indexes, existing_signals)
    _ensure_collection_indexes(trade_executions, trade_execution_indexes, existing_executions)

    print("✅ Index creation complete:")
    print("- stock_scores: symbol+score_date(desc), score_date(desc)+composite(desc), composite(desc)")
    print("- stock_info: symbol")
    print("- trade_signals: user_id+plan_id, user_id+order_id, user_id+timestamp(desc)")
    print("- trade_executions: user_id+plan_id, user_id+order_id, user_id+timestamp(desc)")


if __name__ == "__main__":
    ensure_indexes()
