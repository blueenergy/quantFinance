"""Unit tests for daily gold panel service."""

from __future__ import annotations

import pytest
from bson import ObjectId

from services.daily_gold_panel import (
    DEFAULT_RECIPE_ID,
    _jsonify,
    aggregate_stats,
    build_daily_gold_panel,
    resolve_recipe_id,
)


class _Cursor:
    def __init__(self, docs):
        self.docs = list(docs)

    def sort(self, key, direction=None):
        if isinstance(key, list) and direction is None:
            for sort_key, sort_direction in reversed(key):
                self.docs = sorted(
                    self.docs,
                    key=lambda doc: doc.get(sort_key) or "",
                    reverse=sort_direction < 0,
                )
            return self
        self.docs = sorted(self.docs, key=lambda doc: doc.get(key) or "", reverse=direction < 0)
        return self

    def limit(self, n):
        self.docs = self.docs[:n]
        return self

    def __iter__(self):
        return iter(self.docs)


class _Collection:
    def __init__(self):
        self.docs = []

    def find_one(self, query=None, projection=None, sort=None):
        docs = list(self.find(query, projection))
        if sort:
            for key, direction in reversed(sort):
                docs = sorted(docs, key=lambda doc: doc.get(key) or "", reverse=direction < 0)
        return docs[0] if docs else None

    def find(self, query=None, projection=None):
        rows = [dict(doc) for doc in self.docs if self._matches(doc, query or {})]
        if projection and projection.get("_id") == 0:
            for row in rows:
                row.pop("_id", None)
        return _Cursor(rows)

    def _matches(self, doc, query):
        for key, expected in query.items():
            if key == "$in":
                continue
            if key == "status" and isinstance(expected, dict) and "$in" in expected:
                if doc.get("status") not in expected["$in"]:
                    return False
                continue
            if key.endswith("_date") and isinstance(expected, dict):
                value = str(doc.get(key) or "")
                if "$lt" in expected and not (value < str(expected["$lt"])):
                    return False
                if "$lte" in expected and not (value <= str(expected["$lte"])):
                    return False
                continue
            if doc.get(key) != expected:
                return False
        return True


class _Db(dict):
    def __getitem__(self, key):
        if key not in self:
            self[key] = _Collection()
        return super().__getitem__(key)


def _setup_data_db():
    return _Db()


@pytest.mark.unit
def test_resolve_recipe_id_defaults():
    assert resolve_recipe_id(None) == DEFAULT_RECIPE_ID
    assert resolve_recipe_id("daily_gold_v1_hs300_g60c40") == "daily_gold_v1_hs300_g60c40"


@pytest.mark.unit
def test_jsonify_converts_mongo_object_id():
    object_id = ObjectId()
    assert _jsonify({"_id": object_id}) == {"_id": str(object_id)}


@pytest.mark.unit
def test_invalid_score_date_is_rejected():
    with pytest.raises(ValueError, match="Invalid score_date"):
        build_daily_gold_panel(data_db=_setup_data_db(), score_date="not-a-date")


@pytest.mark.unit
def test_aggregate_stats_win_rate():
    stats = aggregate_stats(
        [
            {"outcome": "trailing_stop", "return_pct": 0.04},
            {"outcome": "expire", "return_pct": 0.02},
            {"outcome": "trailing_stop", "return_pct": -0.02},
            {"outcome": "insufficient_data", "return_pct": None},
        ]
    )
    assert stats["scored_count"] == 3
    assert stats["win_rate"] == pytest.approx(2 / 3)
    assert stats["payoff_ratio"] == pytest.approx(1.5)
    assert stats["profit_factor"] == pytest.approx(3.0)


@pytest.mark.unit
def test_build_daily_gold_panel_merges_rank_delta():
    data_db = _setup_data_db()
    data_db["daily_gold_snapshots"].docs.extend(
        [
            {
                "recipe_id": DEFAULT_RECIPE_ID,
                "score_date": "20260819",
                "rows": [{"symbol": "000001.SZ", "rank": 2}],
                "top10": [{"symbol": "000001.SZ", "rank": 2, "name": "A", "score": 90}],
            },
            {
                "recipe_id": DEFAULT_RECIPE_ID,
                "score_date": "20260820",
                "rows": [{"symbol": "000001.SZ", "rank": 1}],
                "top10": [{"symbol": "000001.SZ", "rank": 1, "name": "A", "score": 91}],
            },
        ]
    )
    data_db["daily_gold_lots"].docs.append(
        {
            "recipe_id": DEFAULT_RECIPE_ID,
            "lot_id": "lot1",
            "symbol": "000001.SZ",
            "status": "open",
            "entry_score_date": "20260819",
        }
    )
    panel = build_daily_gold_panel(data_db=data_db, recipe_id=DEFAULT_RECIPE_ID, score_date="20260820")
    assert panel["success"] is True
    assert panel["score_date"] == "20260820"
    assert len(panel["recipes"]) == 2
    assert panel["top10"][0]["prev_rank"] == 2
    assert panel["top10"][0]["rank_delta"] == 1
    assert len(panel["open_lots"]) == 1


@pytest.mark.unit
def test_historical_panel_excludes_future_exit_and_current_peak():
    data_db = _setup_data_db()
    data_db["daily_gold_snapshots"].docs.extend(
        [
            {
                "_id": ObjectId(),
                "recipe_id": DEFAULT_RECIPE_ID,
                "score_date": "20260819",
                "rows": [{"symbol": "000001.SZ", "rank": 1}],
                "top10": [{"symbol": "000001.SZ", "rank": 1}],
            },
            {
                "_id": ObjectId(),
                "recipe_id": DEFAULT_RECIPE_ID,
                "score_date": "20260820",
                "rows": [{"symbol": "000002.SZ", "rank": 1}],
                "top10": [{"symbol": "000002.SZ", "rank": 1}],
            },
        ]
    )
    data_db["daily_gold_lots"].docs.append(
        {
            "recipe_id": DEFAULT_RECIPE_ID,
            "lot_id": "lot1",
            "symbol": "000001.SZ",
            "status": "closed",
            "entry_score_date": "20260818",
            "entry_date": "20260819",
            "entry_price": 10,
            "peak_high": 15,
            "exit_trigger_date": "20260820",
            "exit_date": "20260821",
            "exit_price": 12,
            "return_pct": 0.2,
            "outcome": "trailing_stop",
        }
    )

    panel = build_daily_gold_panel(
        data_db=data_db,
        recipe_id=DEFAULT_RECIPE_ID,
        score_date="20260819",
    )
    assert panel["is_historical"] is True
    assert panel["stats"]["scored_count"] == 0
    assert panel["recent_closed"] == []
    assert panel["open_lots"][0]["status"] == "open"
    assert panel["open_lots"][0]["peak_high"] is None
