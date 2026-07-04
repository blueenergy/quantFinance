"""Unit tests for IndexService (Mongo-only constituents, no Tushare)."""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import pytest

from services.index_services import IndexService


def _matches(doc: Dict[str, Any], query: Dict[str, Any]) -> bool:
    for key, expected in query.items():
        value = doc.get(key)
        if isinstance(expected, dict):
            if "$lte" in expected and not (value is not None and str(value) <= str(expected["$lte"])):
                return False
            continue
        if value != expected:
            return False
    return True


class FakeIndexConstituentsCollection:
    def __init__(self, docs: List[Dict[str, Any]]):
        self.docs = list(docs)

    def find_one(self, query=None, projection=None, sort=None):
        query = query or {}
        matched = [dict(d) for d in self.docs if _matches(d, query)]
        if not matched:
            return None
        if sort:
            for field, direction in reversed(sort):
                matched.sort(key=lambda row: row.get(field, ""), reverse=direction < 0)
        return matched[0]

    def find(self, query=None, projection=None):
        query = query or {}
        return [dict(d) for d in self.docs if _matches(d, query)]

    def distinct(self, field, query=None):
        query = query or {}
        return sorted({d.get(field) for d in self.docs if _matches(d, query) and d.get(field)})

    def create_index(self, *args: Any, **kwargs: Any) -> None:
        pass


@pytest.mark.unit
@pytest.mark.parametrize(
    "code,expected",
    [
        ("hs300", "hs300"),
        ("000300.SH", "hs300"),
        ("399300.SZ", "hs300"),
        ("csi500", "csi500"),
        ("000905.SH", "csi500"),
        ("star50", "star50"),
        ("000688.SH", "star50"),
        ("sse50", "sse50"),
        ("000016.SH", "sse50"),
        ("csi1000", "csi1000"),
        ("000852.SH", "csi1000"),
        ("csi2000", "csi2000"),
        ("932000.CSI", "csi2000"),
        ("932000.SH", "csi2000"),
    ],
)
def test_store_key_for_index(code: str, expected: str) -> None:
    assert IndexService._store_key_for_index(code) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("000001.SZ", "000001.SZ"),
        ("688001.sh", "688001.SH"),
        ("600519.SH", "600519.SH"),
        ("600519", "600519.SH"),
        ("000001", "000001.SZ"),
        ("688001", "688001.SH"),
    ],
)
def test_ensure_ts_code(raw: str, expected: str) -> None:
    assert IndexService._ensure_ts_code(raw) == expected


@pytest.mark.unit
def test_ensure_ts_code_unknown_returns_stripped() -> None:
    assert IndexService._ensure_ts_code("  foo  ") == "foo"


@pytest.mark.unit
def test_get_index_constituents_uses_latest_snapshot_only(monkeypatch: pytest.MonkeyPatch) -> None:
    docs = [
        {
            "index_code": "hs300",
            "weight_trade_date": "20260102",
            "update_date": "20260102",
            "update_time": "09:00:00",
            "symbol": "000001.SZ",
            "name": "NewName",
            "industry": "银行",
            "market_cap": 2.0,
            "weight": 2.0,
        },
        {
            "index_code": "hs300",
            "weight_trade_date": "20260101",
            "update_date": "20260101",
            "update_time": "10:00:00",
            "symbol": "000002.SZ",
            "name": "Removed",
            "industry": "银行",
            "market_cap": 1.0,
            "weight": 1.0,
        },
    ]

    def fake_get_data_db() -> Dict[str, Any]:
        return {"index_constituents": FakeIndexConstituentsCollection(docs)}

    monkeypatch.setattr("services.index_services.get_data_db", fake_get_data_db)

    svc = IndexService()
    out = asyncio.run(svc.get_index_constituents("hs300"))
    assert len(out) == 1
    assert out[0].symbol == "000001.SZ"
    assert out[0].name == "NewName"
    assert out[0].weight == 2.0


@pytest.mark.unit
def test_get_index_constituents_empty_db_uses_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get_data_db() -> Dict[str, Any]:
        return {"index_constituents": FakeIndexConstituentsCollection([])}

    monkeypatch.setattr("services.index_services.get_data_db", fake_get_data_db)

    svc = IndexService()
    out = asyncio.run(svc.get_index_constituents("hs300"))
    assert len(out) >= 1
    assert any(c.symbol == "000001.SZ" for c in out)
