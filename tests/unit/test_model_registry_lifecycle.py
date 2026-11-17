from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from services.model_registry_lifecycle import transition_model


class _Models:
    def __init__(self, doc):
        self.doc = doc

    def find_one(self, query, projection=None):
        for key in ("model_id", "_id"):
            if query.get(key) and self.doc.get(key) == query[key]:
                return self.doc
        return None

    def update_one(self, query, update):
        for key, value in (update.get("$set") or {}).items():
            self.doc[key] = value
        return SimpleNamespace(modified_count=1)


class _ShadowProfiles:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    def find_one(self, query):
        for doc in self.docs:
            if all(doc.get(key) == value for key, value in query.items()):
                return doc
        return None


class _Activations:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    def find_one(self, query):
        for doc in self.docs:
            if all(doc.get(key) == value for key, value in query.items()):
                return doc
        return None


def _doc(**overrides):
    doc = {
        "_id": "m1",
        "model_id": "m1",
        "state": "research",
        "accepted": False,
        "accepted_reasons": [],
        "state_flags": {"live_active": False, "shadow_enabled": False, "shadow_profile_any": False, "validated": False},
        "shadow_profiles": [],
        "state_history": [],
        "validation_runs": [],
        "health": {"active": False},
        "updated_at": datetime(2026, 11, 17),
    }
    doc.update(overrides)
    return doc


def test_research_to_validated_records_validation_and_audit():
    models = _Models(_doc())
    doc = transition_model(
        models,
        "m1",
        "validated",
        actor="admin",
        reason="portfolio OOS weak-positive",
        evidence={
            "result_id": "result-1",
            "protocol": "portfolio_oos",
            "verdict": "weak",
            "metrics": {"periods": 21, "excess_t": 0.44},
        },
    )
    assert doc["state"] == "validated"
    assert doc["accepted"] is False
    assert doc["state_flags"]["validated"] is True
    assert doc["validation_runs"][-1]["result_id"] == "result-1"
    assert doc["latest_validation"]["verdict"] == "weak"
    assert doc["state_history"][-1]["from"] == "research"
    assert doc["state_history"][-1]["to"] == "validated"
    assert doc["state_history"][-1]["actor"] == "admin"


def test_validated_to_shadow_requires_enabled_profile():
    doc = _doc(state="validated", state_flags={"live_active": False, "shadow_enabled": False, "shadow_profile_any": False, "validated": True})
    models = _Models(doc)
    with pytest.raises(HTTPException) as exc_info:
        transition_model(models, "m1", "shadow", actor="admin", reason="no profile")
    assert exc_info.value.status_code == 409

    doc["shadow_profiles"] = [{"profile_id": "p1", "enabled": True}]
    updated = transition_model(models, "m1", "shadow", actor="admin", reason="profile enabled", evidence={"profile_id": "p1"})
    assert updated["state"] == "shadow"
    assert updated["accepted"] is True
    assert updated["accepted_reasons"] == ["shadow"]
    assert updated["state_flags"]["shadow_enabled"] is True


def test_shadow_to_live_requires_live_activation():
    doc = _doc(state="shadow", accepted=True, accepted_reasons=["shadow"], shadow_profiles=[{"profile_id": "p1", "enabled": True}])
    models = _Models(doc)
    activations = _Activations([{"activation_id": "a1", "status": "active", "execution_venue": "live", "params_override": {"model_id": "m1"}}])
    updated = transition_model(
        models,
        "m1",
        "live",
        actor="admin",
        reason="activation a1",
        evidence={"activation_id": "a1"},
        activations=activations,
    )
    assert updated["state"] == "live"
    assert updated["accepted"] is True
    assert updated["accepted_reasons"] == ["live", "shadow"]
    assert updated["state_flags"]["live_active"] is True


def test_invalid_transition_and_expected_state():
    models = _Models(_doc())
    with pytest.raises(HTTPException) as exc_info:
        transition_model(models, "m1", "live", actor="admin", reason="jump")
    assert exc_info.value.status_code == 409

    with pytest.raises(HTTPException) as exc_info:
        transition_model(models, "m1", "validated", actor="admin", reason="stale", evidence={"result_id": "r"}, expected_state="validated")
    assert exc_info.value.status_code == 409


def test_validate_requires_evidence_unless_override():
    models = _Models(_doc())
    with pytest.raises(HTTPException) as exc_info:
        transition_model(models, "m1", "validated", actor="admin", reason="manual")
    assert exc_info.value.status_code == 409

    doc = transition_model(models, "m1", "validated", actor="admin", reason="manual override", admin_override=True)
    assert doc["state"] == "validated"
    assert doc["state_history"][-1]["admin_override"] is True


def test_live_to_shadow_and_retire_clears_accepted():
    doc = _doc(
        state="live",
        accepted=True,
        accepted_reasons=["live", "shadow"],
        state_flags={"live_active": True, "shadow_enabled": True, "shadow_profile_any": True, "validated": True},
        shadow_profiles=[{"profile_id": "p1", "enabled": True}],
    )
    models = _Models(doc)
    demoted = transition_model(models, "m1", "shadow", actor="admin", reason="demote")
    assert demoted["state"] == "shadow"
    assert demoted["accepted"] is True
    assert demoted["accepted_reasons"] == ["shadow"]

    retired = transition_model(models, "m1", "retired", actor="admin", reason="retire")
    assert retired["state"] == "retired"
    assert retired["accepted"] is False
    assert retired["accepted_reasons"] == []
    assert retired["health"]["active"] is False


def test_terminal_state_requires_admin_override_to_revive():
    models = _Models(_doc(state="retired", accepted=False, state_flags={"live_active": False, "shadow_enabled": False, "shadow_profile_any": True, "validated": False}))
    with pytest.raises(HTTPException) as exc_info:
        transition_model(models, "m1", "research", actor="admin", reason="revive")
    assert exc_info.value.status_code == 409

    doc = transition_model(models, "m1", "research", actor="admin", reason="revive", admin_override=True)
    assert doc["state"] == "research"
