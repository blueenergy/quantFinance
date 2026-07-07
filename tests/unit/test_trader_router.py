"""Unit tests for trader router endpoints in quantFinance.

Tests cover:
- GET /trader/positions (fetch positions)
- POST /trader/positions/sync (sync positions batch)
- DELETE /trader/positions/cleanup (cleanup stale positions)
- POST /trader/account/sync (sync account info)
- GET /trader/account (fetch account info)
"""

import pytest
from unittest.mock import Mock, patch
from fastapi.testclient import TestClient
from bson import ObjectId

from routers import trader
from main import app
from auth import get_current_active_user


class FakeCollection:
    """Fake MongoDB collection for testing."""
    
    def __init__(self, docs=None):
        self.docs = docs or []
        self.inserted = []
        self.updated = []
        self.deleted = []
    
    def find(self, query=None, sort=None):
        """Find documents matching query."""
        class Cursor(list):
            def sort(self, *args, **kwargs):
                return self
            
            def limit(self, n):
                return Cursor(list(self)[:n])
        
        if query is None:
            query = {}
        
        results = []
        for doc in self.docs:
            if self._matches(doc, query):
                results.append(doc)
        
        return Cursor(results)
    
    def find_one(self, query, sort=None):
        """Find first document matching query."""
        cursor = self.find(query)
        return cursor[0] if cursor else None
    
    def insert_many(self, docs):
        """Insert multiple documents."""
        result = Mock()
        result.inserted_ids = [ObjectId() for _ in docs]
        self.inserted.extend(docs)
        self.docs.extend(docs)
        return result

    def insert_one(self, doc):
        """Insert one document."""
        doc = dict(doc)
        doc["_id"] = ObjectId()
        self.inserted.append(doc)
        self.docs.append(doc)
        result = Mock()
        result.inserted_id = doc["_id"]
        return result
    
    def update_one(self, filter_query, update_doc, upsert=False):
        """Update one document."""
        result = Mock()
        for i, doc in enumerate(self.docs):
            if self._matches(doc, filter_query):
                if "$set" in update_doc:
                    doc.update(update_doc["$set"])
                result.matched_count = 1
                self.updated.append((filter_query, update_doc))
                return result
        
        if upsert:
            new_doc = dict(filter_query)
            if "$set" in update_doc:
                new_doc.update(update_doc["$set"])
            new_doc["_id"] = ObjectId()
            self.docs.append(new_doc)
            self.inserted.append(new_doc)
            result.matched_count = 0
            result.upserted_id = new_doc["_id"]
        else:
            result.matched_count = 0
        
        return result
    
    def delete_many(self, query):
        """Delete documents matching query."""
        result = Mock()
        initial_count = len(self.docs)
        
        self.docs = [doc for doc in self.docs if not self._matches(doc, query)]
        result.deleted_count = initial_count - len(self.docs)
        
        self.deleted.append(query)
        return result
    
    def _matches(self, doc, query):
        """Check if document matches query."""
        if not query:
            return True
        
        for key, value in query.items():
            if key == "$or":
                if not any(self._matches(doc, branch) for branch in value):
                    return False
                continue
            if key == "$nin":
                # Handle $nin operator for array fields
                continue

            if isinstance(value, dict) and "$exists" in value:
                exists = key in doc
                if bool(value["$exists"]) != exists:
                    return False
                continue

            if key not in doc:
                return False

            if isinstance(value, dict):
                if "$nin" in value:
                    # Check if doc value is NOT in the $nin list
                    if doc[key] in value["$nin"]:
                        return False
                elif "$in" in value:
                    # Check if doc value is IN the $in list
                    if doc[key] not in value["$in"]:
                        return False
                else:
                    return False
            else:
                if doc[key] != value:
                    return False
        
        return True


class TestCleanupPositionsEndpoint:
    """Tests for DELETE /trader/positions/cleanup endpoint."""
    
    @pytest.fixture
    def setup(self, monkeypatch):
        """Setup test environment."""
        # Create fake collections
        self.positions_coll = FakeCollection([
            {
                "_id": ObjectId(),
                "user_id": "user_123",
                "securities_account_id": "000000000000000000000001",
                "symbol": "600000",
                "qty": 1000,
                "avg_price": 10.5
            },
            {
                "_id": ObjectId(),
                "user_id": "user_123",
                "securities_account_id": "000000000000000000000001",
                "symbol": "300001",
                "qty": 500,
                "avg_price": 25.0
            },
            {
                "_id": ObjectId(),
                "user_id": "user_123",
                "securities_account_id": "000000000000000000000001",
                "symbol": "000001",
                "qty": 200,
                "avg_price": 15.5
            }
        ])
        
        self.securities_coll = FakeCollection([
            {
                "_id": ObjectId("000000000000000000000001"),
                "user_id": "user_123",
                "account_id": "ACC_123",
                "broker": "miniQMT"
            }
        ])
        
        fake_collections = {
            "trader_positions": self.positions_coll,
            "securities_accounts": self.securities_coll
        }
        
        # Mock get_db_collections
        def mock_get_db_collections():
            return fake_collections
        
        monkeypatch.setattr(trader, "get_db_collections", mock_get_db_collections)
        
        # Override dependency
        app.dependency_overrides[get_current_active_user] = lambda: {"id": "user_123"}
        
        yield
        
        # Clean up overrides
        app.dependency_overrides = {}
    
    def test_cleanup_removes_stale_positions(self, setup):
        """Test that cleanup removes positions not in current_symbols list."""
        client = TestClient(app)
        
        # Initial check: should have 3 positions
        assert len(self.positions_coll.docs) == 3
        
        # Cleanup - keep only 600000, remove 300001 and 000001
        response = client.request(
            "DELETE",
            "/api/trader/positions/cleanup",
            json={
                "current_symbols": ["600000"],
                "securities_account_id": "000000000000000000000001"
            }
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["deleted_count"] == 2
        
        # Verify only 600000 remains
        assert len(self.positions_coll.docs) == 1
        assert self.positions_coll.docs[0]["symbol"] == "600000"
    
    def test_cleanup_with_multiple_symbols(self, setup):
        """Test cleanup keeping multiple symbols."""
        client = TestClient(app)
        
        # Keep 600000 and 300001, remove 000001
        response = client.request(
            "DELETE",
            "/api/trader/positions/cleanup",
            json={
                "current_symbols": ["600000", "300001"],
                "securities_account_id": "000000000000000000000001"
            }
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["deleted_count"] == 1
        
        # Verify 2 positions remain
        assert len(self.positions_coll.docs) == 2
        symbols = [doc["symbol"] for doc in self.positions_coll.docs]
        assert set(symbols) == {"600000", "300001"}
    
    def test_cleanup_with_empty_symbols(self, setup):
        """Test cleanup with empty symbols list (cleanup all)."""
        client = TestClient(app)
        
        # Empty list should cleanup everything
        response = client.request(
            "DELETE",
            "/api/trader/positions/cleanup",
            json={
                "current_symbols": [],
                "securities_account_id": "000000000000000000000001"
            }
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["deleted_count"] == 3
        
        # Verify all positions removed
        assert len(self.positions_coll.docs) == 0
    
    def test_cleanup_no_matches(self, setup):
        """Test cleanup when all current symbols already exist."""
        client = TestClient(app)
        
        # All symbols already held
        response = client.request(
            "DELETE",
            "/api/trader/positions/cleanup",
            json={
                "current_symbols": ["600000", "300001", "000001"],
                "securities_account_id": "000000000000000000000001"
            }
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["deleted_count"] == 0
        
        # Verify all positions still exist
        assert len(self.positions_coll.docs) == 3
    
    def test_cleanup_response_includes_timestamp(self, setup):
        """Test that cleanup response includes timestamp."""
        client = TestClient(app)
        
        response = client.request(
            "DELETE",
            "/api/trader/positions/cleanup",
            json={
                "current_symbols": ["600000"],
                "securities_account_id": "000000000000000000000001"
            }
        )
        
        assert response.status_code == 200
        data = response.json()
        assert "timestamp" in data
        assert isinstance(data["timestamp"], (int, float))

    def test_cleanup_requires_securities_account_id(self, setup):
        """Test that cleanup cannot operate across all user accounts."""
        client = TestClient(app)

        response = client.request(
            "DELETE",
            "/api/trader/positions/cleanup",
            json={
                "current_symbols": ["600000"],
                "securities_account_id": None,
            }
        )

        assert response.status_code == 400
        assert "securities_account_id is required" in response.json()["detail"]
    
    def test_cleanup_validates_input(self, setup):
        """Test that cleanup validates required parameters."""
        client = TestClient(app)
        
        # Missing current_symbols
        response = client.request(
            "DELETE",
            "/api/trader/positions/cleanup",
            json={"securities_account_id": None}
        )
        
        # FastAPI should reject due to missing required field
        assert response.status_code == 422


class TestSyncPositionsEndpoint:
    """Tests for POST /trader/positions/sync endpoint."""
    
    @pytest.fixture
    def setup(self, monkeypatch):
        """Setup test environment."""
        self.positions_coll = FakeCollection()
        self.securities_coll = FakeCollection([
            {
                "_id": ObjectId("000000000000000000000001"),
                "user_id": "user_123",
                "account_id": "ACC_123",
                "broker": "miniQMT"
            }
        ])
        
        fake_collections = {
            "trader_positions": self.positions_coll,
            "securities_accounts": self.securities_coll
        }
        
        def mock_get_db_collections():
            return fake_collections
        
        monkeypatch.setattr(trader, "get_db_collections", mock_get_db_collections)
        
        # Override dependency
        app.dependency_overrides[get_current_active_user] = lambda: {"id": "user_123"}
        
        yield
        
        # Clean up overrides
        app.dependency_overrides = {}
    
    def test_sync_positions_creates_batch(self, setup):
        """Test that sync_positions properly stores position batch."""
        client = TestClient(app)
        
        positions = [
            {"symbol": "600000", "qty": 1000, "avg_price": 10.5},
            {"symbol": "300001", "qty": 500, "avg_price": 25.0}
        ]
        
        response = client.post(
            "/api/trader/positions/sync",
            json={
                "positions": positions,
                "securities_account_id": "000000000000000000000001"
            }
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["synced_count"] == 2
        
        # Verify positions stored
        assert len(self.positions_coll.docs) == 2
    
    def test_sync_positions_clears_old_data(self, setup):
        """Test that sync_positions clears previous data."""
        client = TestClient(app)
        
        account_id = "000000000000000000000001"
        
        # Pre-populate with old position for the SAME account
        self.positions_coll.docs = [
            {
                "_id": ObjectId(),
                "user_id": "user_123",
                "securities_account_id": account_id,
                "symbol": "999999",
                "qty": 100,
                "avg_price": 5.0
            }
        ]
        
        # Sync new positions
        positions = [{"symbol": "600000.SH", "qty": 1000}]
        
        response = client.post(
            "/api/trader/positions/sync",
            json={
                "positions": positions,
                "securities_account_id": account_id
            }
        )
        
        assert response.status_code == 200
        
        # Old position should be removed
        assert len(self.positions_coll.docs) == 1
        assert self.positions_coll.docs[0]["symbol"] == "600000.SH"

    def test_sync_positions_requires_securities_account_id(self, setup):
        """Test that position sync cannot clear all accounts for a user."""
        client = TestClient(app)

        response = client.post(
            "/api/trader/positions/sync",
            json={
                "positions": [{"symbol": "600000.SH", "qty": 1000}],
                "securities_account_id": None,
            },
        )

        assert response.status_code == 400
        assert "securities_account_id is required" in response.json()["detail"]


class TestGetPositionsEndpoint:
    """Tests for GET /trader/positions endpoint."""
    
    @pytest.fixture
    def setup(self, monkeypatch):
        """Setup test environment."""
        self.positions_coll = FakeCollection([
            {
                "_id": ObjectId(),
                "user_id": "user_123",
                "symbol": "600000.SH",
                "qty": 1000,
                "avg_price": 10.5
            }
        ])
        
        self.stock_info_coll = FakeCollection([
            {
                "_id": ObjectId(),
                "symbol": "600000.SH",
                "name": "浦发银行"
            }
        ])
        
        fake_collections = {
            "trader_positions": self.positions_coll,
        }

        def mock_get_db_collections():
            return fake_collections

        def mock_get_data_db():
            return {"stock_info": self.stock_info_coll}

        monkeypatch.setattr(trader, "get_db_collections", mock_get_db_collections)
        monkeypatch.setattr(trader, "get_data_db", mock_get_data_db)
        
        # Override dependency
        app.dependency_overrides[get_current_active_user] = lambda: {"id": "user_123"}
        
        yield
        
        # Clean up overrides
        app.dependency_overrides = {}
    
    def test_get_positions_returns_list(self, setup):
        """Test that get_positions returns position list."""
        client = TestClient(app)
        
        response = client.get("/api/trader/positions")
        
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["count"] == 1
        assert len(data["data"]) == 1
        assert data["data"][0]["symbol"] == "600000.SH"
    
    def test_get_positions_enriches_with_stock_names(self, setup):
        """Test that get_positions enriches positions with stock names."""
        client = TestClient(app)
        
        response = client.get("/api/trader/positions")
        
        assert response.status_code == 200
        data = response.json()
        assert "stock_name" in data["data"][0]
        assert data["data"][0]["stock_name"] == "浦发银行"


@pytest.mark.unit
def test_get_pending_signals_stamps_first_seen(monkeypatch):
    signals_coll = FakeCollection(
        [
            {
                "_id": ObjectId(),
                "user_id": "user_123",
                "order_id": "order-buy",
                "symbol": "600000.SH",
                "action": "buy",
                "status": "pending",
                "is_executable": True,
                "mode": "live",
                "timestamp": 100.0,
            },
            {
                "_id": ObjectId(),
                "user_id": "user_123",
                "order_id": "order-sell",
                "symbol": "000001.SZ",
                "action": "sell",
                "status": "retry_pending",
                "is_executable": True,
                "mode": "live",
                "timestamp": 101.0,
                "first_seen_at": 99.0,
                "first_seen_by": "quantTrader:old",
            },
        ]
    )

    monkeypatch.setattr(trader, "get_db_collections", lambda: {"trade_signals": signals_coll})

    response = trader.get_pending_signals(
        limit=50,
        include_submitted=False,
        x_quanttrader_worker_id="quantTrader:worker-1",
        current_user={"id": "user_123"},
    )

    assert response["success"] is True
    by_order = {row["order_id"]: row for row in response["data"]}
    assert by_order["order-buy"]["first_seen_by"] == "quantTrader:worker-1"
    assert by_order["order-buy"]["first_seen_at"] > 0
    assert by_order["order-sell"]["first_seen_at"] == 99.0
    assert by_order["order-sell"]["first_seen_by"] == "quantTrader:old"


class TestCreateExecutionEndpoint:
    """Tests for POST /trader/executions snapshot rebuild scheduling."""

    @pytest.fixture
    def setup(self, monkeypatch):
        self.executions_coll = FakeCollection()
        self.signals_coll = FakeCollection(
            [
                {
                    "_id": ObjectId(),
                    "user_id": "user_123",
                    "order_id": "live-plan-abc123",
                    "plan_id": "plan-xyz",
                    "status": "submitted",
                }
            ]
        )
        fake_collections = {
            "trade_executions": self.executions_coll,
            "trade_signals": self.signals_coll,
        }

        def mock_get_db_collections():
            return fake_collections

        monkeypatch.setattr(trader, "get_db_collections", mock_get_db_collections)
        app.dependency_overrides[get_current_active_user] = lambda: {"id": "user_123"}

        self.rebuild_calls = []

        def mock_rebuild(trading_collections, finance_collections, plan, user_id, **kwargs):
            self.rebuild_calls.append(
                {
                    "plan_id": plan.get("plan_id"),
                    "user_id": user_id,
                    "reason": kwargs.get("reason"),
                    "trigger_order_id": kwargs.get("trigger_order_id"),
                }
            )

        monkeypatch.setattr(trader, "rebuild_live_snapshot_for_plan_guarded", mock_rebuild)

        def mock_get_trading_db_collections():
            return {
                "portfolio_trade_plans": FakeCollection(
                    [
                        {
                            "_id": ObjectId(),
                            "plan_id": "plan-xyz",
                            "user_id": "user_123",
                            "status": "approved",
                        }
                    ]
                )
            }

        monkeypatch.setattr(trader, "get_trading_db_collections", mock_get_trading_db_collections)

        yield

        app.dependency_overrides = {}

    def test_create_execution_schedules_snapshot_rebuild_for_terminal_status(self, setup, monkeypatch):
        started = []

        class ImmediateThread:
            def __init__(self, target=None, daemon=None):
                self._target = target

            def start(self):
                started.append(True)
                if self._target:
                    self._target()

        monkeypatch.setattr(trader.threading, "Thread", ImmediateThread)

        response = trader.create_execution(
            execution={
                "order_id": "live-plan-abc123",
                "symbol": "600000.SH",
                "action": "buy",
                "size": 100,
                "filled_size": 100,
                "filled_price": 10.5,
                "status": "filled",
                "timestamp": 1_700_000_000,
            },
            current_user={"id": "user_123"},
        )

        assert response["success"] is True
        assert started == [True]
        assert self.rebuild_calls == [
            {
                "plan_id": "plan-xyz",
                "user_id": "user_123",
                "reason": "execution",
                "trigger_order_id": "live-plan-abc123",
            }
        ]

    def test_create_execution_skips_snapshot_rebuild_for_non_terminal_status(self, setup, monkeypatch):
        started = []

        class ImmediateThread:
            def __init__(self, target=None, daemon=None):
                self._target = target

            def start(self):
                started.append(True)
                if self._target:
                    self._target()

        monkeypatch.setattr(trader.threading, "Thread", ImmediateThread)

        response = trader.create_execution(
            execution={
                "order_id": "live-plan-abc123",
                "symbol": "600000.SH",
                "action": "buy",
                "size": 100,
                "status": "submitted",
                "timestamp": 1_700_000_000,
            },
            current_user={"id": "user_123"},
        )

        assert response["success"] is True
        assert started == []
        assert self.rebuild_calls == []


@pytest.mark.unit
def test_cleanup_positions_endpoint_integration(monkeypatch):
    """Integration test for cleanup endpoint."""
    positions_coll = FakeCollection([
        {
            "_id": ObjectId(),
            "user_id": "user_123",
            "securities_account_id": "000000000000000000000001",
            "symbol": "600000",
            "qty": 1000
        },
        {
            "_id": ObjectId(),
            "user_id": "user_123",
            "securities_account_id": "000000000000000000000001",
            "symbol": "300001",
            "qty": 500
        }
    ])
    
    securities_coll = FakeCollection(
        [
            {
                "_id": ObjectId("000000000000000000000001"),
                "user_id": "user_123",
                "account_id": "ACC_123",
                "broker": "miniQMT",
            }
        ]
    )
    fake_collections = {
        "trader_positions": positions_coll,
        "securities_accounts": securities_coll,
    }
    
    def mock_get_db_collections():
        return fake_collections
    
    monkeypatch.setattr(trader, "get_db_collections", mock_get_db_collections)
    
    # Override dependency
    app.dependency_overrides[get_current_active_user] = lambda: {"id": "user_123"}
    
    try:
        client = TestClient(app)
        
        # Execute cleanup
        response = client.request(
            "DELETE",
            "/api/trader/positions/cleanup",
            json={
                "current_symbols": ["600000"],
                "securities_account_id": "000000000000000000000001"
            }
        )
        
        assert response.status_code == 200
        assert response.json()["deleted_count"] == 1
        assert len(positions_coll.docs) == 1
    finally:
        # Clean up overrides
        app.dependency_overrides = {}
