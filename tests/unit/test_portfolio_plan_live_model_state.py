import pytest
from fastapi import HTTPException

from routers.portfolio_plans import publishing


class _Models:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    def find_one(self, query, projection=None):
        for doc in self.docs:
            if doc.get("model_id") == query.get("model_id"):
                return doc
        return None


def _patch_models(monkeypatch, docs):
    monkeypatch.setattr(
        publishing,
        "get_ml_db_collections",
        lambda: {"models": _Models(docs)},
    )


def test_empty_model_id_is_noop(monkeypatch):
    _patch_models(monkeypatch, [{"model_id": "m1", "state": "research"}])
    publishing._require_live_model_state("")


def test_live_state_passes(monkeypatch):
    _patch_models(monkeypatch, [{"model_id": "m1", "state": "live"}])
    publishing._require_live_model_state("m1")


def test_non_live_state_is_rejected(monkeypatch):
    _patch_models(monkeypatch, [{"model_id": "m1", "state": "validated"}])
    with pytest.raises(HTTPException) as exc_info:
        publishing._require_live_model_state("m1")
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["model_id"] == "m1"
    assert exc_info.value.detail["state"] == "validated"


def test_missing_registry_document_is_legacy_noop(monkeypatch):
    _patch_models(monkeypatch, [])
    publishing._require_live_model_state("legacy-model")
