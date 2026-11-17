import pytest
from fastapi import HTTPException

from routers import portfolio_research


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
        portfolio_research,
        "get_ml_db_collections",
        lambda: {"models": _Models(docs)},
    )


def test_live_registry_state_passes(monkeypatch):
    _patch_models(monkeypatch, [{"model_id": "m1", "state": "live"}])
    portfolio_research._require_live_registry_state({"model_id": "m1"})


def test_validated_registry_state_is_rejected(monkeypatch):
    _patch_models(monkeypatch, [{"model_id": "m1", "state": "validated"}])
    with pytest.raises(HTTPException) as exc_info:
        portfolio_research._require_live_registry_state({"model_id": "m1"})
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["state"] == "validated"


def test_missing_registry_document_is_legacy_noop(monkeypatch):
    _patch_models(monkeypatch, [])
    portfolio_research._require_live_registry_state({"model_id": "legacy-model"})


def test_no_model_id_is_noop(monkeypatch):
    _patch_models(monkeypatch, [{"model_id": "m1", "state": "research"}])
    portfolio_research._require_live_registry_state({})
