"""Test to ensure the (symbol, trade_date) compound index exists on money_flow collection.

This guards against accidental removal or rename of the index that the collector
relies on for efficient upserts.

Behavior:
  * Imports money_flow_collector (which runs _ensure_money_flow_indexes on import)
  * Retrieves index_information from the bound collection
  * Asserts an index key exactly [('symbol', 1), ('trade_date', 1)] is present
  * If MongoDB is unreachable (e.g., CI without service), test is skipped instead of failing
"""

import pytest
import importlib

try:  # Attempt dynamic import; if it fails (e.g. env missing pymongo), skip module cleanly
    mfc = importlib.import_module("money_flow_collector")  # noqa: F401
except Exception as import_exc:  # pragma: no cover
    pytest.skip(f"Cannot import money_flow_collector: {import_exc}", allow_module_level=True)


def test_money_flow_symbol_trade_date_index():
    collection = getattr(mfc, 'money_flow_collection', None)
    if collection is None:
        pytest.skip("money_flow_collection not available")
    try:
        info = collection.index_information()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"Cannot access MongoDB to inspect indexes: {exc}")

    expected_key = [('symbol', 1), ('trade_date', 1)]
    keys_present = [meta.get('key') for meta in info.values()]
    assert any(k == expected_key for k in keys_present), (
        f"Missing compound index (symbol, trade_date). Existing keys: {keys_present}"
    )
