"""Unit tests for market events API."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth import get_current_user
from routers import market_events as me
from routers.market_events import router


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def sort(self, *_args, **_kwargs):
        return self

    def limit(self, _n):
        return self

    def __iter__(self):
        return iter(self._rows)


class _FakeCollection:
    def __init__(self, docs):
        self.docs = docs
        self.inserted = []

    def find(self, query, projection=None):
        rows = []
        for doc in self.docs:
            et = doc.get("event_time")
            gte = (query or {}).get("event_time", {}).get("$gte")
            lte = (query or {}).get("event_time", {}).get("$lte")
            if gte and et < gte:
                continue
            if lte and et > lte:
                continue
            if doc.get("importance", 0) < (query or {}).get("importance", {}).get("$gte", 0):
                continue
            if "region" in (query or {}) and doc.get("region") != query["region"]:
                continue
            if "category" in (query or {}) and doc.get("category") != query["category"]:
                continue
            if "symbol" in (query or {}) and doc.get("symbol") != query["symbol"]:
                continue
            if "idempotency_key" in (query or {}) and doc.get("idempotency_key") != query["idempotency_key"]:
                continue
            if "status" in (query or {}) and "$in" in query["status"]:
                if doc.get("status") not in query["status"]["$in"]:
                    continue
            rows.append(doc)
        return _FakeCursor(rows)

    def count_documents(self, query):
        return sum(1 for _ in self.find(query))

    def find_one(self, query=None, sort=None):
        rows = list(self.find(query or {}))
        if sort:
            for field, direction in reversed(sort):
                rows.sort(key=lambda r: r.get(field) or "", reverse=direction < 0)
        return rows[0] if rows else None

    def insert_one(self, doc):
        doc = dict(doc)
        doc["_id"] = "task-1"
        self.inserted.append(doc)
        self.docs.append(doc)
        return type("R", (), {"inserted_id": "task-1"})()


def test_list_market_events_filters(monkeypatch):
    now = datetime(2026, 7, 23, 1, 0, tzinfo=timezone.utc)
    docs = [
        {
            "_id": "1",
            "identity_key": "a",
            "title": "US CPI",
            "event_time": now,
            "region": "US",
            "category": "macro_data",
            "importance": 4,
            "sources": [],
        },
        {
            "_id": "2",
            "identity_key": "b",
            "title": "Small",
            "event_time": now,
            "region": "CN",
            "category": "macro_data",
            "importance": 2,
            "sources": [],
        },
    ]
    fake = _FakeCollection(docs)
    monkeypatch.setattr(me, "get_data_db", lambda: {"market_events": fake})

    resp = me.query_market_events(from_date="20260723", to_date="20260723", min_importance=3)
    assert resp["count"] == 1
    assert resp["events"][0]["title"] == "US CPI"


def test_serialize_event_marks_naive_mongo_datetime_as_utc():
    """PyMongo returns naive UTC; without +00:00 browsers in CST mis-parse as local."""
    doc = {
        "_id": "amd",
        "title": "AMD Advancing AI大会",
        # Correct absolute time: 2026-07-23 16:30 UTC == 2026-07-24 00:30 Beijing
        "event_time": datetime(2026, 7, 23, 16, 30, 0),
        "sources": [{"name": "wallstreetcn", "seen_at": datetime(2026, 7, 23, 10, 0, 0)}],
        "fetched_at": datetime(2026, 7, 23, 10, 0, 0),
        "updated_at": datetime(2026, 7, 23, 10, 0, 0),
    }
    out = me._serialize_event(doc)
    assert out["event_time"] == "2026-07-23T16:30:00+00:00"
    assert out["sources"][0]["seen_at"].endswith("+00:00")
    assert out["fetched_at"].endswith("+00:00")


def test_enqueue_briefs_requires_auth(monkeypatch):
    app = FastAPI()
    app.include_router(router, prefix="/api")
    tasks = _FakeCollection([])

    class _DB(dict):
        def __getitem__(self, key):
            if key == "analysis_tasks":
                return tasks
            raise KeyError(key)

    monkeypatch.setattr(me, "db", _DB())
    client = TestClient(app)

    # No auth override → 401/403 depending on security scheme behavior
    unauth = client.post("/api/market/events/enqueue-briefs", json={"min_importance": 4, "limit": 5})
    assert unauth.status_code in {401, 403}

    app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "username": "alice"}
    ok = client.post("/api/market/events/enqueue-briefs", json={"min_importance": 4, "limit": 5})
    assert ok.status_code == 200
    body = ok.json()
    assert body["status"] == "pending"
    assert body["reused"] is False
    assert tasks.inserted
    assert tasks.inserted[0]["type"] == "market_events_brief"
    assert tasks.inserted[0].get("user_id") == "u1" or tasks.inserted[0].get("username") == "alice"
