import pytest

from routers import trade_execution as te


class _FakeCursor(list):
    def sort(self, field, direction=None):
        return _FakeCursor(sorted(self, key=lambda row: row.get(field) or 0, reverse=direction == -1))

    def limit(self, n):
        return _FakeCursor(list(self)[:n])


class _FakeWriteCollection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])
        self.insert_count = 0
        self.update_count = 0

    def find_one(self, query, projection=None, sort=None):  # noqa: ARG002
        for doc in self.docs:
            if all(doc.get(key) == value for key, value in query.items()):
                return doc
        return None

    def insert_one(self, doc):
        self.insert_count += 1
        row = dict(doc)
        row.setdefault("_id", f"id-{self.insert_count}")
        self.docs.append(row)

        class Result:
            inserted_id = row["_id"]

        return Result()

    def update_one(self, query, update):
        self.update_count += 1
        for doc in self.docs:
            if all(doc.get(key) == value for key, value in query.items()):
                doc.update(update.get("$set", {}))

                class Result:
                    matched_count = 1
                    modified_count = 1

                return Result()

        class Result:
            matched_count = 0
            modified_count = 0

        return Result()


@pytest.mark.unit
def test_group_executions_by_order_keeps_latest_per_order_id():
    executions = [
        {"_id": "1", "order_id": "A", "timestamp": 1, "value": "old"},
        {"_id": "2", "order_id": "A", "timestamp": 2, "value": "new"},
        {"_id": "3", "order_id": "B", "timestamp": 5, "value": "only"},
    ]

    grouped = te._group_executions_by_order(executions)

    assert len(grouped) == 2
    by_order = {d["order_id"]: d for d in grouped}

    assert by_order["A"]["timestamp"] == 2
    assert by_order["A"]["value"] == "new"
    assert by_order["B"]["timestamp"] == 5
    assert by_order["B"]["value"] == "only"


@pytest.mark.unit
def test_group_executions_by_order_falls_back_to_id_when_no_order_id():
    executions = [
        {"_id": "x1", "timestamp": 1},
        {"_id": "x1", "timestamp": 3},  # same _id, later timestamp should win
        {"_id": "x2", "timestamp": 2},
    ]

    grouped = te._group_executions_by_order(executions)

    # since order_id is missing, grouping key is str(_id)
    assert len(grouped) == 2
    by_id = {str(d["_id"]): d for d in grouped}

    assert by_id["x1"]["timestamp"] == 3
    assert by_id["x2"]["timestamp"] == 2


@pytest.mark.unit
def test_compute_realized_pnl_simple_round_trip():
    # buy 100 @10, sell 100 @12 -> pnl = 200, net_quantity = 0, avg_cost back to 0
    trades = [
        {"timestamp": 1, "side": "BUY", "filled_size": 100, "filled_price": 10.0},
        {"timestamp": 2, "side": "SELL", "filled_size": 100, "filled_price": 12.0},
    ]

    result = te._compute_realized_pnl_for_group(trades)

    assert result["net_quantity"] == 0
    assert result["realized_pnl"] == 200.0
    assert result["avg_cost"] == 0.0


@pytest.mark.unit
def test_compute_realized_pnl_partial_sell():
    # buy 100 @10, sell 40 @12 -> pnl = 80, net_quantity = 60, avg_cost stays at 10
    trades = [
        {"timestamp": 1, "side": "BUY", "filled_size": 100, "filled_price": 10.0},
        {"timestamp": 2, "side": "SELL", "filled_size": 40, "filled_price": 12.0},
    ]

    result = te._compute_realized_pnl_for_group(trades)

    assert result["net_quantity"] == 60
    assert result["realized_pnl"] == 80.0
    assert result["avg_cost"] == 10.0


@pytest.mark.unit
def test_compute_realized_pnl_sell_without_prior_buy_is_not_treated_as_profit():
    # sell 100 @12 without any prior buy in system view -> pnl should be 0, net_quantity stays 0, avg_cost 0
    trades = [
        {"timestamp": 1, "side": "SELL", "filled_size": 100, "filled_price": 12.0},
    ]

    result = te._compute_realized_pnl_for_group(trades)

    assert result["net_quantity"] == 0
    assert result["realized_pnl"] == 0.0
    assert result["avg_cost"] == 0.0


@pytest.mark.unit
def test_compute_realized_pnl_with_commission_on_both_sides():
    # buy 100 @10 with 1 fee, sell 100 @12 with 1 fee ->
    # avg_cost after buy = (10*100 + 1) / 100 = 10.01
    # realized_pnl on sell = (12-10.01)*100 - 1 = 198
    trades = [
        {"timestamp": 1, "side": "BUY", "filled_size": 100, "filled_price": 10.0, "commission": 1.0},
        {"timestamp": 2, "side": "SELL", "filled_size": 100, "filled_price": 12.0, "commission": 1.0},
    ]

    result = te._compute_realized_pnl_for_group(trades)

    assert result["net_quantity"] == 0
    assert result["realized_pnl"] == pytest.approx(198.0)
    assert result["avg_cost"] == 0.0


@pytest.mark.unit
def test_compute_realized_pnl_with_sell_side_commission_only():
    # buy 100 @10, no fee; sell 100 @12 with 1 fee -> realized_pnl = 200 - 1 = 199
    trades = [
        {"timestamp": 1, "side": "BUY", "filled_size": 100, "filled_price": 10.0},
        {"timestamp": 2, "side": "SELL", "filled_size": 100, "filled_price": 12.0, "commission": 1.0},
    ]

    result = te._compute_realized_pnl_for_group(trades)

    assert result["net_quantity"] == 0
    assert result["realized_pnl"] == pytest.approx(199.0)


@pytest.mark.unit
def test_execution_filled_size_uses_trade_execution_contract_field():
    execution = {"status": "filled", "filled_size": 100, "size": 200}

    assert te._is_filled_execution(execution) is True
    assert te._execution_filled_size(execution) == 100


@pytest.mark.unit
def test_execution_filled_size_does_not_treat_signal_fields_as_execution_contract():
    execution = {"status": "filled", "filled_qty": 100, "size": 200}

    assert te._is_filled_execution(execution) is False
    assert te._execution_filled_size(execution) == 0


@pytest.mark.unit
def test_trade_time_window_query_uses_trade_execution_time_contract():
    query = te._trade_time_window_query(days=30, start_date=None, end_date=None)
    conditions = query["$or"]

    assert any("timestamp" in condition for condition in conditions)
    assert any("date" in condition for condition in conditions)
    assert not any(
        "created_at" in condition or "updated_at" in condition or "executed_at" in condition
        for condition in conditions
    )


@pytest.mark.unit
def test_signal_time_window_query_uses_signal_lifecycle_timestamps():
    query = te._signal_time_window_query(days=30, start_date=None, end_date=None)
    conditions = query["$or"]

    assert any("timestamp" in condition for condition in conditions)
    assert any("updated_at" in condition for condition in conditions)
    assert any("executed_at" in condition for condition in conditions)


@pytest.mark.unit
def test_signal_activity_timestamp_prefers_update_time():
    signal = {"timestamp": 1, "updated_at": 10, "executed_at": 9}

    assert te._signal_activity_timestamp(signal) == 10


@pytest.mark.unit
def test_create_trader_execution_is_idempotent(monkeypatch):
    executions = _FakeWriteCollection()
    signals = _FakeWriteCollection(
        [
            {
                "order_id": "order-1",
                "user_id": "user-1",
                "plan_id": "plan-1",
                "mode": "live",
            }
        ]
    )
    monkeypatch.setattr(
        te,
        "get_db_collections",
        lambda: {"trade_executions": executions, "trade_signals": signals},
    )

    payload = {
        "order_id": "order-1",
        "status": "filled",
        "filled_size": 100,
        "filled_price": 10.5,
        "broker_order_id": "broker-1",
        "timestamp": 1000.0,
    }

    first = te.create_trader_execution(dict(payload), current_user={"id": "user-1"})
    second = te.create_trader_execution({**payload, "timestamp": 1001.0}, current_user={"id": "user-1"})

    assert first["inserted"] is True
    assert second["inserted"] is False
    assert executions.insert_count == 1
    assert executions.update_count == 1
    assert len(executions.docs) == 1
    assert executions.docs[0]["timestamp"] == 1001.0


@pytest.mark.unit
def test_trade_pnl_summary_unrealized_zero_when_no_minute_price(monkeypatch):
    user_id = "u2"

    # same executions as before (open position), but minute_bars has no price -> unrealized_pnl should be 0
    exec_docs = [
        {
            "_id": "1",
            "user_id": user_id,
            "symbol": "000001",
            "strategy": "s1",
            "side": "BUY",
            "filled_size": 100,
            "filled_price": 10.0,
            "timestamp": 1,
            "order_id": "o1",
        },
        {
            "_id": "2",
            "user_id": user_id,
            "symbol": "000001",
            "strategy": "s1",
            "side": "BUY",
            "filled_size": 100,
            "filled_price": 12.0,
            "timestamp": 2,
            "order_id": "o2",
        },
    ]

    class FakeExecCollection:
        def __init__(self, docs):
            self.docs = docs

        def find(self, query):
            uid = query.get("user_id")
            return _FakeCursor([d for d in self.docs if d.get("user_id") == uid])

    class FakeMinuteBarsCollection:
        def __init__(self, docs):
            self.docs = docs

        def aggregate(self, pipeline):  # noqa: ARG002
            return self.docs

    class FakeStockInfoCollection:
        def __init__(self, docs):
            self.docs = docs

        def find(self, query, projection=None):  # noqa: ARG002
            symbols = set(query.get("symbol", {}).get("$in", []))
            return [d for d in self.docs if d.get("symbol") in symbols]

    minute_docs = []  # no price available
    stock_docs = [{"symbol": "000001", "name": "TestStock"}]

    fake_collections = {
        "trade_executions": FakeExecCollection(exec_docs),
    }
    fake_data_db = {
        "minute_bars": FakeMinuteBarsCollection(minute_docs),
        "stock_info": FakeStockInfoCollection(stock_docs),
    }

    monkeypatch.setattr(te, "get_db_collections", lambda: fake_collections)
    monkeypatch.setattr(te, "get_data_db", lambda: fake_data_db)

    result = te.get_trade_pnl_summary(current_user={"id": user_id})

    assert result["success"] is True
    assert len(result["data"]) == 1
    row = result["data"][0]

    assert row["net_quantity"] == 200
    # realized pnl is not the focus here; key is unrealized should be 0 without price
    assert row["unrealized_pnl"] == 0.0
    assert result["total_unrealized_pnl"] == 0.0


@pytest.mark.unit
def test_trade_pnl_summary_unrealized_zero_when_net_quantity_zero(monkeypatch):
    user_id = "u3"

    # buy 100@10, sell 100@12 -> net_qty = 0, realized_pnl = 200, unrealized_pnl must be 0 even with price
    exec_docs = [
        {
            "_id": "1",
            "user_id": user_id,
            "symbol": "000001",
            "strategy": "s1",
            "side": "BUY",
            "filled_size": 100,
            "filled_price": 10.0,
            "timestamp": 1,
            "order_id": "o1",
        },
        {
            "_id": "2",
            "user_id": user_id,
            "symbol": "000001",
            "strategy": "s1",
            "side": "SELL",
            "filled_size": 100,
            "filled_price": 12.0,
            "timestamp": 2,
            "order_id": "o2",
        },
    ]

    class FakeExecCollection2:
        def __init__(self, docs):
            self.docs = docs

        def find(self, query):
            uid = query.get("user_id")
            return _FakeCursor([d for d in self.docs if d.get("user_id") == uid])

    class FakeMinuteBarsCollection2:
        def __init__(self, docs):
            self.docs = docs

        def aggregate(self, pipeline):  # noqa: ARG002
            return self.docs

    class FakeStockInfoCollection2:
        def __init__(self, docs):
            self.docs = docs

        def find(self, query, projection=None):  # noqa: ARG002
            symbols = set(query.get("symbol", {}).get("$in", []))
            return [d for d in self.docs if d.get("symbol") in symbols]

    minute_docs = [{"_id": "000001", "close": 15.0}]
    stock_docs = [{"symbol": "000001", "name": "TestStock"}]

    fake_collections = {
        "trade_executions": FakeExecCollection2(exec_docs),
    }
    fake_data_db = {
        "minute_bars": FakeMinuteBarsCollection2(minute_docs),
        "stock_info": FakeStockInfoCollection2(stock_docs),
    }

    monkeypatch.setattr(te, "get_db_collections", lambda: fake_collections)
    monkeypatch.setattr(te, "get_data_db", lambda: fake_data_db)

    result = te.get_trade_pnl_summary(current_user={"id": user_id})

    assert result["success"] is True
    row = result["data"][0]
    assert row["net_quantity"] == 0
    assert row["realized_pnl"] == pytest.approx(200.0)
    assert row["unrealized_pnl"] == 0.0
    assert result["total_unrealized_pnl"] == 0.0


@pytest.mark.unit
def test_trade_pnl_summary_multiple_symbols_total_unrealized_matches_sum(monkeypatch):
    user_id = "u4"

    # symbol 000001: buy 100@10, buy 100@12, sell 50@13 -> net_qty = 150, avg_cost = 11
    # symbol 000002: buy 50@20 -> net_qty = 50, avg_cost = 20
    exec_docs = [
        {
            "_id": "1",
            "user_id": user_id,
            "symbol": "000001",
            "strategy": "s1",
            "side": "BUY",
            "filled_size": 100,
            "filled_price": 10.0,
            "timestamp": 1,
            "order_id": "o1",
        },
        {
            "_id": "2",
            "user_id": user_id,
            "symbol": "000001",
            "strategy": "s1",
            "side": "BUY",
            "filled_size": 100,
            "filled_price": 12.0,
            "timestamp": 2,
            "order_id": "o2",
        },
        {
            "_id": "3",
            "user_id": user_id,
            "symbol": "000001",
            "strategy": "s1",
            "side": "SELL",
            "filled_size": 50,
            "filled_price": 13.0,
            "timestamp": 3,
            "order_id": "o3",
        },
        {
            "_id": "4",
            "user_id": user_id,
            "symbol": "000002",
            "strategy": "s2",
            "side": "BUY",
            "filled_size": 50,
            "filled_price": 20.0,
            "timestamp": 1,
            "order_id": "o4",
        },
    ]

    class FakeExecCollection3:
        def __init__(self, docs):
            self.docs = docs

        def find(self, query):
            uid = query.get("user_id")
            return _FakeCursor([d for d in self.docs if d.get("user_id") == uid])

    class FakeMinuteBarsCollection3:
        def __init__(self, docs):
            self.docs = docs

        def aggregate(self, pipeline):  # noqa: ARG002
            return self.docs

    class FakeStockInfoCollection3:
        def __init__(self, docs):
            self.docs = docs

        def find(self, query, projection=None):  # noqa: ARG002
            symbols = set(query.get("symbol", {}).get("$in", []))
            return [d for d in self.docs if d.get("symbol") in symbols]

    # prices: 000001 -> 12, 000002 -> 19
    # unrealized_1 = (12-11)*150 = 150
    # unrealized_2 = (19-20)*50 = -50
    # total = 100
    minute_docs = [
        {"_id": "000001", "close": 12.0},
        {"_id": "000002", "close": 19.0},
    ]
    stock_docs = [
        {"symbol": "000001", "name": "Stock1"},
        {"symbol": "000002", "name": "Stock2"},
    ]

    fake_collections = {
        "trade_executions": FakeExecCollection3(exec_docs),
    }
    fake_data_db = {
        "minute_bars": FakeMinuteBarsCollection3(minute_docs),
        "stock_info": FakeStockInfoCollection3(stock_docs),
    }

    monkeypatch.setattr(te, "get_db_collections", lambda: fake_collections)
    monkeypatch.setattr(te, "get_data_db", lambda: fake_data_db)

    result = te.get_trade_pnl_summary(current_user={"id": user_id})

    assert result["success"] is True
    rows = result["data"]
    total_from_rows = sum(r["unrealized_pnl"] for r in rows)

    assert result["total_unrealized_pnl"] == pytest.approx(total_from_rows)
