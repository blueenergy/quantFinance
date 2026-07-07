import pytest

from routers import trade_execution as te


class _FakeCursor(list):
    def __init__(self, docs, *, projection=None):
        super().__init__(docs)
        self.projection = projection

    def sort(self, field, direction=None):
        reverse = direction == -1
        return _FakeCursor(
            sorted(self, key=lambda row: row.get(field) or 0, reverse=reverse),
            projection=self.projection,
        )

    def limit(self, n):
        return _FakeCursor(list(self)[:n], projection=self.projection)


class _TradeActivitiesCollection:
    def __init__(self, docs):
        self.docs = list(docs)
        self.last_projection = None

    def find(self, query, projection=None):
        self.last_projection = projection
        filtered = [dict(doc) for doc in self.docs if self._matches(doc, query)]
        return _FakeCursor(filtered, projection=projection)

    def _matches(self, doc, query):
        for key, expected in query.items():
            if key == "$or":
                if not any(self._matches(doc, clause) for clause in expected):
                    return False
                continue
            if isinstance(expected, dict):
                if "$ne" in expected and doc.get(key) == expected["$ne"]:
                    return False
                continue
            if doc.get(key) != expected:
                return False
        return True


class _StockInfoCollection:
    def find(self, query, projection=None):  # noqa: ARG002
        symbols = set(query.get("symbol", {}).get("$in", []))
        return [{"symbol": symbol, "name": f"Name-{symbol}"} for symbol in symbols]


@pytest.fixture
def activity_docs():
    user_id = "user-1"
    return {
        "executions": [
            {
                "_id": "exec-filled",
                "user_id": user_id,
                "worker_mode": "live",
                "symbol": "000001",
                "side": "BUY",
                "status": "filled",
                "filled_size": 100,
                "filled_price": 10.5,
                "strategy_name": "s1",
                "timestamp": 100,
                "order_id": "order-filled",
            },
            {
                "_id": "exec-empty",
                "user_id": user_id,
                "worker_mode": "live",
                "symbol": "000002",
                "side": "BUY",
                "status": "filled",
                "filled_size": 0,
                "strategy_name": "s1",
                "timestamp": 90,
                "order_id": "order-empty",
            },
            {
                "_id": "exec-backtest",
                "user_id": user_id,
                "worker_mode": "backtest",
                "symbol": "000003",
                "side": "BUY",
                "status": "filled",
                "filled_size": 50,
                "filled_price": 8.0,
                "strategy_name": "s1",
                "timestamp": 200,
                "order_id": "order-backtest",
            },
        ],
        "signals": [
            {
                "_id": "sig-pending",
                "user_id": user_id,
                "worker_mode": "live",
                "symbol": "600000",
                "action": "buy",
                "status": "pending",
                "price": 11.0,
                "strategy": "s2",
                "timestamp": 80,
                "order_id": "order-pending",
            },
            {
                "_id": "sig-executed-only",
                "user_id": user_id,
                "mode": "live",
                "symbol": "600001",
                "action": "sell",
                "status": "executed",
                "avg_price": 12.0,
                "strategy": "s2",
                "timestamp": 70,
                "order_id": "order-signal-only",
                "effective_limit_price": 12.1,
                "last_market_price": 12.0,
                "submitted_age_seconds": 30,
                "broker_status_msg": "ok",
            },
            {
                "_id": "sig-dup-order",
                "user_id": user_id,
                "worker_mode": "live",
                "symbol": "600002",
                "action": "buy",
                "status": "executed",
                "avg_price": 9.0,
                "strategy": "s2",
                "timestamp": 60,
                "order_id": "order-filled",
            },
        ],
    }


def _patch_trade_activity_db(monkeypatch, activity_docs):
    executions = _TradeActivitiesCollection(activity_docs["executions"])
    signals = _TradeActivitiesCollection(activity_docs["signals"])
    stock_info = _StockInfoCollection()

    monkeypatch.setattr(
        te,
        "get_db_collections",
        lambda: {
            "trade_executions": executions,
            "trade_signals": signals,
        },
    )
    monkeypatch.setattr(
        te,
        "get_data_db",
        lambda: {"stock_info": stock_info},
    )
    return executions, signals


@pytest.mark.unit
def test_execution_matches_status_filter_executed_requires_fill():
    filled = {"status": "filled", "filled_size": 100}
    empty = {"status": "filled", "filled_size": 0}

    assert te._execution_matches_status_filter(filled, "executed") is True
    assert te._execution_matches_status_filter(empty, "executed") is False
    assert te._execution_matches_status_filter(empty, "all") is True


@pytest.mark.unit
def test_signal_matches_status_filter_pending_and_executed():
    pending = {"status": "submitted"}
    executed = {"status": "executed"}

    assert te._signal_matches_status_filter(pending, "pending") is True
    assert te._signal_matches_status_filter(executed, "pending") is False
    assert te._signal_matches_status_filter(executed, "executed") is True
    assert te._signal_matches_status_filter(pending, "all") is True


@pytest.mark.unit
def test_get_all_trade_activities_executions_tab_excludes_signals(monkeypatch, activity_docs):
    executions, signals = _patch_trade_activity_db(monkeypatch, activity_docs)

    result = te.get_all_trade_activities(
        limit=100,
        skip=0,
        start_date=None,
        end_date=None,
        days=None,
        all_dates=True,
        status_filter="all",
        activity_type="executions",
        current_user={"id": "user-1"},
    )

    assert result["success"] is True
    assert result["activity_type"] == "executions"
    assert all(row["source"] == "trade_execution" for row in result["data"])
    assert {row["order_id"] for row in result["data"]} == {"order-filled", "order-empty"}
    assert executions.last_projection == te._EXECUTION_ACTIVITY_PROJECTION
    assert signals.last_projection is None


@pytest.mark.unit
def test_get_all_trade_activities_executions_executed_filter(monkeypatch, activity_docs):
    _patch_trade_activity_db(monkeypatch, activity_docs)

    result = te.get_all_trade_activities(
        limit=100,
        skip=0,
        start_date=None,
        end_date=None,
        days=None,
        all_dates=True,
        status_filter="executed",
        activity_type="executions",
        current_user={"id": "user-1"},
    )

    assert len(result["data"]) == 1
    assert result["data"][0]["order_id"] == "order-filled"
    assert result["data"][0]["status"] == "FILLED"
    assert result["data"][0]["stock_name"].startswith("Name-000001")


@pytest.mark.unit
def test_get_all_trade_activities_signals_tab_excludes_executions(monkeypatch, activity_docs):
    executions, signals = _patch_trade_activity_db(monkeypatch, activity_docs)

    result = te.get_all_trade_activities(
        limit=100,
        skip=0,
        start_date=None,
        end_date=None,
        days=None,
        all_dates=True,
        status_filter="all",
        activity_type="signals",
        current_user={"id": "user-1"},
    )

    assert result["activity_type"] == "signals"
    assert all(row["source"] == "trade_signal" for row in result["data"])
    assert {row["order_id"] for row in result["data"]} == {
        "order-pending",
        "order-signal-only",
        "order-filled",
    }
    assert executions.last_projection is None
    assert signals.last_projection == te._SIGNAL_ACTIVITY_PROJECTION


@pytest.mark.unit
def test_get_all_trade_activities_signals_pending_filter(monkeypatch, activity_docs):
    _patch_trade_activity_db(monkeypatch, activity_docs)

    result = te.get_all_trade_activities(
        limit=100,
        skip=0,
        start_date=None,
        end_date=None,
        days=None,
        all_dates=True,
        status_filter="pending",
        activity_type="signals",
        current_user={"id": "user-1"},
    )

    assert len(result["data"]) == 1
    row = result["data"][0]
    assert row["order_id"] == "order-pending"
    assert row["status"] == "PENDING"
    assert row["status_category"] == "pending"


@pytest.mark.unit
def test_get_all_trade_activities_signals_projection_fields(monkeypatch, activity_docs):
    _patch_trade_activity_db(monkeypatch, activity_docs)

    result = te.get_all_trade_activities(
        limit=100,
        skip=0,
        start_date=None,
        end_date=None,
        days=None,
        all_dates=True,
        status_filter="executed",
        activity_type="signals",
        current_user={"id": "user-1"},
    )

    row = next(item for item in result["data"] if item["order_id"] == "order-signal-only")
    assert row["effective_limit_price"] == 12.1
    assert row["last_market_price"] == 12.0
    assert row["submitted_age_seconds"] == 30
    assert row["broker_status_msg"] == "ok"
    assert row["raw_signal_status"] == "executed"


@pytest.mark.unit
def test_get_all_trade_activities_legacy_merge_dedupes_by_order_id(monkeypatch, activity_docs):
    _patch_trade_activity_db(monkeypatch, activity_docs)

    result = te.get_all_trade_activities(
        limit=100,
        skip=0,
        start_date=None,
        end_date=None,
        days=None,
        all_dates=True,
        status_filter="all",
        activity_type=None,
        current_user={"id": "user-1"},
    )

    assert result["activity_type"] is None
    sources = {row["order_id"]: row["source"] for row in result["data"]}
    assert sources["order-filled"] == "trade_execution"
    assert "order-signal-only" in sources
    assert sources["order-signal-only"] == "trade_signal"
    # Legacy merge must keep dropping unfilled executions, matching pre-refactor behavior.
    assert "order-empty" not in sources
