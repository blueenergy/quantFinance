"""
Unit tests for LLM configuration endpoints
Tests the new LLM configuration management features added today
"""
import pytest
from fastapi.testclient import TestClient
from bson import ObjectId

from main import app
from routers import user_config as user_router
from auth import get_current_active_user


class FakeUsersCollection:
    """Mock users collection for testing"""
    def __init__(self, docs=None):
        self.docs = docs or []
        self.updated = []
        self.inserted = []

    def find_one(self, query):
        """Find one document matching the query"""
        for d in self.docs:
            if "_id" in query:
                query_id = query["_id"]
                doc_id = d.get("_id")
                # Handle ObjectId conversion
                if isinstance(query_id, ObjectId):
                    query_id = str(query_id)
                if isinstance(doc_id, ObjectId):
                    doc_id = str(doc_id)
                if doc_id == query_id:
                    return d
            else:
                match = True
                for k, v in query.items():
                    if d.get(k) != v:
                        match = False
                        break
                if match:
                    return d
        return None

    def update_one(self, flt, update):
        """Update one document"""
        self.updated.append((flt, update))
        modified_count = 0
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
                        # Handle nested updates like "llm_configs.$.field"
                        for key, value in update["$set"].items():
                            if "." in key:
                                # Handle array positional updates (e.g., "llm_configs.0.name")
                                parts = key.split(".")
                                if (
                                    len(parts) == 3
                                    and parts[0] == "llm_configs"
                                    and parts[1].isdigit()
                                ):
                                    idx = int(parts[1])
                                    field = parts[2]
                                    cfgs = d.get("llm_configs") or []
                                    if 0 <= idx < len(cfgs):
                                        cfgs[idx][field] = value
                                        modified_count = 1
                                elif parts[1] == "$" and "llm_configs._id" in flt:
                                    # Update specific item in array
                                    config_id = flt["llm_configs._id"]
                                    if isinstance(config_id, ObjectId):
                                        config_id_str = str(config_id)
                                    else:
                                        config_id_str = config_id
                                    
                                    for cfg in d.get("llm_configs", []):
                                        cfg_id = cfg.get("_id")
                                        if isinstance(cfg_id, ObjectId):
                                            cfg_id = str(cfg_id)
                                        if cfg_id == config_id_str or cfg.get("_id") == config_id:
                                            cfg[parts[2]] = value
                                            modified_count = 1
                                            break
                            else:
                                d[key] = value
                                modified_count = 1
                    if "$push" in update:
                        for key, value in update["$push"].items():
                            if key not in d:
                                d[key] = []
                            d[key].append(value)
                        modified_count = 1
                    if "$pull" in update:
                        for key, condition in update["$pull"].items():
                            if key in d and isinstance(d[key], list):
                                original_len = len(d[key])
                                d[key] = [item for item in d[key] if item.get("_id") != condition.get("_id")]
                                # Only set modified_count if something was actually removed
                                if len(d[key]) < original_len:
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
def test_get_llm_configs_empty(monkeypatch):
    """Test getting LLM configs when user has none"""
    user_id = str(ObjectId())  # Generate valid ObjectId
    fake_users = FakeUsersCollection([
        {"_id": user_id, "username": "testuser", "llm_configs": []}
    ])

    # Mock users_collection directly
    monkeypatch.setattr(user_router, "users_collection", fake_users)

    client = TestClient(app)
    client.app.dependency_overrides[get_current_active_user] = lambda: {"id": user_id, "username": "testuser"}

    resp = client.get("/api/user/llm-configs")
    assert resp.status_code == 200
    body = resp.json()
    # API returns a list directly, not {"success": true, "llm_configs": []}
    assert isinstance(body, list)
    assert len(body) == 0

    client.app.dependency_overrides.pop(get_current_active_user, None)


@pytest.mark.unit
def test_create_llm_config_success(monkeypatch):
    """Test creating a new LLM config"""
    user_id = str(ObjectId())  # Generate valid ObjectId
    fake_users = FakeUsersCollection([
        {"_id": user_id, "username": "testuser", "llm_configs": []}
    ])

    monkeypatch.setattr(user_router, "users_collection", fake_users)

    client = TestClient(app)
    client.app.dependency_overrides[get_current_active_user] = lambda: {"id": user_id, "username": "testuser"}

    config_data = {
        "name": "My DeepSeek Config",
        "provider": "deepseek",
        "api_key": "sk-test-key-123",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat"
    }

    resp = client.post("/api/user/llm-configs", json=config_data)
    assert resp.status_code == 200
    body = resp.json()
    # API returns the config object directly
    assert "id" in body
    assert body["name"] == "My DeepSeek Config"
    assert body["provider"] == "deepseek"
    assert body["model"] == "deepseek-chat"

    client.app.dependency_overrides.pop(get_current_active_user, None)


@pytest.mark.unit
def test_update_llm_config_success(monkeypatch):
    """Test updating an existing LLM config"""
    user_id = str(ObjectId())  # Generate valid ObjectId
    config_id = str(ObjectId())
    config_oid = ObjectId(config_id)  # Create ObjectId object
    fake_users = FakeUsersCollection([
        {
            "_id": user_id,
            "username": "testuser",
            "llm_configs": [
                {
                    "_id": config_oid,  # Use ObjectId object, not string
                    "name": "Old Config",
                    "provider": "openai",
                    "api_key": "old-key",
                    "model": "gpt-3.5-turbo"
                }
            ]
        }
    ])

    monkeypatch.setattr(user_router, "users_collection", fake_users)

    client = TestClient(app)
    client.app.dependency_overrides[get_current_active_user] = lambda: {"id": user_id, "username": "testuser"}

    update_data = {
        "name": "Updated Config",
        "provider": "deepseek",
        "api_key": "new-key",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat"
    }

    resp = client.put(f"/api/user/llm-configs/{config_id}", json=update_data)
    assert resp.status_code == 200
    body = resp.json()
    # API returns updated config object directly
    assert "id" in body
    assert body["name"] == "Updated Config"

    client.app.dependency_overrides.pop(get_current_active_user, None)


@pytest.mark.unit
def test_delete_llm_config_success(monkeypatch):
    """Test deleting an LLM config"""
    user_id = str(ObjectId())  # Generate valid ObjectId
    config_id = str(ObjectId())
    config_oid = ObjectId(config_id)  # Create ObjectId object
    fake_users = FakeUsersCollection([
        {
            "_id": user_id,
            "username": "testuser",
            "llm_configs": [
                {
                    "_id": config_oid,  # Use ObjectId object
                    "name": "Config to Delete",
                    "provider": "openai",
                    "api_key": "test-key"
                }
            ]
        }
    ])

    monkeypatch.setattr(user_router, "users_collection", fake_users)

    client = TestClient(app)
    client.app.dependency_overrides[get_current_active_user] = lambda: {"id": user_id, "username": "testuser"}

    resp = client.delete(f"/api/user/llm-configs/{config_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["message"] == "配置删除成功"  # 修复错误消息

    client.app.dependency_overrides.pop(get_current_active_user, None)


@pytest.mark.unit
def test_activate_llm_config_success(monkeypatch):
    """Test activating an LLM config"""
    user_id = str(ObjectId())  # Generate valid ObjectId
    config_id = str(ObjectId())
    config_oid = ObjectId(config_id)  # Create ObjectId object
    fake_users = FakeUsersCollection([
        {
            "_id": user_id,
            "username": "testuser",
            "llm_configs": [
                {
                    "_id": config_oid,  # Use ObjectId object
                    "name": "Config to Activate",
                    "provider": "deepseek",
                    "api_key": "test-key",
                    "model": "deepseek-chat"
                }
            ],
            "active_llm_config_id": None
        }
    ])

    monkeypatch.setattr(user_router, "users_collection", fake_users)

    client = TestClient(app)
    client.app.dependency_overrides[get_current_active_user] = lambda: {"id": user_id, "username": "testuser"}

    resp = client.post(f"/api/user/llm-configs/{config_id}/activate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["message"] == "配置激活成功"  # 修复错误消息

    # Verify the config was activated
    user = fake_users.find_one({"_id": user_id})
    assert user["active_llm_config_id"] == config_id

    client.app.dependency_overrides.pop(get_current_active_user, None)


@pytest.mark.unit
def test_get_security_settings(monkeypatch):
    """Test getting security settings"""
    user_id = str(ObjectId())  # Generate valid ObjectId
    fake_users = FakeUsersCollection([
        {"_id": user_id, "username": "testuser", "two_factor_enabled": False}
    ])

    monkeypatch.setattr(user_router, "users_collection", fake_users)

    client = TestClient(app)
    client.app.dependency_overrides[get_current_active_user] = lambda: {
        "id": user_id,
        "username": "testuser",
        "two_factor_enabled": False
    }

    resp = client.get("/api/user/security-settings")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["two_factor_enabled"] is False

    client.app.dependency_overrides.pop(get_current_active_user, None)


@pytest.mark.unit
def test_create_llm_config_missing_required_fields(monkeypatch):
    """Test creating LLM config with missing required fields"""
    user_id = str(ObjectId())  # Generate valid ObjectId
    fake_users = FakeUsersCollection([
        {"_id": user_id, "username": "testuser", "llm_configs": []}
    ])

    monkeypatch.setattr(user_router, "users_collection", fake_users)

    client = TestClient(app)
    client.app.dependency_overrides[get_current_active_user] = lambda: {"id": user_id, "username": "testuser"}

    # Missing provider and api_key
    config_data = {
        "name": "Incomplete Config"
    }

    resp = client.post("/api/user/llm-configs", json=config_data)
    assert resp.status_code == 422  # Validation error

    client.app.dependency_overrides.pop(get_current_active_user, None)


@pytest.mark.unit
def test_delete_nonexistent_config(monkeypatch):
    """Test deleting a config that doesn't exist"""
    user_id = str(ObjectId())  # Generate valid ObjectId
    fake_users = FakeUsersCollection([
        {"_id": user_id, "username": "testuser", "llm_configs": []}
    ])

    monkeypatch.setattr(user_router, "users_collection", fake_users)

    client = TestClient(app)
    client.app.dependency_overrides[get_current_active_user] = lambda: {"id": user_id, "username": "testuser"}

    fake_config_id = str(ObjectId())
    resp = client.delete(f"/api/user/llm-configs/{fake_config_id}")
    assert resp.status_code == 404
    body = resp.json()
    # The actual error message is "配置不存在"
    assert "配置不存在" in body["detail"] or "配置" in body["detail"]

    client.app.dependency_overrides.pop(get_current_active_user, None)
