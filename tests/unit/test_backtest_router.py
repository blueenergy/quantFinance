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
    
    def find(self, query=None):
        """Mock find with query support"""
        if query is None or query == {}:
            filtered = self.docs[:]
        else:
            filtered = []
            for doc in self.docs:
                match = True
                for key, value in query.items():
                    if doc.get(key) != value:
                        match = False
                        break
                if match:
                    filtered.append(doc)
        
        class Cursor(list):
            def __init__(self, items):
                super().__init__(items)
            
            def sort(self, field, direction):
                # Sort by field (we'll keep it simple for tests)
                return Cursor(sorted(self, key=lambda x: x.get(field, ''), reverse=(direction == -1)))
            
            def limit(self, n):
                return Cursor(list(self)[:n])
        
        return Cursor(filtered)
    
    def find_one(self, query):
        """Mock find_one"""
        for doc in self.docs:
            match = True
            for key, value in query.items():
                if doc.get(key) != value:
                    match = False
                    break
            if match:
                return doc.copy()
        return None
    
    def update_one(self, query, update, upsert=False):
        """Mock update_one"""
        class UpdateResult:
            def __init__(self, matched_count, modified_count):
                self.matched_count = matched_count
                self.modified_count = modified_count
        
        for doc in self.docs:
            match = True
            for key, value in query.items():
                if doc.get(key) != value:
                    match = False
                    break
            if match:
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
    
    def delete_one(self, query):
        """Mock delete_one"""
        class DeleteResult:
            def __init__(self, deleted_count):
                self.deleted_count = deleted_count
        
        for i, doc in enumerate(self.docs):
            match = True
            for key, value in query.items():
                if doc.get(key) != value:
                    match = False
                    break
            if match:
                del self.docs[i]
                return DeleteResult(1)
        
        return DeleteResult(0)


@pytest.fixture
def mock_user():
    """Mock authenticated user"""
    return {
        "id": "user123",
        "username": "testuser",
        "email": "test@example.com"
    }


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
        "users": FakeBacktestCollection([])
    }
    
    # Patch at the backtest module level (where it's imported)
    monkeypatch.setattr(backtest, 'get_db_collections', lambda: collections)
    
    client = TestClient(app)
    client.app.dependency_overrides[get_current_user] = lambda: mock_user
    
    payload = {
        "symbol": "000858",
        "strategy_key": "turtle",
        "strategy_params": {"period": 20},
        "start_date": "20240101",
        "end_date": "20241231",
        "initial_cash": 1000000.0
    }
    
    response = client.post("/api/backtest/tasks", json=payload)
    
    assert response.status_code == 200
    data = response.json()
    assert data["symbol"] == "000858"
    assert data["strategy_key"] == "turtle"
    assert data["status"] == "pending"
    assert data["user_id"] == "user123"
    assert "task_id" in data
    
    # Verify task was stored
    assert len(collections["backtest_tasks"].docs) == 1
    
    client.app.dependency_overrides.clear()


@pytest.mark.unit
def test_list_backtest_tasks(monkeypatch, mock_user):
    """Test listing user's backtest tasks"""
    # Create fresh collections for this test
    collections = {
        "backtest_tasks": FakeBacktestCollection([]),
        "backtest_results": FakeBacktestCollection([]),
        "users": FakeBacktestCollection([])
    }
    
    # Add some existing tasks
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
    
    # Test filter by status
    response = client.get("/api/backtest/tasks?status=pending")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["status"] == "pending"
    
    client.app.dependency_overrides.clear()


@pytest.mark.unit
def test_get_backtest_task(monkeypatch, mock_user):
    """Test getting task details"""
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
    
    # Test non-existent task
    response = client.get("/api/backtest/tasks/bt_999")
    assert response.status_code == 404
    
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
    client.app.dependency_overrides[get_current_user] = lambda: mock_user
    
    response = client.get("/api/backtest/tasks/pending/poll")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2
    assert all(task["task_id"] for task in data)
    
    client.app.dependency_overrides.clear()


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
    client.app.dependency_overrides[get_current_user] = lambda: mock_user
    
    response = client.post("/api/backtest/tasks/bt_001/claim?worker_id=worker1")
    assert response.status_code == 200
    assert "claimed successfully" in response.json()["message"]
    
    # Verify task status changed
    task = collections["backtest_tasks"].find_one({"task_id": "bt_001"})
    assert task["status"] == "claimed"
    assert task["worker_id"] == "worker1"
    
    # Try to claim again (should fail)
    response = client.post("/api/backtest/tasks/bt_001/claim?worker_id=worker2")
    assert response.status_code == 409
    
    client.app.dependency_overrides.clear()


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
    client.app.dependency_overrides[get_current_user] = lambda: mock_user
    
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
    
    response = client.post("/api/backtest/tasks/bt_001/report", json=payload)
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
    
    client.app.dependency_overrides.clear()


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
    client.app.dependency_overrides[get_current_user] = lambda: mock_user
    
    response = client.post(
        "/api/backtest/tasks/bt_001/fail?error_message=Strategy%20execution%20failed"
    )
    assert response.status_code == 200
    
    # Verify task status changed to failed
    task = collections["backtest_tasks"].find_one({"task_id": "bt_001"})
    assert task["status"] == "failed"
    assert "failed" in task.get("error_message", "").lower()
    
    client.app.dependency_overrides.clear()
