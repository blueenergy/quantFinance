import pytest
from fastapi.testclient import TestClient
from bson import ObjectId

from main import app
from routers import user_config as user_router  # 修复导入：user -> user_config
from auth import get_current_active_user, verify_password, get_password_hash


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
        modified_count = 0
        # Apply update to in-memory docs for convenience
        for d in self.docs:
            if "_id" in flt:
                flt_id = flt["_id"]
                doc_id = d.get("_id")
                # Handle ObjectId conversion
                if isinstance(flt_id, ObjectId):
                    flt_id = str(flt_id)
                if isinstance(doc_id, ObjectId):
                    doc_id = str(doc_id)
                if doc_id == flt_id:
                    if "$set" in update:
                        d.update(update["$set"])
                        modified_count = 1
                    break
            else:
                match = True
                for k, v in flt.items():
                    if d.get(k) != v:
                        match = False
                        break
                if match:
                    if "$set" in update:
                        d.update(update["$set"])
                        modified_count = 1
                    break
        
        # Return a mock result object
        class UpdateResult:
            def __init__(self, modified_count):
                self.modified_count = modified_count
        
        return UpdateResult(modified_count)


@pytest.mark.unit
def test_change_password_success(monkeypatch):
    user_id = str(ObjectId())  # Generate valid ObjectId
    fake_users = FakeUsersCollection([
        {"_id": user_id, "username": "alice", "hashed_password": "old_hash"}
    ])

    # Mock users_collection and auth functions
    monkeypatch.setattr(user_router, "users_collection", fake_users)
    
    # Mock the imports inside the change_password function
    import auth
    monkeypatch.setattr(auth, "verify_password", lambda plain, hashed: plain == "oldpwd" and hashed == "old_hash")
    monkeypatch.setattr(auth, "get_password_hash", lambda pwd: f"hashed:{pwd}")

    client = TestClient(app)
    # Override auth dependency to provide current user context with hashed_password
    client.app.dependency_overrides[get_current_active_user] = lambda: {
        "id": user_id, 
        "username": "alice",
        "hashed_password": "old_hash"
    }

    resp = client.post(
        "/api/user/change-password",
        json={"old_password": "oldpwd", "new_password": "newpassword"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert fake_users.updated, "Expected update_one to be called"
    flt, update = fake_users.updated[0]
    # Compare as ObjectId
    assert flt["_id"] == ObjectId(user_id) or str(flt["_id"]) == user_id
    assert update["$set"]["hashed_password"] == "hashed:newpassword"

    client.app.dependency_overrides.pop(get_current_active_user, None)


@pytest.mark.unit
def test_change_password_incorrect_old(monkeypatch):
    user_id = str(ObjectId())  # Generate valid ObjectId
    fake_users = FakeUsersCollection([
        {"_id": user_id, "username": "alice", "hashed_password": "old_hash"}
    ])

    monkeypatch.setattr(user_router, "users_collection", fake_users)
    
    import auth
    monkeypatch.setattr(auth, "verify_password", lambda plain, hashed: False)

    client = TestClient(app)
    client.app.dependency_overrides[get_current_active_user] = lambda: {
        "id": user_id, 
        "username": "alice",
        "hashed_password": "old_hash"
    }

    resp = client.post(
        "/api/user/change-password",
        json={"old_password": "wrongpwd", "new_password": "newpassword"},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["detail"] == "当前密码不正确"  # 修复错误消息

    client.app.dependency_overrides.pop(get_current_active_user, None)
