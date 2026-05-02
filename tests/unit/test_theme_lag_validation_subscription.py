"""Unit tests: theme_lag_validation_subscription API + subscription scan idempotency."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest
from bson import ObjectId
from fastapi.testclient import TestClient

from auth import get_current_active_user
from main import app


class _Cursor(list):
    def sort(self, *args, **kwargs):
        return self


def _doc_matches(doc: Dict[str, Any], query: Dict[str, Any]) -> bool:
    for k, v in query.items():
        if isinstance(v, dict) and "$ne" in v:
            if doc.get(k) == v["$ne"]:
                return False
        else:
            if doc.get(k) != v:
                return False
    return True


class FakeSubscriptionsCollection:
    """In-memory ``theme_lag_validation_subscriptions`` with upsert + delete."""

    def __init__(self) -> None:
        self.docs: List[Dict[str, Any]] = []

    def create_index(self, *args: Any, **kwargs: Any) -> None:
        pass

    def find(self, query: Optional[Dict[str, Any]] = None, projection: Any = None) -> _Cursor:
        q = query or {}
        return _Cursor([d for d in self.docs if _doc_matches(d, q)])

    def find_one(self, query: Dict[str, Any], sort: Any = None) -> Optional[Dict[str, Any]]:
        matches = [d for d in self.docs if _doc_matches(d, query)]
        if not matches:
            return None
        if sort:
            field, direction = sort[0]
            matches.sort(key=lambda x: str(x.get(field, "")), reverse=(direction < 0))
        return matches[0]

    def update_one(self, filt: Dict[str, Any], update: Dict[str, Any], upsert: bool = False) -> Any:
        for d in self.docs:
            if all(d.get(k) == v for k, v in filt.items()):
                if "$set" in update:
                    d.update(update["$set"])
                return type("R", (), {"matched_count": 1, "modified_count": 1, "upserted_id": None})()

        if upsert:
            new_doc: Dict[str, Any] = {}
            new_doc.update(filt)
            if "$setOnInsert" in update:
                new_doc.update(update["$setOnInsert"])
            if "$set" in update:
                new_doc.update(update["$set"])
            if "_id" not in new_doc:
                new_doc["_id"] = ObjectId()
            self.docs.append(new_doc)
            return type("R", (), {"matched_count": 0, "modified_count": 0, "upserted_id": new_doc["_id"]})()

        return type("R", (), {"matched_count": 0, "modified_count": 0, "upserted_id": None})()

    def delete_one(self, filt: Dict[str, Any]) -> Any:
        for i, d in enumerate(self.docs):
            if all(d.get(k) == v for k, v in filt.items()):
                self.docs.pop(i)
                return type("R", (), {"deleted_count": 1})()
        return type("R", (), {"deleted_count": 0})()


class FakeValidationCollection:
    """``theme_lag_candidate_validation`` with ``find_one(..., sort=...)``."""

    def __init__(self, docs: List[Dict[str, Any]]) -> None:
        self.docs = docs

    def find_one(self, query: Dict[str, Any], sort: Optional[List[Any]] = None) -> Optional[Dict[str, Any]]:
        matches = [d for d in self.docs if _doc_matches(d, query)]
        if not matches:
            return None
        if sort:
            field, direction = sort[0]
            matches.sort(key=lambda x: str(x.get(field, "")), reverse=(direction < 0))
        return matches[0]


class FakeNotificationsCollection:
    def __init__(self) -> None:
        self.inserted: List[Dict[str, Any]] = []

    def insert_one(self, doc: Dict[str, Any]) -> None:
        self.inserted.append(dict(doc))


class FakeNotificationSettingsCollection:
    def find_one(self, query: Dict[str, Any]) -> None:
        return None


class UsersStub:
    def find_one(self, query: Dict[str, Any]) -> None:
        return None


@pytest.fixture
def user_id() -> str:
    return str(ObjectId())


@pytest.fixture
def auth_client(user_id: str):
    client = TestClient(app)
    client.app.dependency_overrides[get_current_active_user] = lambda: {
        "id": user_id,
        "username": "tl_test",
    }
    yield client
    client.app.dependency_overrides.pop(get_current_active_user, None)


@pytest.mark.unit
def test_list_theme_lag_subscriptions_empty(auth_client, monkeypatch, user_id):
    from routers import theme_lag_validation_subscription as mod

    subs = FakeSubscriptionsCollection()

    def fake_get_db_collections():
        return {"theme_lag_validation_subscriptions": subs}

    monkeypatch.setattr(mod, "get_db_collections", fake_get_db_collections)
    r = auth_client.get("/api/user/theme-lag-validation-subscriptions")
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["data"]["items"] == []


@pytest.mark.unit
def test_upsert_theme_lag_subscription_roundtrip(auth_client, monkeypatch, user_id):
    from routers import theme_lag_validation_subscription as mod

    subs = FakeSubscriptionsCollection()

    def fake_get_db_collections():
        return {"theme_lag_validation_subscriptions": subs}

    monkeypatch.setattr(mod, "get_db_collections", fake_get_db_collections)

    r1 = auth_client.post(
        "/api/user/theme-lag-validation-subscriptions",
        json={"index_store_key": "csi1000", "enabled": True},
    )
    assert r1.status_code == 200
    assert r1.json()["success"] is True
    assert len(subs.docs) == 1
    assert subs.docs[0]["user_id"] == user_id
    assert subs.docs[0]["index_store_key"] == "csi1000"
    assert subs.docs[0]["enabled"] is True

    r2 = auth_client.post(
        "/api/user/theme-lag-validation-subscriptions",
        json={"index_store_key": "csi1000", "enabled": False},
    )
    assert r2.status_code == 200
    assert len(subs.docs) == 1
    assert subs.docs[0]["enabled"] is False


@pytest.mark.unit
def test_delete_theme_lag_subscription_not_found(auth_client, monkeypatch, user_id):
    from routers import theme_lag_validation_subscription as mod

    subs = FakeSubscriptionsCollection()

    def fake_get_db_collections():
        return {"theme_lag_validation_subscriptions": subs}

    monkeypatch.setattr(mod, "get_db_collections", fake_get_db_collections)
    r = auth_client.delete("/api/user/theme-lag-validation-subscriptions/csi1000")
    assert r.status_code == 404


@pytest.mark.unit
def test_delete_theme_lag_subscription_success(auth_client, monkeypatch, user_id):
    from routers import theme_lag_validation_subscription as mod

    subs = FakeSubscriptionsCollection()
    subs.docs.append(
        {
            "_id": ObjectId(),
            "user_id": user_id,
            "index_store_key": "csi1000",
            "enabled": True,
        }
    )

    def fake_get_db_collections():
        return {"theme_lag_validation_subscriptions": subs}

    monkeypatch.setattr(mod, "get_db_collections", fake_get_db_collections)
    r = auth_client.delete("/api/user/theme-lag-validation-subscriptions/csi1000")
    assert r.status_code == 200
    assert r.json()["success"] is True
    assert subs.docs == []


@pytest.mark.unit
def test_upsert_invalid_index_store_key_rejected(auth_client, monkeypatch, user_id):
    from routers import theme_lag_validation_subscription as mod

    subs = FakeSubscriptionsCollection()

    def fake_get_db_collections():
        return {"theme_lag_validation_subscriptions": subs}

    monkeypatch.setattr(mod, "get_db_collections", fake_get_db_collections)
    r = auth_client.post(
        "/api/user/theme-lag-validation-subscriptions",
        json={"index_store_key": "bad key!", "enabled": True},
    )
    assert r.status_code == 400
    assert subs.docs == []


@pytest.mark.unit
def test_subscription_scan_idempotent_same_eval_ts(monkeypatch):
    """Second scan with unchanged eval watermark must not insert duplicate notifications."""
    import services.theme_lag_validation_subscription_task as tl_task

    uid = str(ObjectId())
    sub_id = ObjectId()
    subs = FakeSubscriptionsCollection()
    subs.docs.append(
        {
            "_id": sub_id,
            "user_id": uid,
            "index_store_key": "csi1000",
            "enabled": True,
            "last_notified_eval_utc_ts": 0.0,
        }
    )
    val = FakeValidationCollection(
        [
            {
                "index_store_key": "csi1000",
                "anchor_trade_date": "20260115",
                "eval_completed_at": 1_700_000_000.0,
                "run_status": "computed",
                "aggregate": {},
            }
        ]
    )
    notifications = FakeNotificationsCollection()

    def fake_get_db_collections():
        return {
            "theme_lag_validation_subscriptions": subs,
            "notifications": notifications,
            "notification_settings": FakeNotificationSettingsCollection(),
            "users": UsersStub(),
        }

    def fake_get_data_db():
        return {"theme_lag_candidate_validation": val}

    monkeypatch.setattr(tl_task, "_send_optional_theme_lag_email", lambda *a, **k: None)
    monkeypatch.setattr("db.get_db_collections", fake_get_db_collections)
    monkeypatch.setattr("db.get_data_db", fake_get_data_db)

    tl_task.run_theme_lag_validation_subscription_scan("job-a")
    assert len(notifications.inserted) == 1
    assert notifications.inserted[0]["user_id"] == uid
    assert notifications.inserted[0]["type"] == "theme_lag_validation"

    row = subs.docs[0]
    assert row.get("last_notified_eval_utc_ts") == 1_700_000_000.0

    tl_task.run_theme_lag_validation_subscription_scan("job-b")
    assert len(notifications.inserted) == 1, "second scan must not duplicate notification"


@pytest.mark.unit
def test_subscription_scan_skips_when_eval_not_newer(monkeypatch):
    import services.theme_lag_validation_subscription_task as tl_task

    uid = str(ObjectId())
    subs = FakeSubscriptionsCollection()
    subs.docs.append(
        {
            "_id": ObjectId(),
            "user_id": uid,
            "index_store_key": "csi1000",
            "enabled": True,
            "last_notified_eval_utc_ts": 1_700_000_000.0,
        }
    )
    val = FakeValidationCollection(
        [
            {
                "index_store_key": "csi1000",
                "anchor_trade_date": "20260115",
                "eval_completed_at": 1_700_000_000.0,
                "run_status": "computed",
            }
        ]
    )
    notifications = FakeNotificationsCollection()

    def fake_get_db_collections():
        return {
            "theme_lag_validation_subscriptions": subs,
            "notifications": notifications,
            "notification_settings": FakeNotificationSettingsCollection(),
            "users": UsersStub(),
        }

    def fake_get_data_db():
        return {"theme_lag_candidate_validation": val}

    monkeypatch.setattr(tl_task, "_send_optional_theme_lag_email", lambda *a, **k: None)
    monkeypatch.setattr("db.get_db_collections", fake_get_db_collections)
    monkeypatch.setattr("db.get_data_db", fake_get_data_db)

    tl_task.run_theme_lag_validation_subscription_scan("job-c")
    assert notifications.inserted == []
