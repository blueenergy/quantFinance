import asyncio
from datetime import datetime

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from routers import ops_repair
from tests.unit.test_score_audit_router import _Collection

pytestmark = pytest.mark.unit


def _collections(audits=None, repairs=None):
    return {
        "score_audit_jobs": _Collection(audits),
        "ops_repair_jobs": _Collection(repairs),
    }


def _stale_audit(**overrides):
    row = {
        "job_id": "audit-stale",
        "status": "completed",
        "overall_verdict": "stored_growth_stale_vs_live",
        "start_date": "20260801",
        "end_date": "20260910",
        "universe_index": "csi1000",
    }
    row.update(overrides)
    return row


def _create(monkeypatch, collections, **overrides):
    monkeypatch.setattr(ops_repair, "get_db_collections", lambda: collections)
    payload = {"source_audit_job_id": "audit-stale", "job_kind": "score_force_refresh"}
    payload.update(overrides)
    request = ops_repair.CreateOpsRepairJobRequest(**payload)
    return asyncio.run(ops_repair.create_ops_repair_job(request, current_user={"id": "user-1"}))


def test_create_copies_window_and_year_shards(monkeypatch):
    collections = _collections(audits=[_stale_audit()])
    response = _create(monkeypatch, collections)
    job = response["data"]
    assert job["status"] == "pending"
    assert job["job_kind"] == "score_force_refresh"
    assert job["start_date"] == "20260801"
    assert job["end_date"] == "20260910"
    assert job["universe_index"] == "csi1000"
    assert job["runtime"] == "docker"
    assert job["source_audit_job_id"] == "audit-stale"
    assert job["shards"] == [
        {"start": "20260801", "end": "20260910", "status": "pending"},
    ]


def test_create_rejects_missing_audit(monkeypatch):
    collections = _collections()
    with pytest.raises(HTTPException) as exc:
        _create(monkeypatch, collections)
    assert exc.value.status_code == 400
    assert "not found" in str(exc.value.detail)


def test_create_rejects_non_stale_audit(monkeypatch):
    collections = _collections(audits=[_stale_audit(overall_verdict="self_consistent")])
    with pytest.raises(HTTPException) as exc:
        _create(monkeypatch, collections)
    assert exc.value.status_code == 400
    assert "not stale" in str(exc.value.detail)


def test_create_rejects_pending_audit(monkeypatch):
    collections = _collections(audits=[_stale_audit(status="pending")])
    with pytest.raises(HTTPException) as exc:
        _create(monkeypatch, collections)
    assert exc.value.status_code == 400


def test_create_rejects_cross_year_without_confirm(monkeypatch):
    collections = _collections(
        audits=[_stale_audit(start_date="20210101", end_date="20230630")]
    )
    with pytest.raises(HTTPException) as exc:
        _create(
            monkeypatch,
            collections,
            source_audit_job_id="audit-stale",
        )
    assert exc.value.status_code == 400
    assert "confirm_years" in str(exc.value.detail)


def test_create_accepts_cross_year_with_confirm(monkeypatch):
    collections = _collections(
        audits=[_stale_audit(start_date="20210101", end_date="20230630")]
    )
    response = _create(monkeypatch, collections, confirm_years=True)
    shards = response["data"]["shards"]
    assert [row["start"] + ":" + row["end"] for row in shards] == [
        "20210101:20211231",
        "20220101:20221231",
        "20230101:20230630",
    ]


def test_create_rejects_window_outside_audit(monkeypatch):
    collections = _collections(audits=[_stale_audit()])
    with pytest.raises(HTTPException) as exc:
        _create(monkeypatch, collections, start_date="20210101", end_date="20210131")
    assert exc.value.status_code == 400


def test_create_rejects_when_queue_is_full(monkeypatch):
    collections = _collections(
        audits=[_stale_audit()],
        repairs=[{"status": "running"}],
    )
    with pytest.raises(HTTPException) as exc:
        _create(monkeypatch, collections)
    assert exc.value.status_code == 409


def test_unknown_kind_rejected_by_model():
    with pytest.raises(ValidationError):
        ops_repair.CreateOpsRepairJobRequest(
            source_audit_job_id="audit-stale",
            job_kind="score_gap_fill",
        )


def test_list_filters_by_source_audit(monkeypatch):
    collections = _collections(
        repairs=[
            {
                "job_id": "r1",
                "source_audit_job_id": "audit-stale",
                "status": "pending",
                "created_at": datetime(2026, 9, 10),
            },
            {
                "job_id": "r2",
                "source_audit_job_id": "other",
                "status": "completed",
                "created_at": datetime(2026, 9, 9),
            },
        ]
    )
    monkeypatch.setattr(ops_repair, "get_db_collections", lambda: collections)
    response = asyncio.run(
        ops_repair.list_ops_repair_jobs(
            source_audit_job_id="audit-stale",
            status=None,
            limit=30,
            current_user={"id": "user-1"},
        )
    )
    assert [row["job_id"] for row in response["data"]] == ["r1"]
