"""Unit tests for backtest router API endpoints

Tests cover:
- User APIs: create, list, get, delete backtest tasks
- User APIs: get backtest results
- Worker APIs: poll, claim, report results, report failure
"""

import pytest
from datetime import datetime
from fastapi.testclient import TestClient
from bson import ObjectId

from main import app
from routers import backtest
from auth import get_current_user


class FakeBacktestCollection:
    """Mock collection for backtest tasks and results"""
    
    def __init__(self, docs=None):
        self.docs = docs or []
        self._next_id = 1
    
    def insert_one(self, doc):
        """Mock insert_one"""
        class InsertResult:
            def __init__(self, inserted_id):
                self.inserted_id = inserted_id
        
        doc_id = ObjectId()
        doc['_id'] = doc_id
        self.docs.append(doc.copy())
        return InsertResult(doc_id)
    
    def _matches(self, doc, query):
        if not query:
            return True
        for key, value in query.items():
            if key == "$or":
                if not any(self._matches(doc, subquery) for subquery in value):
                    return False
                continue
            doc_value = doc.get(key)
            if isinstance(value, dict):
                if "$exists" in value:
                    exists = key in doc
                    if exists != value["$exists"]:
                        return False
                if "$in" in value and doc_value not in value["$in"]:
                    return False
                if "$nin" in value and doc_value in value["$nin"]:
                    return False
                continue
            if doc_value != value:
                return False
        return True

    def find(self, query=None, projection=None):
        """Mock find with query support"""
        if query is None or query == {}:
            filtered = self.docs[:]
        else:
            filtered = [doc for doc in self.docs if self._matches(doc, query)]
        
        class Cursor(list):
            def __init__(self, items):
                super().__init__(items)
            
            def sort(self, field, direction=None):
                if isinstance(field, list):
                    field, direction = field[0]
                return Cursor(sorted(self, key=lambda x: x.get(field, ''), reverse=(direction == -1)))
            
            def limit(self, n):
                return Cursor(list(self)[:n])
        
        return Cursor(filtered)
    
    def find_one(self, query):
        """Mock find_one"""
        for doc in self.docs:
            if self._matches(doc, query):
                return doc.copy()
        return None
    
    def update_one(self, query, update, upsert=False):
        """Mock update_one"""
        class UpdateResult:
            def __init__(self, matched_count, modified_count):
                self.matched_count = matched_count
                self.modified_count = modified_count
        
        for doc in self.docs:
            if self._matches(doc, query):
                if '$set' in update:
                    doc.update(update['$set'])
                return UpdateResult(1, 1)
        
        if upsert:
            new_doc = query.copy()
            if '$set' in update:
                new_doc.update(update['$set'])
            self.docs.append(new_doc)
            return UpdateResult(0, 1)
        
        return UpdateResult(0, 0)

    def update_many(self, query, update):
        """Mock update_many"""
        class UpdateResult:
            def __init__(self, matched_count, modified_count):
                self.matched_count = matched_count
                self.modified_count = modified_count

        matched = 0
        modified = 0
        for doc in self.docs:
            if self._matches(doc, query):
                matched += 1
                if '$set' in update:
                    doc.update(update['$set'])
                    modified += 1
        return UpdateResult(matched, modified)

    def insert_many(self, docs):
        """Mock insert_many"""
        class InsertManyResult:
            def __init__(self, inserted_ids):
                self.inserted_ids = inserted_ids

        ids = []
        for doc in docs:
            doc_id = ObjectId()
            doc['_id'] = doc_id
            self.docs.append(doc.copy())
            ids.append(doc_id)
        return InsertManyResult(ids)
    
    def delete_one(self, query):
        """Mock delete_one"""
        class DeleteResult:
            def __init__(self, deleted_count):
                self.deleted_count = deleted_count
        
        for i, doc in enumerate(self.docs):
            if self._matches(doc, query):
                del self.docs[i]
                return DeleteResult(1)
        
        return DeleteResult(0)

    def delete_many(self, query):
        """Mock delete_many"""
        class DeleteResult:
            def __init__(self, deleted_count):
                self.deleted_count = deleted_count

        deleted = 0
        remaining = []
        for doc in self.docs:
            if self._matches(doc, query):
                deleted += 1
            else:
                remaining.append(doc)
        self.docs = remaining
        return DeleteResult(deleted)


@pytest.fixture
def mock_user():
    """Mock authenticated user"""
    return {
        "id": "user123",
        "username": "testuser",
        "email": "test@example.com"
    }


def worker_headers(monkeypatch):
    monkeypatch.setenv("BACKTEST_WORKER_TOKEN", "test-worker-secret")
    return {"X-Backtest-Worker-Token": "test-worker-secret"}


# ============================================================================
# User API Tests - Task Management
# ============================================================================

@pytest.mark.unit
def test_create_backtest_task(monkeypatch, mock_user):
    """Test creating a new backtest task"""
    # Create fresh collections for this test
    collections = {
        "backtest_tasks": FakeBacktestCollection([]),
        "backtest_results": FakeBacktestCollection([]),
        "users": FakeBacktestCollection([]),
        "stock_info": FakeBacktestCollection([
            {"symbol": "000858.SZ", "name": "五粮液"}
        ])
    }
    
    # Patch at the backtest module level (where it's imported)
    monkeypatch.setattr(backtest, 'get_db_collections', lambda: collections)
    
    client = TestClient(app)
    client.app.dependency_overrides[get_current_user] = lambda: mock_user
    
    payload = {
        "symbol": "000858",
        "strategy_key": "turtle",
        "preset": "turtle_conservative",
        "strategy_params": {"period": 20},
        "start_date": "20240101",
        "end_date": "20241231",
        "initial_cash": 1000000.0
    }
    
    response = client.post("/api/backtest/tasks", json=payload)
    
    assert response.status_code == 200
    data = response.json()
    assert data["symbol"] == "000858.SZ"
    assert data["strategy_key"] == "turtle"
    assert data["preset"] == "turtle_conservative"
    assert data.get("stock_name") == "五粮液"
    assert data["status"] == "pending"
    assert data["user_id"] == "user123"
    assert "task_id" in data
    
    # Verify task was stored
    assert len(collections["backtest_tasks"].docs) == 1
    assert collections["backtest_tasks"].docs[0].get("preset") == "turtle_conservative"
    
    client.app.dependency_overrides.clear()


@pytest.mark.unit
def test_create_etf_backtest_task(monkeypatch, mock_user):
    """Test creating an ETF backtest task."""
    collections = {
        "backtest_tasks": FakeBacktestCollection([]),
        "backtest_results": FakeBacktestCollection([]),
        "users": FakeBacktestCollection([]),
        "stock_info": FakeBacktestCollection([]),
        "etf_basic": FakeBacktestCollection([
            {"ts_code": "510300.SH", "name": "沪深300ETF"}
        ])
    }

    monkeypatch.setattr(backtest, 'get_db_collections', lambda: collections)

    client = TestClient(app)
    client.app.dependency_overrides[get_current_user] = lambda: mock_user

    payload = {
        "symbol": "510300.SH",
        "asset_type": "etf",
        "strategy_key": "turtle",
        "strategy_params": {"period": 20},
        "start_date": "20240101",
        "end_date": "20241231",
        "initial_cash": 1000000.0
    }

    response = client.post("/api/backtest/tasks", json=payload)

    assert response.status_code == 200
    data = response.json()
    assert data["symbol"] == "510300.SH"
    assert data["asset_type"] == "etf"
    assert data.get("stock_name") == "沪深300ETF"
    assert collections["backtest_tasks"].docs[0].get("asset_type") == "etf"

    client.app.dependency_overrides.clear()


@pytest.mark.unit
def test_list_backtest_tasks(monkeypatch, mock_user):
    """Test listing user's backtest tasks"""
    # Create fresh collections for this test
    collections = {
        "backtest_tasks": FakeBacktestCollection([]),
        "backtest_results": FakeBacktestCollection([]),
        "users": FakeBacktestCollection([]),
        "stock_info": FakeBacktestCollection([
            {"symbol": "000858", "name": "五粮液"},
            {"symbol": "600036", "name": "招商银行"}
        ])
    }
    
    # Add some existing tasks
    collections["backtest_tasks"].docs = [
        {
            "task_id": "bt_001",
            "user_id": "user123",
            "symbol": "000858",
            "strategy_key": "turtle",
            "preset": "turtle_conservative",
            "strategy_params": {},
            "start_date": "20240101",
            "end_date": "20241231",
            "initial_cash": 1000000.0,
            "status": "pending",
            "worker_id": None,
            "created_at": datetime.now(),
            "started_at": None,
            "completed_at": None,
            "error_message": None
        },
        {
            "task_id": "bt_002",
            "user_id": "user123",
            "symbol": "600036",
            "strategy_key": "grid",
            "preset": None,
            "strategy_params": {},
            "start_date": "20240101",
            "end_date": "20241231",
            "initial_cash": 1000000.0,
            "status": "completed",
            "worker_id": "worker1",
            "created_at": datetime.now(),
            "started_at": datetime.now(),
            "completed_at": datetime.now(),
            "error_message": None
        }
    ]
    
    # Patch at the backtest module level (where it's imported)
    monkeypatch.setattr(backtest, 'get_db_collections', lambda: collections)
    
    client = TestClient(app)
    client.app.dependency_overrides[get_current_user] = lambda: mock_user
    
    # Test list all tasks
    response = client.get("/api/backtest/tasks")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2
    assert data[0].get("preset") in ("turtle_conservative", None)
    # stock_name should be enriched from stock_info when available
    assert any(t.get("stock_name") == "五粮液" for t in data)
    
    # Test filter by status
    response = client.get("/api/backtest/tasks?status=pending")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["status"] == "pending"
    
    client.app.dependency_overrides.clear()


@pytest.mark.unit
def test_list_backtest_tasks_excludes_batch_children_by_default(monkeypatch, mock_user):
    """Batch child tasks should stay out of the single-stock backtest page."""
    collections = {
        "backtest_tasks": FakeBacktestCollection([
            {
                "task_id": "bt_single",
                "user_id": "user123",
                "symbol": "000858",
                "asset_type": "stock",
                "strategy_key": "turtle",
                "preset": None,
                "strategy_params": {},
                "start_date": "20240101",
                "end_date": "20241231",
                "initial_cash": 1000000.0,
                "status": "completed",
                "worker_id": None,
                "created_at": datetime.now(),
            },
            {
                "task_id": "bt_batch_child",
                "batch_id": "bb_001",
                "user_id": "user123",
                "symbol": "600036",
                "asset_type": "stock",
                "strategy_key": "turtle",
                "preset": None,
                "strategy_params": {},
                "start_date": "20240101",
                "end_date": "20241231",
                "initial_cash": 1000000.0,
                "status": "completed",
                "worker_id": None,
                "created_at": datetime.now(),
            },
        ]),
        "backtest_results": FakeBacktestCollection([]),
        "users": FakeBacktestCollection([]),
        "stock_info": FakeBacktestCollection([]),
    }
    monkeypatch.setattr(backtest, 'get_db_collections', lambda: collections)

    client = TestClient(app)
    client.app.dependency_overrides[get_current_user] = lambda: mock_user

    response = client.get("/api/backtest/tasks")
    assert response.status_code == 200
    assert [row["task_id"] for row in response.json()] == ["bt_single"]

    response = client.get("/api/backtest/tasks?include_batch=true")
    assert response.status_code == 200
    assert {row["task_id"] for row in response.json()} == {"bt_single", "bt_batch_child"}

    client.app.dependency_overrides.clear()


@pytest.mark.unit
def test_list_backtest_tasks_tolerates_empty_historical_dates(monkeypatch, mock_user):
    """Old malformed tasks with empty dates should not break task history."""
    collections = {
        "backtest_tasks": FakeBacktestCollection([
            {
                "task_id": "bt_dirty",
                "user_id": "user123",
                "symbol": "000858",
                "asset_type": "stock",
                "strategy_key": "turtle",
                "preset": None,
                "strategy_params": {},
                "start_date": "",
                "end_date": "",
                "initial_cash": 1000000.0,
                "status": "failed",
                "worker_id": None,
                "created_at": datetime.now(),
                "error_message": "legacy bad task",
            },
        ]),
        "backtest_results": FakeBacktestCollection([]),
        "users": FakeBacktestCollection([]),
        "stock_info": FakeBacktestCollection([]),
    }
    monkeypatch.setattr(backtest, 'get_db_collections', lambda: collections)

    client = TestClient(app)
    client.app.dependency_overrides[get_current_user] = lambda: mock_user

    response = client.get("/api/backtest/tasks")
    assert response.status_code == 200
    data = response.json()
    assert data[0]["task_id"] == "bt_dirty"
    assert data[0]["start_date"] is None
    assert data[0]["end_date"] is None

    client.app.dependency_overrides.clear()


@pytest.mark.unit
def test_get_backtest_task(monkeypatch, mock_user):
    """Test getting task details"""
    # Create fresh collections for this test
    collections = {
        "backtest_tasks": FakeBacktestCollection([]),
        "backtest_results": FakeBacktestCollection([]),
        "users": FakeBacktestCollection([]),
        "stock_info": FakeBacktestCollection([
            {"symbol": "000858", "name": "五粮液"}
        ])
    }
    
    collections["backtest_tasks"].docs = [
        {
            "task_id": "bt_001",
            "user_id": "user123",
            "symbol": "000858",
            "strategy_key": "turtle",
            "preset": "turtle_conservative",
            "strategy_params": {"period": 20},
            "start_date": "20240101",
            "end_date": "20241231",
            "initial_cash": 1000000.0,
            "status": "pending",
            "worker_id": None,
            "created_at": datetime.now(),
            "started_at": None,
            "completed_at": None,
            "error_message": None
        }
    ]
    
    # Patch at the backtest module level (where it's imported)
    monkeypatch.setattr(backtest, 'get_db_collections', lambda: collections)
    
    client = TestClient(app)
    client.app.dependency_overrides[get_current_user] = lambda: mock_user
    
    response = client.get("/api/backtest/tasks/bt_001")
    assert response.status_code == 200
    data = response.json()
    assert data["task_id"] == "bt_001"
    assert data["symbol"] == "000858"
    assert data.get("preset") == "turtle_conservative"
    assert data.get("stock_name") == "五粮液"
    
    # Test non-existent task
    response = client.get("/api/backtest/tasks/bt_999")
    assert response.status_code == 404
    
    client.app.dependency_overrides.clear()


@pytest.mark.unit
def test_delete_backtest_batch(monkeypatch, mock_user):
    """Test deleting a batch removes batch doc, tasks, and results."""
    collections = {
        "backtest_batches": FakeBacktestCollection(
            [
                {
                    "batch_id": "bb_del",
                    "user_id": "user123",
                    "name": "to delete",
                    "status": "completed",
                }
            ]
        ),
        "backtest_tasks": FakeBacktestCollection(
            [
                {
                    "task_id": "bt_a",
                    "batch_id": "bb_del",
                    "user_id": "user123",
                    "status": "completed",
                },
                {
                    "task_id": "bt_b",
                    "batch_id": "bb_del",
                    "user_id": "user123",
                    "status": "pending",
                },
            ]
        ),
        "backtest_results": FakeBacktestCollection(
            [
                {"task_id": "bt_a", "metrics": {}},
                {"task_id": "bt_b", "metrics": {}},
            ]
        ),
        "strategy_lab_loops": FakeBacktestCollection([]),
        "users": FakeBacktestCollection([]),
    }
    monkeypatch.setattr(backtest, "get_db_collections", lambda: collections)
    monkeypatch.setattr(backtest, "get_analyzer_db", lambda: {"backtest_batch_reviews": FakeBacktestCollection([])})

    client = TestClient(app)
    client.app.dependency_overrides[get_current_user] = lambda: mock_user

    response = client.delete("/api/backtest/batches/bb_del")
    assert response.status_code == 200
    body = response.json()
    assert body["deleted_tasks"] == 2
    assert collections["backtest_batches"].find_one({"batch_id": "bb_del"}) is None
    assert len(collections["backtest_tasks"].docs) == 0
    assert len(collections["backtest_results"].docs) == 0

    client.app.dependency_overrides.clear()


@pytest.mark.unit
def test_delete_backtest_batch_allows_terminal_loop_link(monkeypatch, mock_user):
    """Deleting a batch linked only to a terminal Strategy Lab Loop is allowed."""
    collections = {
        "backtest_batches": FakeBacktestCollection(
            [{"batch_id": "bb_linked", "user_id": "user123", "name": "linked"}]
        ),
        "backtest_tasks": FakeBacktestCollection([]),
        "backtest_results": FakeBacktestCollection([]),
        "strategy_lab_loops": FakeBacktestCollection(
            [
                {
                    "loop_id": "sl_test",
                    "user_id": "user123",
                    "root_batch_id": "bb_linked",
                    "current_batch_id": "bb_linked",
                    "status": "completed",
                }
            ]
        ),
        "users": FakeBacktestCollection([]),
    }
    monkeypatch.setattr(backtest, "get_db_collections", lambda: collections)
    monkeypatch.setattr(backtest, "get_analyzer_db", lambda: {"backtest_batch_reviews": FakeBacktestCollection([])})

    client = TestClient(app)
    client.app.dependency_overrides[get_current_user] = lambda: mock_user

    response = client.delete("/api/backtest/batches/bb_linked")
    assert response.status_code == 200
    assert collections["backtest_batches"].find_one({"batch_id": "bb_linked"}) is None

    client.app.dependency_overrides.clear()


@pytest.mark.unit
def test_delete_backtest_batch_rejects_active_loop_link(monkeypatch, mock_user):
    """Deleting a batch linked to an active Strategy Lab Loop should be blocked."""
    collections = {
        "backtest_batches": FakeBacktestCollection(
            [{"batch_id": "bb_linked", "user_id": "user123", "name": "linked"}]
        ),
        "backtest_tasks": FakeBacktestCollection([]),
        "backtest_results": FakeBacktestCollection([]),
        "strategy_lab_loops": FakeBacktestCollection(
            [
                {
                    "loop_id": "sl_test",
                    "user_id": "user123",
                    "root_batch_id": "bb_linked",
                    "current_batch_id": "bb_linked",
                    "status": "running",
                }
            ]
        ),
        "users": FakeBacktestCollection([]),
    }
    monkeypatch.setattr(backtest, "get_db_collections", lambda: collections)

    client = TestClient(app)
    client.app.dependency_overrides[get_current_user] = lambda: mock_user

    response = client.delete("/api/backtest/batches/bb_linked")
    assert response.status_code == 400
    assert "自动迭代 Loop" in response.json()["detail"]
    assert collections["backtest_batches"].find_one({"batch_id": "bb_linked"}) is not None

    client.app.dependency_overrides.clear()


@pytest.mark.unit
def test_delete_backtest_task(monkeypatch, mock_user):
    """Test deleting/cancelling backtest task"""
    # Create fresh collections for this test
    collections = {
        "backtest_tasks": FakeBacktestCollection([]),
        "backtest_results": FakeBacktestCollection([]),
        "users": FakeBacktestCollection([])
    }
    
    collections["backtest_tasks"].docs = [
        {
            "task_id": "bt_pending",
            "user_id": "user123",
            "symbol": "000858",
            "strategy_key": "turtle",
            "strategy_params": {},
            "start_date": "20240101",
            "end_date": "20241231",
            "initial_cash": 1000000.0,
            "status": "pending",
            "worker_id": None,
            "created_at": datetime.now()
        },
        {
            "task_id": "bt_running",
            "user_id": "user123",
            "symbol": "600036",
            "strategy_key": "grid",
            "strategy_params": {},
            "start_date": "20240101",
            "end_date": "20241231",
            "initial_cash": 1000000.0,
            "status": "running",
            "worker_id": "worker1",
            "created_at": datetime.now()
        }
    ]
    
    # Patch at the backtest module level (where it's imported)
    monkeypatch.setattr(backtest, 'get_db_collections', lambda: collections)
    
    client = TestClient(app)
    client.app.dependency_overrides[get_current_user] = lambda: mock_user
    
    # Test delete pending task (should be deleted)
    response = client.delete("/api/backtest/tasks/bt_pending")
    assert response.status_code == 200
    assert "deleted" in response.json()["message"]
    assert len(collections["backtest_tasks"].docs) == 1
    
    # Test cancel running task (should be cancelled, not deleted)
    response = client.delete("/api/backtest/tasks/bt_running")
    assert response.status_code == 200
    assert "cancelled" in response.json()["message"]
    assert len(collections["backtest_tasks"].docs) == 1
    
    # Verify status changed to cancelled
    task = collections["backtest_tasks"].find_one({"task_id": "bt_running"})
    assert task["status"] == "cancelled"
    
    client.app.dependency_overrides.clear()


# ============================================================================
# User API Tests - Results
# ============================================================================

@pytest.mark.unit
def test_get_backtest_result(monkeypatch, mock_user):
    """Test getting backtest results"""
    # Create fresh collections for this test
    collections = {
        "backtest_tasks": FakeBacktestCollection([]),
        "backtest_results": FakeBacktestCollection([]),
        "users": FakeBacktestCollection([])
    }
    
    collections["backtest_tasks"].docs = [
        {
            "task_id": "bt_completed",
            "user_id": "user123",
            "symbol": "000858",
            "strategy_key": "turtle",
            "strategy_params": {},
            "start_date": "20240101",
            "end_date": "20241231",
            "initial_cash": 1000000.0,
            "status": "completed",
            "worker_id": "worker1",
            "created_at": datetime.now(),
            "completed_at": datetime.now()
        }
    ]
    
    collections["backtest_results"].docs = [
        {
            "task_id": "bt_completed",
            "user_id": "user123",
            "symbol": "000858",
            "strategy_key": "turtle",
            "metrics": {
                "total_return": 0.15,
                "sharpe_ratio": 1.2,
                "max_drawdown": -0.08,
                "win_rate": 0.6,
                "total_trades": 10
            },
            "trades": [
                {
                    "datetime": "2024-03-15 10:30:00",
                    "action": "buy",
                    "price": 15.5,
                    "quantity": 100,
                    "commission": 5.0,
                    "pnl": 0,
                    "cumulative_pnl": 0
                }
            ],
            "equity_curve": [
                {"date": "2024-01-01", "value": 1000000},
                {"date": "2024-12-31", "value": 1150000}
            ],
            "created_at": datetime.now()
        }
    ]
    
    # Patch at the backtest module level (where it's imported)
    monkeypatch.setattr(backtest, 'get_db_collections', lambda: collections)
    
    client = TestClient(app)
    client.app.dependency_overrides[get_current_user] = lambda: mock_user
    
    response = client.get("/api/backtest/results/bt_completed")
    assert response.status_code == 200
    data = response.json()
    assert data["task_id"] == "bt_completed"
    assert data["metrics"]["total_return"] == 0.15
    assert len(data["trades"]) == 1
    assert len(data["equity_curve"]) == 2
    
    client.app.dependency_overrides.clear()


@pytest.mark.unit
def test_get_backtest_result_not_completed(monkeypatch, mock_user):
    """Test getting results for non-completed task returns error"""
    # Create fresh collections for this test
    collections = {
        "backtest_tasks": FakeBacktestCollection([]),
        "backtest_results": FakeBacktestCollection([]),
        "users": FakeBacktestCollection([])
    }
    
    collections["backtest_tasks"].docs = [
        {
            "task_id": "bt_pending",
            "user_id": "user123",
            "symbol": "000858",
            "strategy_key": "turtle",
            "strategy_params": {},
            "start_date": "20240101",
            "end_date": "20241231",
            "initial_cash": 1000000.0,
            "status": "pending",
            "created_at": datetime.now()
        }
    ]
    
    # Patch at the backtest module level (where it's imported)
    monkeypatch.setattr(backtest, 'get_db_collections', lambda: collections)
    
    client = TestClient(app)
    client.app.dependency_overrides[get_current_user] = lambda: mock_user
    
    response = client.get("/api/backtest/results/bt_pending")
    assert response.status_code == 400
    assert "not completed" in response.json()["detail"].lower()
    
    client.app.dependency_overrides.clear()


# ============================================================================
# Worker API Tests
# ============================================================================

@pytest.mark.unit
def test_poll_pending_tasks(monkeypatch, mock_user):
    """Test worker polling for pending tasks"""
    # Create fresh collections for this test
    collections = {
        "backtest_tasks": FakeBacktestCollection([]),
        "backtest_results": FakeBacktestCollection([]),
        "users": FakeBacktestCollection([])
    }
    
    collections["backtest_tasks"].docs = [
        {
            "task_id": "bt_001",
            "user_id": "user123",
            "symbol": "000858",
            "strategy_key": "turtle",
            "strategy_params": {"period": 20},
            "start_date": "20240101",
            "end_date": "20241231",
            "initial_cash": 1000000.0,
            "status": "pending",
            "created_at": datetime.now()
        },
        {
            "task_id": "bt_002",
            "user_id": "user456",
            "symbol": "600036",
            "strategy_key": "grid",
            "strategy_params": {},
            "start_date": "20240101",
            "end_date": "20241231",
            "initial_cash": 1000000.0,
            "status": "pending",
            "created_at": datetime.now()
        }
    ]
    
    # Patch at the backtest module level (where it's imported)
    monkeypatch.setattr(backtest, 'get_db_collections', lambda: collections)
    
    client = TestClient(app)

    response = client.get("/api/backtest/tasks/pending/poll", headers=worker_headers(monkeypatch))
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2
    assert all(task["task_id"] for task in data)
    

@pytest.mark.unit
def test_poll_pending_tasks_skips_missing_dates(monkeypatch, mock_user):
    """Worker poll should not return malformed pending tasks (cleanup is offline)."""
    collections = {
        "backtest_tasks": FakeBacktestCollection([
            {
                "task_id": "bt_dirty",
                "user_id": "user123",
                "symbol": "000858",
                "strategy_key": "turtle",
                "strategy_params": {},
                "start_date": "",
                "end_date": "",
                "initial_cash": 1000000.0,
                "status": "pending",
                "created_at": datetime.now(),
            },
            {
                "task_id": "bt_valid",
                "user_id": "user123",
                "symbol": "600036",
                "strategy_key": "turtle",
                "strategy_params": {},
                "start_date": "20240101",
                "end_date": "20241231",
                "initial_cash": 1000000.0,
                "status": "pending",
                "created_at": datetime.now(),
            },
        ]),
        "backtest_results": FakeBacktestCollection([]),
        "users": FakeBacktestCollection([]),
    }
    monkeypatch.setattr(backtest, 'get_db_collections', lambda: collections)

    client = TestClient(app)
    response = client.get("/api/backtest/tasks/pending/poll", headers=worker_headers(monkeypatch))

    assert response.status_code == 200
    assert [task["task_id"] for task in response.json()] == ["bt_valid"]
    dirty = collections["backtest_tasks"].find_one({"task_id": "bt_dirty"})
    assert dirty["status"] == "pending"


@pytest.mark.unit
def test_claim_backtest_task(monkeypatch, mock_user):
    """Test worker claiming a task"""
    # Create fresh collections for this test
    collections = {
        "backtest_tasks": FakeBacktestCollection([]),
        "backtest_results": FakeBacktestCollection([]),
        "users": FakeBacktestCollection([])
    }
    
    collections["backtest_tasks"].docs = [
        {
            "task_id": "bt_001",
            "user_id": "user123",
            "symbol": "000858",
            "strategy_key": "turtle",
            "strategy_params": {},
            "start_date": "20240101",
            "end_date": "20241231",
            "initial_cash": 1000000.0,
            "status": "pending",
            "worker_id": None,
            "created_at": datetime.now()
        }
    ]
    
    # Patch at the backtest module level (where it's imported)
    monkeypatch.setattr(backtest, 'get_db_collections', lambda: collections)
    
    client = TestClient(app)
    headers = worker_headers(monkeypatch)

    response = client.post("/api/backtest/tasks/bt_001/claim?worker_id=worker1", headers=headers)
    assert response.status_code == 200
    assert "claimed successfully" in response.json()["message"]
    
    # Verify task status changed
    task = collections["backtest_tasks"].find_one({"task_id": "bt_001"})
    assert task["status"] == "claimed"
    assert task["worker_id"] == "worker1"
    
    # Try to claim again (should fail)
    response = client.post("/api/backtest/tasks/bt_001/claim?worker_id=worker2", headers=headers)
    assert response.status_code == 409
    

@pytest.mark.unit
def test_report_backtest_result(monkeypatch, mock_user):
    """Test worker reporting backtest results"""
    # Create fresh collections for this test
    collections = {
        "backtest_tasks": FakeBacktestCollection([]),
        "backtest_results": FakeBacktestCollection([]),
        "users": FakeBacktestCollection([])
    }
    
    collections["backtest_tasks"].docs = [
        {
            "task_id": "bt_001",
            "user_id": "user123",
            "symbol": "000858",
            "strategy_key": "turtle",
            "strategy_params": {},
            "start_date": "20240101",
            "end_date": "20241231",
            "initial_cash": 1000000.0,
            "status": "claimed",
            "worker_id": "worker1",
            "created_at": datetime.now()
        }
    ]
    
    # Patch at the backtest module level (where it's imported)
    monkeypatch.setattr(backtest, 'get_db_collections', lambda: collections)
    
    client = TestClient(app)
    
    payload = {
        "metrics": {
            "total_return": 0.15,
            "sharpe_ratio": 1.2,
            "max_drawdown": -0.08,
            "win_rate": 0.6,
            "total_trades": 10
        },
        "trades": [
            {
                "datetime": "2024-03-15 10:30:00",
                "action": "buy",
                "price": 15.5,
                "quantity": 100,
                "commission": 5.0,
                "pnl": 0,
                "cumulative_pnl": 0
            }
        ],
        "equity_curve": [
            {"date": "2024-01-01", "value": 1000000},
            {"date": "2024-12-31", "value": 1150000}
        ]
    }
    
    response = client.post(
        "/api/backtest/tasks/bt_001/report",
        json=payload,
        headers=worker_headers(monkeypatch),
    )
    assert response.status_code == 200
    assert "successfully" in response.json()["message"]
    
    # Verify task status changed to completed
    task = collections["backtest_tasks"].find_one({"task_id": "bt_001"})
    assert task["status"] == "completed"
    
    # Verify results were stored
    assert len(collections["backtest_results"].docs) == 1
    result = collections["backtest_results"].docs[0]
    assert result["task_id"] == "bt_001"
    assert result["metrics"]["total_return"] == 0.15
    

@pytest.mark.unit
def test_report_backtest_result_cleans_large_ints(monkeypatch):
    """Persisting worker results should not fail on values beyond BSON int64."""
    collections = {
        "backtest_tasks": FakeBacktestCollection([
            {
                "task_id": "bt_001",
                "user_id": "user123",
                "symbol": "000858",
                "strategy_key": "turtle",
                "strategy_params": {},
                "start_date": "20240101",
                "end_date": "20241231",
                "initial_cash": 1000000.0,
                "status": "claimed",
                "worker_id": "worker1",
                "created_at": datetime.now(),
            }
        ]),
        "backtest_results": FakeBacktestCollection([]),
        "users": FakeBacktestCollection([]),
    }
    monkeypatch.setattr(backtest, 'get_db_collections', lambda: collections)

    huge_int = 2 ** 80
    client = TestClient(app)
    response = client.post(
        "/api/backtest/tasks/bt_001/report",
        json={
            "metrics": {
                "total_return": 0.15,
                "sharpe_ratio": 1.2,
                "max_drawdown": -0.08,
                "win_rate": 0.6,
                "total_trades": huge_int,
            },
            "trades": [
                {
                    "datetime": "2024-03-15 10:30:00",
                    "action": "buy",
                    "price": 15.5,
                    "quantity": huge_int,
                    "commission": 5.0,
                    "pnl": 0,
                    "cumulative_pnl": 0,
                }
            ],
            "equity_curve": [{"date": "2024-12-31", "value": huge_int}],
        },
        headers=worker_headers(monkeypatch),
    )

    assert response.status_code == 200
    stored = collections["backtest_results"].docs[0]
    assert stored["metrics"]["total_trades"] == str(huge_int)
    assert stored["trades"][0]["quantity"] == str(huge_int)


@pytest.mark.unit
def test_report_backtest_failure(monkeypatch, mock_user):
    """Test worker reporting backtest failure"""
    # Create fresh collections for this test
    collections = {
        "backtest_tasks": FakeBacktestCollection([]),
        "backtest_results": FakeBacktestCollection([]),
        "users": FakeBacktestCollection([])
    }
    
    collections["backtest_tasks"].docs = [
        {
            "task_id": "bt_001",
            "user_id": "user123",
            "symbol": "000858",
            "strategy_key": "turtle",
            "strategy_params": {},
            "start_date": "20240101",
            "end_date": "20241231",
            "initial_cash": 1000000.0,
            "status": "claimed",
            "worker_id": "worker1",
            "created_at": datetime.now()
        }
    ]
    
    # Patch at the backtest module level (where it's imported)
    monkeypatch.setattr(backtest, 'get_db_collections', lambda: collections)
    
    client = TestClient(app)
    
    response = client.post(
        "/api/backtest/tasks/bt_001/fail?error_message=Strategy%20execution%20failed",
        headers=worker_headers(monkeypatch),
    )
    assert response.status_code == 200
    
    # Verify task status changed to failed
    task = collections["backtest_tasks"].find_one({"task_id": "bt_001"})
    assert task["status"] == "failed"
    assert "failed" in task.get("error_message", "").lower()
    
@pytest.mark.unit
def test_worker_api_rejects_missing_token(monkeypatch):
    monkeypatch.setenv("BACKTEST_WORKER_TOKEN", "test-worker-secret")
    client = TestClient(app)

    response = client.get("/api/backtest/tasks/pending/poll")

    assert response.status_code == 401
