"""Regression tests for the /api/permissions router.

These cover the 2026-11-25 hardening: every route in that router used to be
reachable without any token, which let an anonymous caller promote any account
to ``premium`` via ``PUT /api/permissions/user/{username}`` and rewrite any
strategy's ``min_service_level``.
"""

import pytest
from fastapi.testclient import TestClient

from main import app
from auth import get_current_active_user, get_current_admin_user
from routers import permissions as permissions_router


class FakeCursor(list):
    def sort(self, *args, **kwargs):
        return self

    def skip(self, n):
        return FakeCursor(list(self)[n:])

    def limit(self, n):
        return FakeCursor(list(self)[:n])


class FakeResult:
    def __init__(self, matched=1, modified=1, inserted_id=None):
        self.matched_count = matched
        self.modified_count = modified
        self.inserted_id = inserted_id


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = docs or []

    def _matches(self, doc, query):
        for key, value in (query or {}).items():
            if doc.get(key) != value:
                return False
        return True

    def count_documents(self, query=None):
        return len([d for d in self.docs if self._matches(d, query)])

    def find(self, query=None, projection=None):
        return FakeCursor([d for d in self.docs if self._matches(d, query)])

    def find_one(self, query, projection=None):
        for doc in self.docs:
            if self._matches(doc, query):
                return doc
        return None

    def update_one(self, query, update):
        for doc in self.docs:
            if self._matches(doc, query):
                doc.update(update.get("$set", {}))
                return FakeResult(matched=1, modified=1)
        return FakeResult(matched=0, modified=0)

    def insert_one(self, doc):
        self.docs.append(doc)
        return FakeResult(inserted_id="inserted-1")


@pytest.fixture
def fake_db(monkeypatch):
    collections = {
        "users": FakeCollection([
            {"username": "alice", "service_level": "free",
             "features": {"beta": True}, "permissions": {"can_x": True}},
            {"username": "bob", "service_level": "free"},
            {"username": "root", "is_admin": True, "service_level": "premium"},
        ]),
        "strategy_metadata": FakeCollection([
            {"key": "hidden_dragon", "name": "潜龙低吸", "min_service_level": "vip"},
        ]),
        "upgrade_requests": FakeCollection([]),
    }
    monkeypatch.setattr(permissions_router, "get_db_collections", lambda: collections)
    monkeypatch.setattr(permissions_router, "log_admin_action", lambda *a, **kw: None)
    return collections


@pytest.fixture
def client():
    c = TestClient(app)
    yield c
    c.app.dependency_overrides.clear()


ADMIN = {"id": "u-root", "username": "root", "is_admin": True, "is_active": True}
ALICE = {"id": "u-alice", "username": "alice", "is_admin": False, "is_active": True}


def _as_admin(client):
    client.app.dependency_overrides[get_current_admin_user] = lambda: ADMIN
    client.app.dependency_overrides[get_current_active_user] = lambda: ADMIN


def _as_user(client, user=ALICE):
    client.app.dependency_overrides[get_current_active_user] = lambda: user


PROTECTED_ROUTES = [
    ("get", "/api/permissions/user/alice", None),
    ("put", "/api/permissions/user/alice", {"service_level": "premium"}),
    ("get", "/api/permissions/strategies", None),
    ("put", "/api/permissions/strategy/hidden_dragon", {"min_service_level": "free"}),
    ("get", "/api/permissions/service-levels", None),
    ("post", "/api/permissions/upgrade-requests", {"target_service_level": "vip"}),
    ("get", "/api/permissions/upgrade-requests", None),
    ("put", "/api/permissions/upgrade-requests/000000000000000000000001", {"status": "approved"}),
]


@pytest.mark.unit
@pytest.mark.parametrize("method,path,body", PROTECTED_ROUTES)
def test_every_route_rejects_anonymous(client, fake_db, method, path, body):
    resp = getattr(client, method)(path, json=body) if body is not None else getattr(client, method)(path)
    assert resp.status_code in (401, 403), f"{method.upper()} {path} returned {resp.status_code}"


@pytest.mark.unit
@pytest.mark.parametrize("method,path,body", [
    ("get", "/api/permissions/user/alice", None),
    ("put", "/api/permissions/user/alice", {"service_level": "premium"}),
    ("get", "/api/permissions/strategies", None),
    ("put", "/api/permissions/strategy/hidden_dragon", {"min_service_level": "free"}),
    ("put", "/api/permissions/upgrade-requests/000000000000000000000001", {"status": "approved"}),
])
def test_admin_routes_reject_non_admin(client, fake_db, method, path, body):
    _as_user(client)
    resp = getattr(client, method)(path, json=body) if body is not None else getattr(client, method)(path)
    assert resp.status_code == 403, f"{method.upper()} {path} returned {resp.status_code}"


@pytest.mark.unit
def test_plain_user_can_read_service_levels(client, fake_db):
    _as_user(client)
    resp = client.get("/api/permissions/service-levels")
    assert resp.status_code == 200
    assert "free" in resp.json()["service_levels"]


@pytest.mark.unit
def test_admin_can_update_user_permissions(client, fake_db):
    _as_admin(client)
    resp = client.put("/api/permissions/user/alice", json={"service_level": "vip"})
    assert resp.status_code == 200
    alice = next(u for u in fake_db["users"].docs if u["username"] == "alice")
    assert alice["service_level"] == "vip"


@pytest.mark.unit
def test_partial_update_keeps_existing_features(client, fake_db):
    """A payload without features/permissions must not wipe them."""
    _as_admin(client)
    resp = client.put("/api/permissions/user/alice", json={"service_level": "basic"})
    assert resp.status_code == 200
    alice = next(u for u in fake_db["users"].docs if u["username"] == "alice")
    assert alice["features"] == {"beta": True}
    assert alice["permissions"] == {"can_x": True}


@pytest.mark.unit
def test_update_user_permissions_rejects_unknown_level(client, fake_db):
    _as_admin(client)
    resp = client.put("/api/permissions/user/alice", json={"service_level": "god"})
    assert resp.status_code == 400


@pytest.mark.unit
def test_upgrade_request_ignores_other_username(client, fake_db):
    """A user may only file an upgrade request for themselves."""
    _as_user(client)
    resp = client.post(
        "/api/permissions/upgrade-requests",
        json={"username": "bob", "target_service_level": "vip"},
    )
    assert resp.status_code == 403
    assert fake_db["upgrade_requests"].docs == []


@pytest.mark.unit
def test_upgrade_request_uses_token_username(client, fake_db):
    _as_user(client)
    resp = client.post(
        "/api/permissions/upgrade-requests",
        json={"username": "alice", "target_service_level": "vip", "reason": "need it"},
    )
    assert resp.status_code == 200
    created = fake_db["upgrade_requests"].docs[0]
    assert created["username"] == "alice"


@pytest.mark.unit
def test_upgrade_requests_list_is_scoped_to_own_user(client, fake_db):
    fake_db["upgrade_requests"].docs.extend([
        {"_id": "r1", "username": "alice", "status": "pending"},
        {"_id": "r2", "username": "bob", "status": "pending"},
    ])
    _as_user(client)
    resp = client.get("/api/permissions/upgrade-requests")
    assert resp.status_code == 200
    usernames = {r["username"] for r in resp.json()["requests"]}
    assert usernames == {"alice"}


@pytest.mark.unit
def test_upgrade_requests_list_admin_sees_all(client, fake_db):
    fake_db["upgrade_requests"].docs.extend([
        {"_id": "r1", "username": "alice", "status": "pending"},
        {"_id": "r2", "username": "bob", "status": "pending"},
    ])
    _as_admin(client)
    resp = client.get("/api/permissions/upgrade-requests")
    assert resp.status_code == 200
    usernames = {r["username"] for r in resp.json()["requests"]}
    assert usernames == {"alice", "bob"}
