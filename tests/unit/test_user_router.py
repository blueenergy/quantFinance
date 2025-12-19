import pytest
from fastapi.testclient import TestClient

from main import app
from routers import user as user_router
from auth import get_current_active_user


class FakeUsersCollection:
    def __init__(self, docs=None):
        self.docs = docs or []
        self.updated = []

    def find_one(self, query):
        for d in self.docs:
            match = True
            for k, v in query.items():
                if d.get(k) != v:
                    match = False
                    break
            if match:
                return d
        return None

    def update_one(self, flt, update):
        self.updated.append((flt, update))
        # Apply update to in-memory docs for convenience
        for d in self.docs:
            match = True
            for k, v in flt.items():
                if d.get(k) != v:
                    match = False
                    break
            if match:
                if "$set" in update:
                    d.update(update["$set"])


@pytest.mark.unit
def test_change_password_success(monkeypatch):
    fake_users = FakeUsersCollection([
        {"_id": "u1", "username": "alice", "hashed_password": "old_hash"}
    ])

    # Use fake collections inside router
    monkeypatch.setattr(user_router, "get_db_collections", lambda: {"users": fake_users})
    # Stub password verification and hashing
    monkeypatch.setattr(user_router, "verify_password", lambda plain, hashed: plain == "oldpwd" and hashed == "old_hash")
    monkeypatch.setattr(user_router, "get_password_hash", lambda pwd: f"hashed:{pwd}")

    client = TestClient(app)
    # Override auth dependency to provide current user context
    client.app.dependency_overrides[get_current_active_user] = lambda: {"id": "u1", "username": "alice"}

    resp = client.post(
        "/api/user/change-password",
        json={"old_password": "oldpwd", "new_password": "newpassword"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert fake_users.updated, "Expected update_one to be called"
    flt, update = fake_users.updated[0]
    assert flt == {"_id": "u1"}
    assert update["$set"]["hashed_password"] == "hashed:newpassword"

    client.app.dependency_overrides.pop(get_current_active_user, None)


@pytest.mark.unit
def test_change_password_incorrect_old(monkeypatch):
    fake_users = FakeUsersCollection([
        {"_id": "u1", "username": "alice", "hashed_password": "old_hash"}
    ])

    monkeypatch.setattr(user_router, "get_db_collections", lambda: {"users": fake_users})
    monkeypatch.setattr(user_router, "verify_password", lambda plain, hashed: False)

    client = TestClient(app)
    client.app.dependency_overrides[get_current_active_user] = lambda: {"id": "u1", "username": "alice"}

    resp = client.post(
        "/api/user/change-password",
        json={"old_password": "wrongpwd", "new_password": "newpassword"},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["detail"] == "Incorrect current password"

    client.app.dependency_overrides.pop(get_current_active_user, None)
