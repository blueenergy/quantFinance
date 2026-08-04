"""Unit tests for multi-combo backtest batch creation."""

from datetime import datetime

import pytest
from fastapi import HTTPException

from services.backtest_batch_schemas import BacktestBatchCreate, BacktestCombo
from services.backtest_batch_service import (
    MAX_COMPARE_TASKS,
    compute_combo_key,
    create_batch,
    infer_asset_type_from_symbol,
    list_batch_result_rows,
    refresh_batch_summary,
)


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    def _matches(self, doc, query):
        if not query:
            return True
        if "$and" in query:
            return all(self._matches(doc, sub) for sub in query["$and"])
        if "$or" in query:
            return any(self._matches(doc, sub) for sub in query["$or"])
        for key, value in query.items():
            doc_value = doc.get(key)
            if isinstance(value, dict):
                if "$in" in value and doc_value not in value["$in"]:
                    return False
                if "$exists" in value:
                    exists = key in doc
                    if exists != value["$exists"]:
                        return False
                continue
            if doc_value != value:
                return False
        return True

    def find(self, query=None, projection=None):
        class Cursor(list):
            def sort(self, field, direction=None):
                if isinstance(field, list):
                    field, direction = field[0]
                reverse = direction == -1
                return Cursor(sorted(self, key=lambda x: x.get(field, ""), reverse=reverse))

        filtered = [d.copy() for d in self.docs if self._matches(d, query or {})]
        if projection:
            out = []
            for doc in filtered:
                if projection.get("_id") == 0:
                    doc = {k: v for k, v in doc.items() if k != "_id"}
                out.append(doc)
            filtered = out
        return Cursor(filtered)

    def find_one(self, query):
        for doc in self.docs:
            if self._matches(doc, query):
                return doc.copy()
        return None

    def insert_one(self, doc):
        self.docs.append(doc.copy())

    def insert_many(self, docs):
        self.docs.extend([d.copy() for d in docs])

    def update_one(self, query, update):
        for doc in self.docs:
            if self._matches(doc, query):
                if "$set" in update:
                    doc.update(update["$set"])
                return type("R", (), {"modified_count": 1})()
        return type("R", (), {"modified_count": 0})()


@pytest.mark.unit
def test_compute_combo_key_stable_across_calls():
    key1 = compute_combo_key("turtle", "turtle_conservative", {"period": 20})
    key2 = compute_combo_key("turtle", "turtle_conservative", {"period": 20})
    assert key1 == key2
    assert len(key1) == 64


@pytest.mark.unit
def test_create_batch_without_combos_legacy_scan():
    collections = {
        "backtest_batches": FakeCollection(),
        "backtest_tasks": FakeCollection(),
    }
    payload = BacktestBatchCreate(
        symbols=["000001.SZ", "000002.SZ"],
        strategy_key="turtle",
        strategy_params={"period": 20},
        start_date="20240101",
        end_date="20241231",
    )
    result = create_batch(collections, {"id": "user1"}, payload)
    assert result["created_tasks"] == 2
    assert collections["backtest_tasks"].docs[0]["batch_index"] == 0
    assert collections["backtest_tasks"].docs[1]["batch_index"] == 1


@pytest.mark.unit
def test_create_batch_with_two_combos_one_symbol():
    collections = {
        "backtest_batches": FakeCollection(),
        "backtest_tasks": FakeCollection(),
    }
    payload = BacktestBatchCreate(
        symbols=["600519.SH"],
        strategy_key="turtle",
        strategy_params={"period": 20},
        start_date="20240101",
        end_date="20241231",
        experiment_type="compare",
        combos=[
            BacktestCombo(strategy_key="turtle", preset="turtle_conservative", strategy_params={"period": 10}),
            BacktestCombo(strategy_key="grid", preset=None, strategy_params={"grid_size": 0.02}),
        ],
    )
    result = create_batch(collections, {"id": "user1"}, payload)
    assert result["created_tasks"] == 2
    keys = {t["combo_key"] for t in collections["backtest_tasks"].docs}
    assert len(keys) == 2
    batch = collections["backtest_batches"].docs[0]
    assert batch["experiment_type"] == "compare"
    assert batch["combos_count"] == 2


@pytest.mark.unit
def test_create_batch_compare_multi_symbols_cartesian():
    collections = {
        "backtest_batches": FakeCollection(),
        "backtest_tasks": FakeCollection(),
    }
    payload = BacktestBatchCreate(
        symbols=["000001.SZ", "510300.SH"],
        asset_type="stock",
        strategy_key="turtle",
        strategy_params={"period": 20},
        start_date="20240101",
        end_date="20241231",
        experiment_type="compare",
        combos=[
            BacktestCombo(strategy_key="turtle", preset="default", strategy_params={"period": 10}),
            BacktestCombo(strategy_key="grid", preset=None, strategy_params={"grid_size": 0.02}),
        ],
    )
    result = create_batch(collections, {"id": "user1"}, payload)
    assert result["created_tasks"] == 4
    tasks = collections["backtest_tasks"].docs
    assert {t["symbol"] for t in tasks} == {"000001.SZ", "510300.SH"}
    by_symbol = {t["symbol"]: t["asset_type"] for t in tasks}
    assert by_symbol["000001.SZ"] == "stock"
    assert by_symbol["510300.SH"] == "etf"
    batch = collections["backtest_batches"].docs[0]
    assert batch["symbols_count"] == 2
    assert batch["combos_count"] == 2
    assert batch["symbols_preview"] == ["000001.SZ", "510300.SH"]


@pytest.mark.unit
def test_create_batch_rejects_duplicate_combo_key():
    collections = {
        "backtest_batches": FakeCollection(),
        "backtest_tasks": FakeCollection(),
    }
    payload = BacktestBatchCreate(
        symbols=["600519.SH"],
        strategy_key="turtle",
        strategy_params={"period": 20},
        start_date="20240101",
        end_date="20241231",
        experiment_type="compare",
        combos=[
            BacktestCombo(strategy_key="turtle", strategy_params={"period": 10}),
            BacktestCombo(strategy_key="turtle", strategy_params={"period": 10}),
        ],
    )
    with pytest.raises(HTTPException) as exc:
        create_batch(collections, {"id": "user1"}, payload)
    assert exc.value.status_code == 422


@pytest.mark.unit
def test_create_batch_rejects_window_too_short_for_strategy():
    """The whole batch is refused up front rather than partial_failing later."""
    collections = {
        "backtest_batches": FakeCollection(),
        "backtest_tasks": FakeCollection(),
        "strategy_metadata": FakeCollection([{"key": "turtle", "min_bars": 50}]),
    }
    payload = BacktestBatchCreate(
        symbols=["000001.SZ", "000002.SZ"],
        strategy_key="turtle",
        strategy_params={"period": 20},
        start_date="20240101",
        end_date="20240131",
    )

    with pytest.raises(HTTPException) as exc:
        create_batch(collections, {"id": "user1"}, payload)

    assert exc.value.status_code == 422
    assert collections["backtest_batches"].docs == []
    assert collections["backtest_tasks"].docs == []


@pytest.mark.unit
def test_create_batch_compare_limit_enforced():
    collections = {
        "backtest_batches": FakeCollection(),
        "backtest_tasks": FakeCollection(),
    }
    combos = [
        BacktestCombo(strategy_key=f"s{i}", strategy_params={"p": i})
        for i in range(MAX_COMPARE_TASKS + 1)
    ]
    payload = BacktestBatchCreate(
        symbols=["600519.SH"],
        strategy_key="s0",
        strategy_params={"p": 0},
        start_date="20240101",
        end_date="20241231",
        experiment_type="compare",
        combos=combos,
    )
    with pytest.raises(HTTPException) as exc:
        create_batch(collections, {"id": "user1"}, payload)
    assert exc.value.status_code == 422


@pytest.mark.unit
def test_scan_batch_allows_many_symbols_without_compare_limit():
    collections = {
        "backtest_batches": FakeCollection(),
        "backtest_tasks": FakeCollection(),
    }
    symbols = [f"{i:06d}.SZ" for i in range(600)]
    payload = BacktestBatchCreate(
        symbols=symbols,
        strategy_key="turtle",
        strategy_params={"period": 20},
        start_date="20240101",
        end_date="20241231",
    )
    result = create_batch(collections, {"id": "user1"}, payload)
    assert result["created_tasks"] == 600


@pytest.mark.unit
def test_list_batch_result_rows_includes_combo_fields():
    batch_id = "bb_test"
    collections = {
        "backtest_batches": FakeCollection([{"batch_id": batch_id, "user_id": "u1"}]),
        "backtest_tasks": FakeCollection(
            [
                {
                    "task_id": "t1",
                    "batch_id": batch_id,
                    "user_id": "u1",
                    "symbol": "600519.SH",
                    "asset_type": "stock",
                    "status": "completed",
                    "strategy_key": "turtle",
                    "preset": "p1",
                    "strategy_params": {"period": 10},
                    "combo_key": "abc",
                    "combo_index": 0,
                    "batch_index": 0,
                }
            ]
        ),
        "backtest_results": FakeCollection(
            [{"task_id": "t1", "metrics": {"total_return": 0.1, "total_trades": 5}}]
        ),
        "stock_info": FakeCollection([{"symbol": "600519.SH", "name": "茅台"}]),
    }
    payload = list_batch_result_rows(collections, batch_id, "u1", limit=500)
    assert payload["total"] == 1
    row = payload["rows"][0]
    assert row["combo_key"] == "abc"
    assert row["strategy_params"]["period"] == 10
    assert row["total_return"] == 0.1


@pytest.mark.unit
def test_infer_asset_type_from_symbol_chi_next_stock():
    assert infer_asset_type_from_symbol("300196.SZ", "etf") == "stock"
    assert infer_asset_type_from_symbol("510300.SH", "stock") == "etf"


@pytest.mark.unit
def test_create_batch_coerces_mislabeled_stock_asset_type():
    collections = {
        "backtest_batches": FakeCollection(),
        "backtest_tasks": FakeCollection(),
    }
    user = {"id": "u1", "username": "tester"}
    payload = BacktestBatchCreate(
        symbols=["300196.SZ"],
        asset_type="etf",
        universe_type="manual",
        strategy_key="atr_breakout",
        strategy_params={"period": 14},
        start_date="20250101",
        end_date="20251231",
        experiment_type="compare",
        combos=[
            BacktestCombo(
                strategy_key="atr_breakout",
                preset="default",
                strategy_params={"period": 14, "atr_mult": 2},
            ),
        ],
    )
    result = create_batch(collections, user, payload)
    batch = collections["backtest_batches"].docs[0]
    assert batch["asset_type"] == "stock"
    task = collections["backtest_tasks"].docs[0]
    assert task["asset_type"] == "stock"
    assert result["created_tasks"] == 1
