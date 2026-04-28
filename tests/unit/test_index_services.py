"""Unit tests for IndexService (Mongo-only constituents, no Tushare)."""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import pytest

from services.index_services import IndexService


def _aggregate_latest_per_symbol(docs: List[Dict[str, Any]], store_key: str) -> List[Dict[str, Any]]:
    """Mimic IndexService pipeline: match index_code, sort by update_date/time desc, first per symbol."""
    matched = [d for d in docs if d.get("index_code") == store_key]
    matched.sort(
        key=lambda d: (d.get("update_date", ""), d.get("update_time", "")),
        reverse=True,
    )
    seen: Dict[str, Dict[str, Any]] = {}
    for d in matched:
        sym = d.get("symbol")
        if sym and sym not in seen:
            seen[sym] = d
    return list(seen.values())


class FakeIndexConstituentsCollection:
    def __init__(self, docs: List[Dict[str, Any]]):
        self.docs = list(docs)

    def aggregate(self, pipeline: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        match = pipeline[0].get("$match", {})
        store_key = match.get("index_code")
        return _aggregate_latest_per_symbol(self.docs, store_key)

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
def test_get_index_constituents_prefers_newer_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    docs = [
        {
            "index_code": "hs300",
            "update_date": "20260101",
            "update_time": "10:00:00",
            "symbol": "000001.SZ",
            "name": "OldName",
            "industry": "银行",
            "market_cap": 1.0,
            "weight": 1.0,
        },
        {
            "index_code": "hs300",
            "update_date": "20260102",
            "update_time": "09:00:00",
            "symbol": "000001.SZ",
            "name": "NewName",
            "industry": "银行",
            "market_cap": 2.0,
            "weight": 2.0,
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
