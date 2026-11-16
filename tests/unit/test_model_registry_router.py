import re
from datetime import datetime

import pytest
from fastapi import HTTPException

from routers import model_registry


class _Collection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])
        self.queries = []

    def find(self, query, projection=None):
        self.queries.append(query)
        rows = [doc for doc in self.docs if _matches(doc, query)]

        class _Cursor:
            def __init__(self, items):
                self.items = items

            def sort(self, key, direction=None):
                reverse = direction is not None and direction < 0
                self.items = sorted(
                    self.items,
                    key=lambda doc: doc.get(key) or "",
                    reverse=reverse,
                )
                return self

            def __iter__(self):
                return iter(self.items)

        return _Cursor(rows)

    def find_one(self, query, projection=None):
        for doc in self.docs:
            if _matches(doc, query):
                return doc
        return None


def _matches(doc, query):
    for key, value in (query or {}).items():
        if key == "$or":
            if not any(_matches(doc, branch) for branch in value):
                return False
            continue
        if isinstance(value, dict):
            if "$in" in value and doc.get(key) not in value["$in"]:
                return False
            if "$regex" in value:
                flags = re.IGNORECASE if "i" in str(value.get("$options") or "") else 0
                if not re.search(value["$regex"], str(doc.get(key) or ""), flags):
                    return False
            continue
        if doc.get(key) != value:
            return False
    return True


def _doc(model_id, state, *, accepted=False, reasons=None, family="f1", index_code="csi1000"):
    return {
        "_id": model_id,
        "model_id": model_id,
        "display_name": model_id,
        "family_id": family,
        "index_code": index_code,
        "state": state,
        "accepted": accepted,
        "accepted_reasons": list(reasons or []),
        "state_flags": {"live_active": state == "live", "shadow_enabled": "shadow" in (reasons or [])},
        "latest_validation": {"protocol": "portfolio_oos", "verdict": "weak"},
        "operational_gate": {"passed": False, "reason": "insufficient_universes"},
        "health": {
            "last_prediction_date": "20261116",
            "date_count": 21,
            "prediction_count": 20000,
            "stale_days": 0,
            "is_current": state in ("live", "shadow"),
            "active": state in ("live", "shadow"),
        },
        "artifacts": [{"result_id": "r1"}],
        "training_jobs": [{"job_id": "j1"}],
        "shadow_profiles": [{"profile_id": "p1"}] if "shadow" in (reasons or []) else [],
        "activations": [{"activation_id": "a1"}] if state == "live" else [],
        "created_at": datetime(2025, 1, 1),
        "updated_at": datetime(2026, 11, 16),
        "backfilled_at": datetime(2026, 11, 16),
    }


@pytest.fixture
def registry(monkeypatch):
    docs = [
        _doc("research-csi1000-clean-raw5", "live", accepted=True, reasons=["live", "shadow"], family="csi1000-clean-raw5"),
        _doc("promo-csi1000-e243b409", "shadow", accepted=True, reasons=["shadow"], family="csi1000-clean-raw5"),
        _doc("research-csi1000-clean-raw5-oos2025", "validated", family="csi1000-clean-raw5"),
        _doc("research-rolling500-hs300", "research", family="hs300-rolling500", index_code="hs300"),
        _doc("promo-hs300-525f02db", "retired", family="hs300-rolling500", index_code="hs300"),
    ]
    collection = _Collection(docs)
    monkeypatch.setattr(
        model_registry,
        "get_ml_db_collections",
        lambda: {"models": collection},
    )
    return collection


def test_summary_counts_states_accepted_and_active_stale(registry):
    response = model_registry.model_registry_summary(current_user={"id": "u1"})
    data = response["data"]

    assert data["total"] == 5
    assert data["accepted"] == 2
    assert data["states"]["live"] == 1
    assert data["states"]["shadow"] == 1
    assert data["states"]["validated"] == 1
    assert data["states"]["research"] == 1
    assert data["states"]["retired"] == 1
    assert data["states"]["rejected"] == 0
    assert data["active_stale"] == 0


def test_list_filters_state_accepted_and_query(registry):
    response = model_registry.list_registry_models(
        state="live,shadow",
        accepted=True,
        q="clean",
        index_code=None,
        family_id=None,
        limit=100,
        offset=0,
        current_user={"id": "u1"},
    )
    rows = response["data"]
    assert response["total"] == 2
    assert [row["state"] for row in rows] == ["live", "shadow"]
    assert all(row["accepted"] for row in rows)

    query = registry.queries[-1]
    assert query["state"] == {"$in": ["live", "shadow"]}
    assert query["accepted"] is True
    assert "$or" in query


def test_list_state_rank_puts_live_before_research(registry):
    response = model_registry.list_registry_models(
        state=None,
        index_code=None,
        family_id=None,
        accepted=None,
        q=None,
        limit=100,
        offset=0,
        current_user={"id": "u1"},
    )
    assert [row["state"] for row in response["data"]] == [
        "live",
        "shadow",
        "validated",
        "research",
        "retired",
    ]


def test_detail_serializes_without_id_and_404(registry):
    response = model_registry.get_registry_model("research-csi1000-clean-raw5", current_user={"id": "u1"})
    assert response["data"]["model_id"] == "research-csi1000-clean-raw5"
    assert "_id" not in response["data"]
    assert response["data"]["state"] == "live"

    with pytest.raises(HTTPException) as exc_info:
        model_registry.get_registry_model("missing", current_user={"id": "u1"})
    assert exc_info.value.status_code == 404
