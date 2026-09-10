import asyncio
from datetime import datetime

import pytest
from fastapi import HTTPException

from routers import score_audit


class _Cursor(list):
    def sort(self, field, direction=None):
        if isinstance(field, list):
            field, direction = field[0]
        reverse = direction == -1
        return _Cursor(sorted(self, key=lambda row: row.get(field) or "", reverse=reverse))

    def limit(self, n):
        return _Cursor(list(self)[:n])


class _Collection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    def create_index(self, *args, **kwargs):
        return True

    def find(self, query=None, projection=None):
        return _Cursor([dict(doc) for doc in self.docs if self._matches(doc, query or {})])

    def find_one(self, query=None, projection=None):
        rows = self.find(query, projection)
        return rows[0] if rows else None

    def insert_one(self, doc):
        self.docs.append(dict(doc))

    def count_documents(self, query=None):
        return len(list(self.find(query)))

    def _matches(self, doc, query):
        for key, expected in (query or {}).items():
            if isinstance(expected, dict) and "$in" in expected:
                if doc.get(key) not in expected["$in"]:
                    return False
            elif doc.get(key) != expected:
                return False
        return True


def _collections(jobs=None):
    return {"score_audit_jobs": _Collection(jobs)}


def _create(monkeypatch, collections, **overrides):
    monkeypatch.setattr(score_audit, "get_db_collections", lambda: collections)
    payload = {
        "start_date": "2021-01-04",
        "end_date": "2021-05-15",
        "universe_index": "csi1000",
        "step": 20,
        "sample": 12,
    }
    payload.update(overrides)
    request = score_audit.CreateScoreAuditJobRequest(**payload)
    return asyncio.run(score_audit.create_score_audit_job(request, current_user={"id": "user-1"}))


def test_create_job_normalizes_dates_and_params(monkeypatch):
    collections = _collections()
    response = _create(monkeypatch, collections)
    job = response["data"]
    assert job["status"] == "pending"
    assert job["source"] == "ui"
    assert job["start_date"] == "20210104"
    assert job["params"]["step"] == 20
    assert job["params"]["universe_index"] == "csi1000"
    assert collections["score_audit_jobs"].docs[0]["job_id"] == job["job_id"]


def test_create_raises_step_for_multi_year_window(monkeypatch):
    collections = _collections()
    response = _create(
        monkeypatch,
        collections,
        start_date="20210101",
        end_date="20231229",
        step=2,
    )
    assert response["data"]["params"]["step"] >= 18


def test_create_rejects_inverted_window(monkeypatch):
    collections = _collections()
    with pytest.raises(HTTPException) as exc:
        _create(monkeypatch, collections, start_date="20210515", end_date="20210104")
    assert exc.value.status_code == 400


def test_create_rejects_when_queue_is_full(monkeypatch):
    jobs = [{"status": "pending"} for _ in range(score_audit.MAX_QUEUE_ACTIVE)]
    collections = _collections(jobs)
    with pytest.raises(HTTPException) as exc:
        _create(monkeypatch, collections)
    assert exc.value.status_code == 409


def test_list_is_environment_wide(monkeypatch):
    collections = _collections(
        [
            {
                "job_id": "a",
                "user_id": "other",
                "status": "completed",
                "created_at": datetime(2026, 9, 1),
                "overall_verdict": "self_consistent",
            },
            {
                "job_id": "b",
                "user_id": "user-1",
                "status": "pending",
                "created_at": datetime(2026, 9, 10),
            },
        ]
    )
    monkeypatch.setattr(score_audit, "get_db_collections", lambda: collections)
    response = asyncio.run(score_audit.list_score_audit_jobs(status=None, limit=30, current_user={"id": "user-1"}))
    ids = [row["job_id"] for row in response["data"]]
    assert ids[0] == "b"
    assert "a" in ids
