"""
Create essential MongoDB indexes for performance.

Indexes:
- stock_scores: (symbol, score_date desc)
- stock_scores: (score_date desc, composite_score desc)
- stock_scores: (composite_score desc)
- stock_info: (symbol)

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


def ensure_indexes():
    db = get_db_collections()
    stock_scores = db["stock_scores"]
    stock_info = db["stock_info"]

    # Define indexes: (keys, options)
    score_indexes: List[Tuple[List[Tuple[str, int]], dict]] = [
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

    # Create indexes idempotently, skipping if same key exists with any name
    existing_scores = _existing_index_keys(stock_scores)
    existing_info = _existing_index_keys(stock_info)

    def has_key(existing: List[List[Tuple[str, int]]], target: List[Tuple[str, int]]) -> bool:
        return any(existing_item == target for existing_item in existing)

    for keys, opts in score_indexes:
        if not has_key(existing_scores, keys):
            stock_scores.create_index(keys, **opts)
    for keys, opts in info_indexes:
        if not has_key(existing_info, keys):
            stock_info.create_index(keys, **opts)

    print("✅ Index creation complete:")
    print("- stock_scores: symbol+score_date(desc), score_date(desc)+composite(desc), composite(desc)")
    print("- stock_info: symbol")


if __name__ == "__main__":
    ensure_indexes()
