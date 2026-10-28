"""Unit tests for the Strategy Lab bull/bear batch fields."""

import pytest

from services.backtest_batch_schemas import (
    BacktestBatchCreate,
    BacktestBatchRerunCreate,
    resolve_regime_index,
)
from services.backtest_batch_service import create_batch


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    def insert_one(self, doc):
        self.docs.append(dict(doc))

    def insert_many(self, docs):
        self.docs.extend(dict(doc) for doc in docs)

    def count_documents(self, query):
        return len(self.find(query))

    def find(self, query=None, projection=None):
        query = query or {}
        rows = []
        for doc in self.docs:
            if _matches(doc, query):
                rows.append(dict(doc))
        return _Cursor(rows)

    def find_one(self, query, projection=None, sort=None):
        rows = list(self.find(query))
        if sort:
            field, direction = sort[0]
            rows.sort(key=lambda row: row.get(field, ""), reverse=direction < 0)
        return rows[0] if rows else None


class _Cursor(list):
    def sort(self, field, direction=1):
        return _Cursor(
            sorted(self, key=lambda row: row.get(field, ""), reverse=direction < 0)
        )


def _matches(doc, query):
    for key, value in query.items():
        if isinstance(value, dict):
            actual = doc.get(key)
            if "$gte" in value and not (actual is not None and actual >= value["$gte"]):
                return False
            if "$lte" in value and not (actual is not None and actual <= value["$lte"]):
                return False
            continue
        if doc.get(key) != value:
            return False
    return True


@pytest.mark.unit
@pytest.mark.parametrize(
    ("universe", "expected"),
    [
        ("csi1000", "000852.SH"),
        ("csi500", "000905.SH"),
        ("hs300", "000300.SH"),
        ("csi2000", "932000.CSI"),
        ("star50", "000688.SH"),
    ],
)
def test_resolve_regime_index_follows_index_universe(universe, expected):
    assert (
        resolve_regime_index(
            regime_cash=True,
            regime_index=None,
            universe_type="index",
            universe_value=universe,
        )
        == expected
    )


@pytest.mark.unit
def test_resolve_regime_index_explicit_wins_and_manual_falls_back():
    assert (
        resolve_regime_index(
            regime_cash=True,
            regime_index="000905.SH",
            universe_type="index",
            universe_value="csi1000",
        )
        == "000905.SH"
    )
    assert (
        resolve_regime_index(
            regime_cash=True,
            regime_index=None,
            universe_type="manual",
            universe_value=None,
        )
        == "000300.SH"
    )


@pytest.mark.unit
def test_resolve_regime_index_rejects_a500_auto():
    with pytest.raises(ValueError, match="中证A500"):
        resolve_regime_index(
            regime_cash=True,
            regime_index=None,
            universe_type="index",
            universe_value="a500",
        )


@pytest.mark.unit
def test_regime_fields_are_cleared_when_switch_is_off():
    payload = BacktestBatchCreate(
        symbols=["000001.SZ"],
        strategy_key="turtle",
        start_date="20240101",
        end_date="20241231",
        regime_cash=False,
        regime_index="000300.SH",
        regime_rule="k_slow_anchor",
    )
    assert payload.regime_index is None
    assert payload.regime_rule is None


@pytest.mark.unit
def test_unknown_regime_index_or_rule_is_rejected():
    with pytest.raises(ValueError, match="unsupported regime_index"):
        BacktestBatchCreate(
            symbols=["000001.SZ"],
            strategy_key="turtle",
            start_date="20240101",
            end_date="20241231",
            regime_cash=True,
            regime_index="999999.SH",
        )
    with pytest.raises(ValueError, match="unsupported regime_rule"):
        BacktestBatchCreate(
            symbols=["000001.SZ"],
            strategy_key="turtle",
            start_date="20240101",
            end_date="20241231",
            regime_cash=True,
            regime_rule="not_a_rule",
        )


@pytest.mark.unit
def test_single_task_regime_fields_reuse_the_same_validators():
    from routers.backtest import BacktestTaskCreate

    task = BacktestTaskCreate(
        symbol="000001.SZ",
        strategy_key="turtle",
        start_date="20240101",
        end_date="20241231",
        regime_cash=True,
        regime_index="000300.SH",
    )
    assert task.regime_index == "000300.SH"
    with pytest.raises(ValueError, match="unsupported regime_index"):
        BacktestTaskCreate(
            symbol="000001.SZ",
            strategy_key="turtle",
            start_date="20240101",
            end_date="20241231",
            regime_index="bad",
        )


@pytest.mark.unit
def test_rerun_regime_fields_reuse_the_same_validators():
    payload = BacktestBatchRerunCreate(regime_index="000300.SH", regime_rule="k_fast_only")
    assert payload.regime_index == "000300.SH"
    assert payload.regime_rule == "k_fast_only"
    with pytest.raises(ValueError, match="unsupported regime_index"):
        BacktestBatchRerunCreate(regime_index="bad")


@pytest.mark.unit
def test_create_batch_persists_regime_on_batch_and_tasks(monkeypatch):
    monkeypatch.setattr(
        "services.backtest_batch_service.validate_regime_window",
        lambda *args, **kwargs: None,
    )
    collections = {
        "backtest_batches": FakeCollection(),
        "backtest_tasks": FakeCollection(),
    }
    payload = BacktestBatchCreate(
        symbols=["000001.SZ", "000002.SZ"],
        strategy_key="turtle",
        start_date="20240101",
        end_date="20241231",
        regime_cash=True,
        regime_index="000300.SH",
    )
    create_batch(collections, {"id": "user1"}, payload)

    batch = collections["backtest_batches"].docs[0]
    assert batch["regime_cash"] is True
    assert batch["regime_index"] == "000300.SH"
    assert batch["regime_rule"] is None
    for task in collections["backtest_tasks"].docs:
        assert task["regime_cash"] is True
        assert task["regime_index"] == "000300.SH"
