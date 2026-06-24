import asyncio
from datetime import datetime

import pytest

from routers import portfolio_plans


class _Cursor(list):
    def sort(self, field, direction=None):
        if isinstance(field, list):
            field, direction = field[0]
        return _Cursor(sorted(self, key=lambda row: row.get(field) or "", reverse=direction == -1))

    def limit(self, n):
        return _Cursor(list(self)[:n])


class _Collection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    def find(self, query=None, projection=None):
        return _Cursor([dict(doc) for doc in self.docs if self._matches(doc, query or {})])

    def find_one(self, query=None, projection=None, sort=None):
        rows = self.find(query, projection)
        if sort:
            rows = rows.sort(sort)
        return rows[0] if rows else None

    def insert_one(self, doc):
        self.docs.append(dict(doc))

    def insert_many(self, docs):
        self.docs.extend(dict(doc) for doc in docs)

    def update_one(self, query, update):
        for doc in self.docs:
            if self._matches(doc, query):
                doc.update(update.get("$set", {}))
                return

    def replace_one(self, query, new_doc, upsert=False):
        for idx, doc in enumerate(self.docs):
            if self._matches(doc, query):
                self.docs[idx] = dict(new_doc)
                return
        if upsert:
            self.docs.append(dict(new_doc))

    def delete_many(self, query):
        self.docs = [doc for doc in self.docs if not self._matches(doc, query)]

    def count_documents(self, query=None):
        return len(self.find(query or {}))

    def _matches(self, doc, query):
        for key, expected in query.items():
            if key == "$or":
                if not any(self._matches(doc, branch) for branch in expected):
                    return False
                continue
            value = doc.get(key)
            if isinstance(expected, dict):
                if "$in" in expected and value not in expected["$in"]:
                    return False
                if "$nin" in expected and value in expected["$nin"]:
                    return False
                if "$ne" in expected and value == expected["$ne"]:
                    return False
                if "$gte" in expected and not (value >= expected["$gte"]):
                    return False
                if "$lte" in expected and not (value <= expected["$lte"]):
                    return False
                if "$exists" in expected:
                    has_key = key in doc and doc.get(key) is not None
                    if expected["$exists"] and not has_key:
                        return False
                    if not expected["$exists"] and has_key:
                        return False
                continue
            if isinstance(value, list):
                if expected not in value:
                    return False
                continue
            if value != expected:
                return False
        return True


class _FailingFindCollection(_Collection):
    def find(self, query=None, projection=None):
        raise AssertionError("trade_executions should not be queried by plan detail")


def _collections():
    return {
        "portfolio_strategy_configs": _Collection(
            [
                {"strategy_template_id": "s1", "status": "enabled", "universe_index": "csi1000"},
                {"strategy_template_id": "s2", "status": "enabled", "universe_index": "csi300"},
                {"strategy_template_id": "disabled-s1", "status": "disabled"},
            ]
        ),
        "portfolio_trade_plans": _Collection(
            [
                {"plan_id": "plan-user-1", "strategy_template_id": "s1", "user_id": "user-1", "base_date": "20250102", "status": "needs_review"},
                {
                    "plan_id": "plan-template-1",
                    "strategy_template_id": "s1",
                    "user_id": "user-1",
                    "base_date": "20250104",
                    "status": "generated",
                },
                {"plan_id": "plan-user-2", "strategy_template_id": "s2", "user_id": "user-2", "base_date": "20250103", "status": "needs_review"},
            ]
        ),
        "portfolio_trade_plan_items": _Collection(),
        "portfolio_plan_reviews": _Collection(),
        "portfolio_paper_positions": _Collection(),
        "portfolio_paper_executions": _Collection(),
        "portfolio_paper_equity": _Collection(),
        "portfolio_live_equity": _Collection(),
        "portfolio_plan_generation_tasks": _Collection(),
        "portfolio_plan_operation_logs": _Collection(),
        "portfolio_worker_status": _Collection(),
        "portfolio_strategy_param_presets": _Collection(
            [
                {
                    "preset_id": "preset-1",
                    "strategy_template_id": "s1",
                    "user_id": "user-1",
                    "status": "enabled",
                    "params": {"top_n": 20, "rebalance_days": 20},
                }
            ]
        ),
    }


def _finance_collections():
    return {
        "securities_accounts": _Collection(
            [
                {
                    "_id": "acct-1",
                    "user_id": "user-1",
                    "account_id": "A123",
                    "broker": "miniQMT",
                    "live_trading_enabled": True,
                    "risk_limits": {"max_order_amount": 20000, "max_daily_amount": 50000},
                },
                {
                    "_id": "acct-off",
                    "user_id": "user-1",
                    "account_id": "A124",
                    "broker": "miniQMT",
                },
            ]
        ),
        "trade_signals": _Collection(),
        "trade_executions": _Collection(),
        "trader_account_equity": _Collection(),
        "trader_accounts": _Collection([{"user_id": "user-1", "securities_account_id": "acct-1", "available_cash": 100000, "synced_at": 10}]),
        "trader_positions": _Collection([{"user_id": "user-1", "securities_account_id": "acct-1", "symbol": "600000", "can_use_volume": 500}]),
    }


def _data_collections():
    return {
        "stock_info": _Collection([{"symbol": "000001", "name": "平安银行"}, {"symbol": "600000", "name": "浦发银行"}]),
        "volume_price": _Collection(),
        "volume_price_realtime_latest": _Collection([{"symbol": "000001", "close": 10.0}, {"symbol": "600000", "close": 12.0}]),
    }


def test_bulk_reject_plan_items_enqueues_single_regeneration(monkeypatch):
    collections = _collections()
    plan = collections["portfolio_trade_plans"].docs[0]
    plan["excluded_symbols"] = ["000003"]
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)

    response = asyncio.run(
        portfolio_plans.bulk_reject_portfolio_plan_items(
            "plan-user-1",
            portfolio_plans.BulkRejectPlanItemsRequest(symbols=["000001", "600000", "000001", " "]),
            current_user={"id": "user-1"},
        )
    )

    data = response["data"]
    assert data["rejected_symbols"] == ["000001", "600000"]
    assert data["excluded_symbols"] == ["000001", "000003", "600000"]
    assert len(collections["portfolio_plan_generation_tasks"].docs) == 1
    task = collections["portfolio_plan_generation_tasks"].docs[0]
    assert task["task_type"] == "generate_portfolio_plan"
    assert task["operation_type"] == "bulk_reject_symbols"
    assert task["operation_symbol"] is None
    assert task["excluded_symbols"] == ["000001", "000003", "600000"]

    updated_plan = collections["portfolio_trade_plans"].find_one({"plan_id": "plan-user-1", "user_id": "user-1"})
    assert updated_plan["excluded_symbols"] == ["000001", "000003", "600000"]
    assert len(collections["portfolio_plan_operation_logs"].docs) == 1
    log = collections["portfolio_plan_operation_logs"].docs[0]
    assert log["operation_type"] == "bulk_reject_symbols"
    assert log["operation_symbol"] is None
    assert log["payload"]["operation_symbols"] == ["000001", "600000"]


def test_bulk_reject_plan_items_rejects_empty_symbols(monkeypatch):
    collections = _collections()
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)

    with pytest.raises(portfolio_plans.HTTPException) as exc:
        asyncio.run(
            portfolio_plans.bulk_reject_portfolio_plan_items(
                "plan-user-1",
                portfolio_plans.BulkRejectPlanItemsRequest(symbols=["", " "]),
                current_user={"id": "user-1"},
            )
        )

    assert exc.value.status_code == 400
    assert not collections["portfolio_plan_generation_tasks"].docs


def test_bulk_reject_plan_items_requires_reselectable_status(monkeypatch):
    collections = _collections()
    collections["portfolio_trade_plans"].docs[0]["status"] = "approved"
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)

    with pytest.raises(portfolio_plans.HTTPException) as exc:
        asyncio.run(
            portfolio_plans.bulk_reject_portfolio_plan_items(
                "plan-user-1",
                portfolio_plans.BulkRejectPlanItemsRequest(symbols=["000001", "600000"]),
                current_user={"id": "user-1"},
            )
        )

    assert exc.value.status_code == 409
    assert not collections["portfolio_plan_generation_tasks"].docs


def test_list_portfolio_plans_is_scoped_to_current_user(monkeypatch):
    collections = _collections()
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)

    response = asyncio.run(
        portfolio_plans.list_portfolio_plans(
            strategy_template_id=None,
            status=None,
            plan_type=None,
            start_date=None,
            end_date=None,
            limit=50,
            current_user={"id": "user-1"},
        )
    )

    assert [row["plan_id"] for row in response["data"]] == ["plan-template-1", "plan-user-1"]


def test_list_portfolio_plans_filters_by_template(monkeypatch):
    collections = _collections()
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)

    response = asyncio.run(
        portfolio_plans.list_portfolio_plans(
            strategy_template_id="s1",
            status=None,
            plan_type=None,
            start_date=None,
            end_date=None,
            limit=50,
            current_user={"id": "user-1"},
        )
    )

    assert [row["plan_id"] for row in response["data"]] == ["plan-template-1", "plan-user-1"]


def test_list_portfolio_strategies_returns_global_catalog(monkeypatch):
    collections = _collections()
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)

    response = asyncio.run(portfolio_plans.list_portfolio_strategies(status=None, current_user={"id": "user-1"}))

    assert [row["strategy_template_id"] for row in response["data"]] == ["disabled-s1", "growth_cycle_index_enhanced", "s1", "s2"]


def test_review_plan_rejects_cross_user_access(monkeypatch):
    collections = _collections()
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)

    with pytest.raises(portfolio_plans.HTTPException) as exc:
        portfolio_plans._review_plan("plan-user-2", "approved", "ok", {"id": "user-1"})

    assert exc.value.status_code == 404
    assert not collections["portfolio_plan_reviews"].docs


def test_review_plan_writes_user_id(monkeypatch):
    collections = _collections()
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)

    response = portfolio_plans._review_plan("plan-user-1", "approved", "ok", {"id": "user-1", "username": "alice"})

    assert response["success"] is True
    assert collections["portfolio_plan_reviews"].docs[0]["user_id"] == "user-1"
    assert collections["portfolio_trade_plans"].find_one({"plan_id": "plan-user-1"})["status"] == "approved"


def test_generate_portfolio_plan_uses_current_user(monkeypatch):
    collections = _collections()
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)

    response = asyncio.run(
        portfolio_plans.generate_portfolio_plan(
            portfolio_plans.GeneratePlanRequest(
                strategy_template_id="s1",
                base_date="2025-01-04",
                mode="auto",
                preset_id="preset-1",
                params_override={"cash_buffer": 0.05, "initial_capital": 250_000},
            ),
            current_user={"id": "user-1"},
        )
    )

    assert response["success"] is True
    assert response["data"]["status"] == "pending"
    assert response["data"]["user_id"] == "user-1"
    assert response["data"]["strategy_template_id"] == "s1"
    assert response["data"]["base_date"] == "2025-01-04"
    assert response["data"]["mode"] == "auto"
    assert "initial_capital_override" not in response["data"]
    assert response["data"]["preset_id"] == "preset-1"
    assert response["data"]["params_override"] == {
        "top_n": 20,
        "rebalance_days": 20,
        "cash_buffer": 0.05,
        "initial_capital": 250_000,
    }
    assert collections["portfolio_plan_generation_tasks"].docs[0]["task_id"] == response["data"]["task_id"]


def test_generate_portfolio_plan_rejects_disabled_strategy(monkeypatch):
    collections = _collections()
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)

    with pytest.raises(portfolio_plans.HTTPException) as exc:
        asyncio.run(
            portfolio_plans.generate_portfolio_plan(
                portfolio_plans.GeneratePlanRequest(strategy_template_id="disabled-s1", base_date="2025-01-04", mode="auto"),
                current_user={"id": "user-1"},
            )
        )

    assert exc.value.status_code == 400


def test_get_portfolio_plan_generation_task_is_scoped_to_current_user(monkeypatch):
    collections = _collections()
    collections["portfolio_plan_generation_tasks"].docs.extend(
        [
            {"task_id": "task-1", "user_id": "user-1", "status": "completed"},
            {"task_id": "task-2", "user_id": "user-2", "status": "completed"},
        ]
    )
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)

    response = asyncio.run(portfolio_plans.get_portfolio_plan_generation_task("task-1", current_user={"id": "user-1"}))

    assert response["data"]["task_id"] == "task-1"
    with pytest.raises(portfolio_plans.HTTPException) as exc:
        asyncio.run(portfolio_plans.get_portfolio_plan_generation_task("task-2", current_user={"id": "user-1"}))
    assert exc.value.status_code == 404


def test_list_portfolio_worker_status_returns_recent_workers(monkeypatch):
    collections = _collections()
    collections["portfolio_worker_status"].docs.extend(
        [
            {
                "worker_id": "worker-old",
                "worker_type": "portfolio_plan_generator",
                "status": "idle",
                "last_seen_at": "2025-01-01T09:00:00",
            },
            {
                "worker_id": "worker-new",
                "worker_type": "portfolio_plan_generator",
                "status": "completed",
                "last_seen_at": "2025-01-01T10:00:00",
            },
            {
                "worker_id": "other-worker",
                "worker_type": "other",
                "status": "idle",
                "last_seen_at": "2025-01-01T11:00:00",
            },
        ]
    )
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)

    response = asyncio.run(
        portfolio_plans.list_portfolio_worker_status(
            worker_type="portfolio_plan_generator",
            limit=20,
            current_user={"id": "user-1"},
        )
    )

    assert [row["worker_id"] for row in response["data"]] == ["worker-new", "worker-old"]


def test_get_portfolio_plan_generation_watermark_uses_strategy_universe(monkeypatch):
    collections = _collections()
    scoring_runs = _Collection(
        [
            {
                "score_date": "20250105",
                "universe_index": "csi1000",
                "status": "completed",
                "updated_at": "2025-01-05T18:00:00",
            },
            {
                "score_date": "20250106",
                "universe_index": "csi1000",
                "status": "running",
                "updated_at": "2025-01-06T09:00:00",
            },
            {
                "score_date": "20250106",
                "universe_index": "csi300",
                "status": "completed",
                "updated_at": "2025-01-06T09:00:00",
            },
        ]
    )
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)
    stock_scores = _Collection(
        [
            {"score_date": "20250105", "symbol": "000001", "index_codes": ["csi1000"]},
            {"score_date": "20250106", "symbol": "000001", "index_codes": ["csi1000"]},
            {"score_date": "20250106", "symbol": "000002", "index_codes": ["csi1000"]},
            {"score_date": "20250106", "symbol": "000300", "index_codes": ["csi300"]},
        ]
    )
    monkeypatch.setattr(portfolio_plans, "finance_db", {"scoring_runs": scoring_runs, "stock_scores": stock_scores})

    response = asyncio.run(
        portfolio_plans.get_portfolio_plan_generation_watermark(
            strategy_template_id="s1",
            base_date="2025-01-06",
            current_user={"id": "user-1"},
        )
    )

    assert response["data"]["universe_index"] == "csi1000"
    assert response["data"]["base_date"] == "20250106"
    assert response["data"]["target_status"] == "running"
    assert response["data"]["target_is_running"] is True
    assert response["data"]["latest_completed_scoring_run"]["score_date"] == "20250105"
    assert response["data"]["latest_available_score_date"] == "20250106"
    assert response["data"]["latest_available_score_count"] == 2
    assert response["data"]["latest_available_score_source"] == "stock_scores"
    assert response["data"]["latest_available_score_scope"] == "index_codes"


def test_get_portfolio_plan_generation_watermark_falls_back_to_global_scores(monkeypatch):
    collections = _collections()
    scoring_runs = _Collection()
    stock_scores = _Collection(
        [
            {"score_date": "20250105", "symbol": "000001"},
            {"score_date": "20250107", "symbol": "000002"},
            {"score_date": "20250107", "symbol": "000003"},
        ]
    )
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)
    monkeypatch.setattr(portfolio_plans, "finance_db", {"scoring_runs": scoring_runs, "stock_scores": stock_scores})

    response = asyncio.run(
        portfolio_plans.get_portfolio_plan_generation_watermark(
            strategy_template_id="s1",
            base_date="2025-01-06",
            current_user={"id": "user-1"},
        )
    )

    assert response["data"]["target_scoring_run"] is None
    assert response["data"]["latest_available_score_date"] == "20250107"
    assert response["data"]["latest_available_score_count"] == 2
    assert response["data"]["latest_available_score_scope"] == "global"


def test_plan_execution_context_derives_phase_and_stamps_sell_settled():
    finance = _finance_collections()
    finance["trade_signals"] = portfolio_plans_collection = _Collection(
        [
            {"plan_id": "plan-user-1", "user_id": "user-1", "mode": "live", "execution_phase": "sell", "status": "filled"},
            {"plan_id": "plan-user-1", "user_id": "user-1", "mode": "live", "execution_phase": "buy", "status": "pending"},
        ]
    )
    trading = {
        "portfolio_paper_executions": _Collection(),
        "portfolio_trade_plans": _Collection([{"plan_id": "plan-user-1", "user_id": "user-1"}]),
    }
    import routers.portfolio_plans as pp

    orig = pp.get_trading_db_collections
    pp.get_trading_db_collections = lambda: trading
    try:
        ctx = pp._plan_execution_context(finance, {"plan_id": "plan-user-1"}, "user-1")
    finally:
        pp.get_trading_db_collections = orig

    assert ctx["execution_mode"] == "live"
    assert ctx["execution_phase_state"] == "sell_settled"
    plan_doc = trading["portfolio_trade_plans"].find_one({"plan_id": "plan-user-1"})
    assert plan_doc.get("sell_settled_at") is not None


def test_plan_execution_context_selling_does_not_stamp_sell_settled():
    finance = _finance_collections()
    finance["trade_signals"] = _Collection(
        [
            {"plan_id": "plan-user-1", "user_id": "user-1", "mode": "live", "execution_phase": "sell", "status": "submitted"},
        ]
    )
    trading = {
        "portfolio_paper_executions": _Collection(),
        "portfolio_trade_plans": _Collection([{"plan_id": "plan-user-1", "user_id": "user-1"}]),
    }
    import routers.portfolio_plans as pp

    orig = pp.get_trading_db_collections
    pp.get_trading_db_collections = lambda: trading
    try:
        ctx = pp._plan_execution_context(finance, {"plan_id": "plan-user-1"}, "user-1")
    finally:
        pp.get_trading_db_collections = orig

    assert ctx["execution_phase_state"] == "selling"
    plan_doc = trading["portfolio_trade_plans"].find_one({"plan_id": "plan-user-1"})
    assert plan_doc.get("sell_settled_at") is None


def test_publish_live_signals_dry_run_builds_contract(monkeypatch):
    collections = _collections()
    collections["portfolio_trade_plans"].docs[0]["status"] = "approved"
    collections["portfolio_trade_plan_items"].docs.extend(
        [
            {
                "plan_id": "plan-user-1",
                "user_id": "user-1",
                "symbol": "000001",
                "action": "buy",
                "delta_shares": 100,
                "target_shares": 100,
                "estimated_price": 10.0,
                "rank": 1,
            },
            {
                "plan_id": "plan-user-1",
                "user_id": "user-1",
                "symbol": "600000",
                "action": "sell",
                "delta_shares": -200,
                "target_shares": 0,
                "estimated_price": 12.0,
                "rank": 2,
            },
        ]
    )
    finance = _finance_collections()
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: finance)
    monkeypatch.setattr(portfolio_plans, "get_data_db", _data_collections)

    response = asyncio.run(
        portfolio_plans.publish_portfolio_plan_live_signals(
            "plan-user-1",
            portfolio_plans.PublishLiveSignalsRequest(securities_account_id="acct-1", dry_run=True),
            current_user={"id": "user-1"},
        )
    )

    assert response["success"] is True
    assert response["data"]["dry_run"] is True
    assert response["data"]["inserted_count"] == 0
    assert len(response["data"]["signals"]) == 2
    signal = response["data"]["signals"][0]
    assert signal["mode"] == "live"
    assert signal["is_executable"] is True
    assert signal["source"] == "portfolio_plan"
    assert signal["status"] == "pending"
    assert signal["securities_account_id"] == "acct-1"
    assert signal["execution_phase"] == "buy"
    assert signal["execution_priority"] == 1
    assert signal["plan_item_rank"] == 1
    assert signal["reference_price"] == 10.0
    assert signal["max_slippage_bps"] == 100
    assert signal["price_ceiling"] == 10.1
    assert signal["effective_limit_price"] == 10.1
    assert signal["activate_after"]
    assert signal["valid_until"]
    activate_after = datetime.fromisoformat(signal["activate_after"])
    valid_until = datetime.fromisoformat(signal["valid_until"])
    assert (valid_until - activate_after).total_seconds() == 600
    assert signal["phase_barrier"] == {"type": "plan_sells_terminal", "plan_id": "plan-user-1"}
    sell_signal = response["data"]["signals"][1]
    assert sell_signal["execution_phase"] == "sell"
    assert sell_signal["execution_priority"] == -9998
    assert sell_signal["price_floor"] == 11.88
    assert sell_signal["phase_barrier"] is None
    assert response["data"]["risk_report"]["blocked_count"] == 0


def test_publish_live_signals_write_is_idempotent(monkeypatch):
    collections = _collections()
    collections["portfolio_trade_plans"].docs[0]["status"] = "approved"
    collections["portfolio_trade_plan_items"].docs.append(
        {
            "plan_id": "plan-user-1",
            "user_id": "user-1",
            "symbol": "000001",
            "action": "buy",
            "delta_shares": 100,
            "target_shares": 100,
            "estimated_price": 10.0,
            "rank": 1,
        }
    )
    finance = _finance_collections()
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: finance)
    monkeypatch.setattr(portfolio_plans, "get_data_db", _data_collections)

    request = portfolio_plans.PublishLiveSignalsRequest(securities_account_id="acct-1", dry_run=False)
    first = asyncio.run(portfolio_plans.publish_portfolio_plan_live_signals("plan-user-1", request, current_user={"id": "user-1"}))
    second = asyncio.run(portfolio_plans.publish_portfolio_plan_live_signals("plan-user-1", request, current_user={"id": "user-1"}))

    assert first["data"]["inserted_count"] == 1
    assert second["data"]["inserted_count"] == 0
    assert second["data"]["existing_count"] == 1
    assert len(finance["trade_signals"].docs) == 1


def test_publish_live_signals_rounds_protected_prices_to_tick(monkeypatch):
    collections = _collections()
    collections["portfolio_trade_plans"].docs[0]["status"] = "approved"
    collections["portfolio_trade_plan_items"].docs.extend(
        [
            {
                "plan_id": "plan-user-1",
                "user_id": "user-1",
                "symbol": "000001",
                "action": "buy",
                "delta_shares": 100,
                "target_shares": 100,
                "estimated_price": 10.005,
                "rank": 1,
            },
            {
                "plan_id": "plan-user-1",
                "user_id": "user-1",
                "symbol": "600000",
                "action": "sell",
                "delta_shares": -100,
                "target_shares": 0,
                "estimated_price": 10.005,
                "rank": 2,
            },
        ]
    )
    finance = _finance_collections()
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: finance)
    monkeypatch.setattr(portfolio_plans, "get_data_db", _data_collections)

    response = asyncio.run(
        portfolio_plans.publish_portfolio_plan_live_signals(
            "plan-user-1",
            portfolio_plans.PublishLiveSignalsRequest(securities_account_id="acct-1", dry_run=True),
            current_user={"id": "user-1"},
        )
    )

    buy_signal = response["data"]["signals"][0]
    sell_signal = response["data"]["signals"][1]
    assert buy_signal["price_ceiling"] == 10.11
    assert buy_signal["effective_limit_price"] == 10.11
    assert sell_signal["price_floor"] == 9.9
    assert sell_signal["effective_limit_price"] == 9.9


def test_publish_live_signals_blocks_after_paper_execution(monkeypatch):
    collections = _collections()
    collections["portfolio_trade_plans"].docs[0]["status"] = "approved"
    collections["portfolio_paper_executions"].docs.append(
        {"plan_id": "plan-user-1", "user_id": "user-1", "symbol": "000001"}
    )
    finance = _finance_collections()
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: finance)

    with pytest.raises(portfolio_plans.HTTPException) as exc:
        asyncio.run(
            portfolio_plans.publish_portfolio_plan_live_signals(
                "plan-user-1",
                portfolio_plans.PublishLiveSignalsRequest(securities_account_id="acct-1", dry_run=True),
                current_user={"id": "user-1"},
            )
        )

    assert exc.value.status_code == 400
    assert "paper executions" in exc.value.detail


def test_execute_paper_blocks_after_live_signal_publish(monkeypatch):
    collections = _collections()
    collections["portfolio_trade_plans"].docs[0]["status"] = "approved"
    finance = _finance_collections()
    finance["trade_signals"].docs.append(
        {
            "user_id": "user-1",
            "plan_id": "plan-user-1",
            "source": "portfolio_plan",
            "mode": "live",
            "order_id": "live-order-1",
        }
    )
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: finance)

    with pytest.raises(portfolio_plans.HTTPException) as exc:
        portfolio_plans._apply_paper_execution(
            collections,
            data_collections={},
            plan_id="plan-user-1",
            user_id="user-1",
        )

    assert exc.value.status_code == 400
    assert "live signals" in exc.value.detail


def test_execute_paper_force_still_blocks_after_live_signal_publish(monkeypatch):
    collections = _collections()
    collections["portfolio_trade_plans"].docs[0]["status"] = "approved"
    finance = _finance_collections()
    finance["trade_signals"].docs.append(
        {
            "user_id": "user-1",
            "plan_id": "plan-user-1",
            "source": "portfolio_plan",
            "mode": "live",
            "order_id": "live-order-1",
        }
    )
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: finance)

    with pytest.raises(portfolio_plans.HTTPException) as exc:
        portfolio_plans._apply_paper_execution(
            collections,
            data_collections={},
            plan_id="plan-user-1",
            force=True,
            user_id="user-1",
        )

    assert exc.value.status_code == 400
    assert "live signals" in exc.value.detail


def test_attach_live_context_preserves_plan_current_without_live_signals():
    finance = _finance_collections()
    items = [
        {
            "plan_id": "plan-1",
            "symbol": "300184.SZ",
            "action": "sell",
            "current_shares": 500,
            "target_shares": 0,
            "delta_shares": -500,
        }
    ]

    enriched = portfolio_plans._attach_live_plan_item_context(
        finance_collections=finance,
        plan={"plan_id": "plan-1"},
        items=items,
        user_id="user-1",
    )

    assert enriched[0]["current_shares"] == 500
    assert enriched[0]["plan_current_shares"] == 500


def test_execute_paper_inherits_lineage_positions(monkeypatch):
    collections = _collections()
    parent = {
        "plan_id": "parent-plan",
        "strategy_template_id": "s1",
        "user_id": "user-1",
        "params_hash": "hash-1",
        "base_date": "20250101",
        "execute_date": "20250102",
        "status": "executed_paper",
    }
    child = {
        "plan_id": "child-plan",
        "strategy_template_id": "s1",
        "user_id": "user-1",
        "params_hash": "hash-1",
        "base_date": "20250103",
        "execute_date": "20250103",
        "status": "approved",
        "params_snapshot": {"initial_capital": 1_000_000, "lot_size": 100},
    }
    collections["portfolio_trade_plans"].docs.extend([parent, child])
    collections["portfolio_paper_positions"].docs.append(
        {
            "strategy_template_id": "s1",
            "user_id": "user-1",
            "params_hash": "hash-1",
            "as_of_date": "20250102",
            "plan_id": "parent-plan",
            "cash": 99_000,
            "equity": 100_000,
            "positions": [{"symbol": "000001", "name": "平安银行", "shares": 100, "cost_basis": 10.0}],
        }
    )
    collections["portfolio_trade_plan_items"].docs.append(
        {
            "plan_id": "child-plan",
            "user_id": "user-1",
            "symbol": "000001",
            "name": "平安银行",
            "industry": "Bank",
            "rank": 1,
            "delta_shares": -100,
        }
    )
    data = _data_collections()
    data["volume_price"].docs.append({"symbol": "000001", "trade_date": "20250103", "open": 11.0, "close": 12.0})
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: _finance_collections())

    result = portfolio_plans._apply_paper_execution(
        collections,
        data_collections=data,
        plan_id="child-plan",
        user_id="user-1",
    )

    assert result["inherited_from_plan_id"] == "parent-plan"
    assert result["position_snapshot"]["cash"] == 100_100
    assert result["position_snapshot"]["positions"] == []
    assert collections["portfolio_paper_equity"].find_one({"plan_id": "child-plan"})["inherited_from_plan_id"] == "parent-plan"


def test_get_portfolio_plan_lineage_equity_returns_combo_rows(monkeypatch):
    collections = _collections()
    collections["portfolio_trade_plans"].docs.extend(
        [
            {
                "plan_id": "parent-plan",
                "strategy_template_id": "s1",
                "user_id": "user-1",
                "params_hash": "hash-1",
                "base_date": "20250101",
                "plan_type": "rebalance",
                "status": "executed_paper",
            },
            {
                "plan_id": "child-plan",
                "strategy_template_id": "s1",
                "user_id": "user-1",
                "params_hash": "hash-1",
                "base_date": "20250103",
                "plan_type": "rebalance",
                "status": "executed_paper",
            },
        ]
    )
    collections["portfolio_paper_equity"].docs.extend(
        [
            {"strategy_template_id": "s1", "user_id": "user-1", "plan_id": "parent-plan", "date": "20250102", "equity": 100_000},
            {"strategy_template_id": "s1", "user_id": "user-1", "params_hash": "hash-1", "plan_id": "child-plan", "date": "20250103", "equity": 101_000},
            {"strategy_template_id": "s1", "user_id": "user-1", "params_hash": "other", "plan_id": "other-plan", "date": "20250103", "equity": 1},
        ]
    )
    collections["portfolio_paper_positions"].docs.append(
        {
            "strategy_template_id": "s1",
            "user_id": "user-1",
            "params_hash": "hash-1",
            "plan_id": "child-plan",
            "as_of_date": "20250104",
            "equity": 102_000,
            "cash": 10_000,
            "market_value": 92_000,
        }
    )
    collections["portfolio_paper_positions"].docs.append(
        {
            "strategy_template_id": "s1",
            "user_id": "user-1",
            "params_hash": "hash-1",
            "plan_id": "parent-plan",
            "as_of_date": "20250104",
            "equity": 101_500,
            "cash": 10_000,
            "market_value": 91_500,
            "updated_at": 1,
        }
    )
    collections["portfolio_paper_positions"].docs[-2]["updated_at"] = 2
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)

    response = asyncio.run(
        portfolio_plans.get_portfolio_plan_lineage_equity(
            "child-plan",
            start_date=None,
            end_date=None,
            current_user={"id": "user-1"},
        )
    )

    assert response["data"]["source_plan_ids"] == ["child-plan", "parent-plan"]
    assert [row["plan_id"] for row in response["data"]["rows"]] == ["parent-plan", "child-plan", "child-plan"]
    assert response["data"]["rows"][1]["is_current_plan"] is True
    assert response["data"]["rows"][2]["source"] == "position_snapshot"
    assert response["data"]["rows"][2]["equity"] == 102_000


def test_plan_live_executions_are_scoped_to_plan_order_ids(monkeypatch):
    collections = _collections()
    finance = _finance_collections()
    finance["trade_signals"].docs.extend(
        [
            {
                "user_id": "user-1",
                "plan_id": "plan-user-1",
                "strategy_template_id": "s1",
                "source": "portfolio_plan",
                "mode": "live",
                "order_id": "plan-order-1",
                "created_at": 1,
            },
            {
                "user_id": "user-1",
                "plan_id": "other-plan",
                "strategy_template_id": "other-strategy",
                "source": "portfolio_plan",
                "mode": "live",
                "order_id": "other-order",
                "created_at": 2,
            },
        ]
    )
    finance["trade_executions"].docs.extend(
        [
            {"user_id": "user-1", "order_id": "plan-order-1", "strategy_template_id": "s1", "timestamp": 10},
            {"user_id": "user-1", "order_id": "other-order", "strategy_template_id": "other-strategy", "timestamp": 11},
            {"user_id": "user-1", "order_id": "manual-order", "strategy_template_id": "manual", "timestamp": 12},
        ]
    )
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: finance)

    response = asyncio.run(
        portfolio_plans.get_portfolio_plan_live_executions(
            "plan-user-1",
            current_user={"id": "user-1"},
        )
    )

    assert response["success"] is True
    assert [row["order_id"] for row in response["data"]["signals"]] == ["plan-order-1"]
    assert [row["order_id"] for row in response["data"]["executions"]] == ["plan-order-1"]
    assert response["data"]["executions"][0]["execution_time"] == "1970-01-01 08:00:10"


def test_plan_detail_enriches_strategy_and_account_positions_separately(monkeypatch):
    collections = _collections()
    collections["portfolio_trade_plans"].docs[0]["status"] = "approved"
    collections["portfolio_trade_plans"].docs[0]["live_signal_count"] = 1
    collections["portfolio_trade_plan_items"].docs.append(
        {
            "plan_id": "plan-user-1",
            "user_id": "user-1",
            "strategy_template_id": "s1",
            "symbol": "000001",
            "action": "buy",
            "current_shares": 0,
            "target_shares": 100,
            "delta_shares": 100,
            "rank": 1,
        }
    )
    finance = _finance_collections()
    finance["trade_signals"].docs.append(
        {
            "user_id": "user-1",
            "plan_id": "plan-user-1",
            "strategy_template_id": "s1",
            "source": "portfolio_plan",
            "mode": "live",
            "order_id": "plan-order-1",
            "symbol": "000001",
            "action": "buy",
            "size": 100,
            "status": "filled",
            "securities_account_id": "acct-1",
        }
    )
    finance["trade_executions"] = _FailingFindCollection()
    finance["trader_positions"].docs.append(
        {
            "user_id": "user-1",
            "securities_account_id": "acct-1",
            "symbol": "000001.SZ",
            "qty": 600,
            "can_use_volume": 500,
        }
    )
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: finance)
    monkeypatch.setattr(portfolio_plans, "get_data_db", _data_collections)

    response = asyncio.run(portfolio_plans.get_portfolio_plan("plan-user-1", current_user={"id": "user-1"}))

    item = response["data"]["items"][0]
    assert item["plan_current_shares"] == 0
    assert item["current_shares"] == 100
    assert item["strategy_current_shares"] == 100
    assert item["account_current_shares"] == 600
    assert item["account_available_shares"] == 500
    assert item["live_filled_qty"] == 100
    assert item["live_remaining_qty"] == 0
    assert item["live_status"] == "filled"
    assert response["data"]["execution_mode"] == "live"
    assert response["data"]["live_execution_context"]["has_live_signals"] is True
    assert response["data"]["live_execution_context"]["signal_count"] == 1


def test_plan_detail_without_live_signals_marks_live_context_false(monkeypatch):
    collections = _collections()
    collections["portfolio_trade_plan_items"].docs.append(
        {
            "plan_id": "plan-user-1",
            "user_id": "user-1",
            "strategy_template_id": "s1",
            "symbol": "000001",
            "action": "buy",
            "current_shares": 0,
            "target_shares": 100,
            "delta_shares": 100,
            "rank": 1,
        }
    )
    finance = _finance_collections()
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: finance)
    monkeypatch.setattr(portfolio_plans, "get_data_db", _data_collections)

    response = asyncio.run(portfolio_plans.get_portfolio_plan("plan-user-1", current_user={"id": "user-1"}))

    item = response["data"]["items"][0]
    assert item["current_shares"] == 0
    assert item["live_filled_qty"] == 0
    assert item["account_current_shares"] is None
    assert response["data"]["execution_mode"] == "not_executed"
    assert response["data"]["live_execution_context"]["has_live_signals"] is False
    assert response["data"]["live_execution_context"]["signal_count"] == 0


def test_paper_executed_plan_with_live_signals_prefers_live_context(monkeypatch):
    collections = _collections()
    collections["portfolio_trade_plans"].docs[0]["status"] = "executed_paper"
    collections["portfolio_trade_plan_items"].docs.append(
        {
            "plan_id": "plan-user-1",
            "user_id": "user-1",
            "strategy_template_id": "s1",
            "symbol": "000001",
            "action": "buy",
            "current_shares": 0,
            "target_shares": 100,
            "delta_shares": 100,
            "rank": 1,
        }
    )
    finance = _finance_collections()
    finance["trade_signals"].docs.append(
        {
            "user_id": "user-1",
            "plan_id": "plan-user-1",
            "strategy_template_id": "s1",
            "source": "portfolio_plan",
            "mode": "live",
            "order_id": "stale-live-order",
            "symbol": "000001",
            "status": "filled",
            "securities_account_id": "acct-1",
        }
    )
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: finance)
    monkeypatch.setattr(portfolio_plans, "get_data_db", _data_collections)

    response = asyncio.run(portfolio_plans.get_portfolio_plan("plan-user-1", current_user={"id": "user-1"}))

    assert response["data"]["execution_mode"] == "live"
    context = response["data"]["live_execution_context"]
    assert context["has_live_signals"] is True
    assert context["signal_count"] == 1
    assert context["has_paper_conflict"] is True


def test_paper_executed_plan_without_live_signals_marks_paper(monkeypatch):
    collections = _collections()
    collections["portfolio_trade_plans"].docs[0]["status"] = "executed_paper"
    finance = _finance_collections()
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: finance)
    monkeypatch.setattr(portfolio_plans, "get_data_db", _data_collections)

    response = asyncio.run(portfolio_plans.get_portfolio_plan("plan-user-1", current_user={"id": "user-1"}))

    assert response["data"]["execution_mode"] == "paper"
    context = response["data"]["live_execution_context"]
    assert context["has_live_signals"] is False
    assert context["signal_count"] == 0
    assert context["has_paper_conflict"] is False


def test_plan_live_equity_returns_sorted_rows(monkeypatch):
    collections = _collections()
    collections["portfolio_live_equity"].docs.extend(
        [
            {"user_id": "user-1", "plan_id": "plan-user-1", "date": "20250605", "equity": 100200},
            {"user_id": "user-1", "plan_id": "plan-user-1", "date": "20250604", "equity": 100000},
            {"user_id": "user-2", "plan_id": "plan-user-1", "date": "20250604", "equity": 1},
        ]
    )
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)

    response = asyncio.run(
        portfolio_plans.get_portfolio_plan_live_equity(
            "plan-user-1",
            start_date=None,
            end_date=None,
            current_user={"id": "user-1"},
        )
    )

    assert [row["date"] for row in response["data"]] == ["20250604", "20250605"]
    assert [row["equity"] for row in response["data"]] == [100000, 100200]


def test_publish_live_signals_blocks_when_live_switch_is_off(monkeypatch):
    collections = _collections()
    collections["portfolio_trade_plans"].docs[0]["status"] = "approved"
    collections["portfolio_trade_plan_items"].docs.append(
        {
            "plan_id": "plan-user-1",
            "user_id": "user-1",
            "symbol": "000001",
            "action": "buy",
            "delta_shares": 100,
            "target_shares": 100,
            "estimated_price": 10.0,
            "rank": 1,
        }
    )
    finance = _finance_collections()
    finance["trader_accounts"].docs.append({"user_id": "user-1", "securities_account_id": "acct-off", "available_cash": 100000, "synced_at": 10})
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: finance)
    monkeypatch.setattr(portfolio_plans, "get_data_db", _data_collections)

    with pytest.raises(portfolio_plans.HTTPException) as exc:
        asyncio.run(
            portfolio_plans.publish_portfolio_plan_live_signals(
                "plan-user-1",
                portfolio_plans.PublishLiveSignalsRequest(securities_account_id="acct-off", dry_run=False),
                current_user={"id": "user-1"},
            )
        )

    assert exc.value.status_code == 409
    assert not finance["trade_signals"].docs


def test_publish_live_signals_blocks_when_position_pct_exceeded(monkeypatch):
    collections = _collections()
    collections["portfolio_trade_plans"].docs[0]["status"] = "approved"
    collections["portfolio_trade_plan_items"].docs.append(
        {
            "plan_id": "plan-user-1",
            "user_id": "user-1",
            "symbol": "000001",
            "action": "buy",
            "delta_shares": 100,
            "target_shares": 100,
            "estimated_price": 10.0,
            "rank": 1,
        }
    )
    finance = _finance_collections()
    finance["securities_accounts"].docs[0]["risk_limits"]["max_position_pct"] = 50
    finance["trader_accounts"].docs[0]["total_asset"] = 1000
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: finance)
    monkeypatch.setattr(portfolio_plans, "get_data_db", _data_collections)

    with pytest.raises(portfolio_plans.HTTPException) as exc:
        asyncio.run(
            portfolio_plans.publish_portfolio_plan_live_signals(
                "plan-user-1",
                portfolio_plans.PublishLiveSignalsRequest(securities_account_id="acct-1", dry_run=False),
                current_user={"id": "user-1"},
            )
        )

    assert exc.value.status_code == 409
    blockers = exc.value.detail["risk_report"]["items"][0]["blockers"]
    assert "max_position_pct_exceeded" in blockers
    assert not finance["trade_signals"].docs


def _remainder_setup(monkeypatch):
    collections = _collections()
    plan = collections["portfolio_trade_plans"].docs[0]
    plan["status"] = "approved"
    plan["live_signal_count"] = 1
    plan["live_signals_published_at"] = 1
    plan["params_snapshot"] = {"lot_size": 100}
    collections["portfolio_trade_plan_items"].docs.append(
        {
            "plan_id": "plan-user-1",
            "user_id": "user-1",
            "symbol": "000001",
            "name": "平安银行",
            "action": "buy",
            "delta_shares": 1000,
            "target_shares": 1000,
            "estimated_price": 10.0,
            "current_shares": 0,
            "rank": 1,
        }
    )
    collections["portfolio_plan_operation_logs"] = _Collection()
    finance = _finance_collections()
    # An original published buy that got cancelled after a 400-share partial fill.
    finance["trade_signals"].docs.append(
        {
            "order_id": "live-plan-orig",
            "user_id": "user-1",
            "plan_id": "plan-user-1",
            "mode": "live",
            "source": "portfolio_plan",
            "symbol": "000001",
            "action": "buy",
            "status": "cancelled",
            "size": 1000,
            "filled_qty": 400,
            "securities_account_id": "acct-1",
        }
    )
    # Live account holds the 400 already filled (synced).
    finance["trader_positions"].docs.append(
        {"user_id": "user-1", "securities_account_id": "acct-1", "symbol": "000001", "qty": 400, "can_use_volume": 400}
    )
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: finance)
    monkeypatch.setattr(portfolio_plans, "get_data_db", _data_collections)
    monkeypatch.setattr(portfolio_plans, "resolve_market_reference_price", lambda symbol: (10.0, "realtime"))
    return collections, finance


def test_replan_remainder_dry_run_computes_gap(monkeypatch):
    collections, finance = _remainder_setup(monkeypatch)

    response = asyncio.run(
        portfolio_plans.replan_portfolio_plan_remainder(
            "plan-user-1",
            portfolio_plans.ReplanRemainderRequest(securities_account_id="acct-1", dry_run=True),
            current_user={"id": "user-1"},
        )
    )

    data = response["data"]
    assert data["dry_run"] is True
    assert data["inserted_count"] == 0
    assert data["actionable_count"] == 1
    assert len(data["signals"]) == 1
    signal = data["signals"][0]
    assert signal["source"] == "plan_topup"
    assert signal["action"] == "buy"
    assert signal["size"] == 600
    assert signal["order_id"].startswith("live-topup-")
    assert signal["replaces_order_ids"] == ["live-plan-orig"]
    assert not finance["trade_signals"].docs[1:]  # nothing inserted


def test_replan_remainder_confirm_inserts_and_is_idempotent(monkeypatch):
    collections, finance = _remainder_setup(monkeypatch)
    request = portfolio_plans.ReplanRemainderRequest(securities_account_id="acct-1", dry_run=False)

    first = asyncio.run(
        portfolio_plans.replan_portfolio_plan_remainder("plan-user-1", request, current_user={"id": "user-1"})
    )
    assert first["data"]["inserted_count"] == 1
    assert len(finance["trade_signals"].docs) == 2  # original + topup
    assert len(collections["portfolio_plan_operation_logs"].docs) == 1

    # The freshly inserted top-up is now an active signal for 000001, so a second
    # run must not double-send.
    second = asyncio.run(
        portfolio_plans.replan_portfolio_plan_remainder("plan-user-1", request, current_user={"id": "user-1"})
    )
    assert second["data"]["inserted_count"] == 0
    assert second["data"]["actionable_count"] == 0
    remainder = second["data"]["remainder"][0]
    assert "live_signal_active" in remainder["reasons"]


def _patch_lineage_live_env(monkeypatch, cols, fin, data):
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: cols)
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: fin)
    monkeypatch.setattr(portfolio_plans, "get_data_db", lambda: data)
    monkeypatch.setattr(portfolio_plans, "_attach_signal_names", lambda _dc, sigs: sigs)


def test_list_live_portfolios_finds_lineage_with_live_signals(monkeypatch):
    cols = _collections()
    fin = _finance_collections()
    data = _data_collections()
    cols["portfolio_trade_plans"] = _Collection(
        [
            {"plan_id": "pla", "strategy_template_id": "stx", "user_id": "user-1", "base_date": "20250101", "status": "approved", "params_hash": "hx", "strategy_name": "L1"},
            {"plan_id": "plb", "strategy_template_id": "stx", "user_id": "user-1", "base_date": "20250125", "status": "approved", "params_hash": "hx", "strategy_name": "L1"},
            {"plan_id": "plz", "strategy_template_id": "sty", "user_id": "user-1", "base_date": "20250110", "status": "approved", "params_hash": "hy", "strategy_name": "NoLive"},
        ]
    )
    fin["trade_signals"].docs = [
        {
            "order_id": "oa",
            "user_id": "user-1",
            "mode": "live",
            "plan_id": "pla",
            "securities_account_id": "acct-1",
        },
    ]
    fin["trade_executions"].docs = [
        {
            "order_id": "oa",
            "user_id": "user-1",
            "symbol": "000001.SZ",
            "side": "BUY",
            "filled_size": 100,
            "filled_price": 10.0,
            "timestamp": 1.0,
        },
    ]
    _patch_lineage_live_env(monkeypatch, cols, fin, data)
    resp = asyncio.run(portfolio_plans.list_live_portfolios(current_user={"id": "user-1"}))
    ports = resp["data"]["portfolios"]
    assert len(ports) == 1
    assert ports[0]["strategy_template_id"] == "stx"
    assert ports[0]["params_hash"] == "hx"
    assert ports[0]["plan_count"] == 2
    assert ports[0]["latest_plan_id"] == "plb"


def test_lineage_live_executions_spans_two_plans(monkeypatch):
    cols = _collections()
    fin = _finance_collections()
    data = _data_collections()
    cols["portfolio_trade_plans"] = _Collection(
        [
            {"plan_id": "pla", "strategy_template_id": "stx", "user_id": "user-1", "base_date": "20250101", "status": "approved", "params_hash": "hx", "strategy_name": "L1"},
            {"plan_id": "plb", "strategy_template_id": "stx", "user_id": "user-1", "base_date": "20250125", "status": "approved", "params_hash": "hx", "strategy_name": "L1"},
        ]
    )
    fin["trade_signals"].docs = [
        {"order_id": "oa", "user_id": "user-1", "mode": "live", "plan_id": "pla", "symbol": "000001.SZ", "name": "Ping", "created_at": 1},
        {"order_id": "ob", "user_id": "user-1", "mode": "live", "plan_id": "plb", "symbol": "600000.SH", "name": "Pu", "created_at": 2},
    ]
    fin["trade_executions"].docs = [
        {"order_id": "oa", "user_id": "user-1", "symbol": "000001.SZ", "side": "BUY", "filled_size": 100, "filled_price": 10.0, "timestamp": 1.0},
        {"order_id": "ob", "user_id": "user-1", "symbol": "600000.SH", "side": "BUY", "filled_size": 200, "filled_price": 11.0, "timestamp": 2.0},
    ]
    _patch_lineage_live_env(monkeypatch, cols, fin, data)
    resp = asyncio.run(portfolio_plans.get_lineage_live_executions("pla", current_user={"id": "user-1"}))
    ex = resp["data"]["executions"]
    assert len(ex) == 2
    assert {ex[0]["source_plan_base_date"], ex[1]["source_plan_base_date"]} == {"20250101", "20250125"}


def test_lineage_live_positions_aggregates_lineage_executions(monkeypatch):
    cols = _collections()
    fin = _finance_collections()
    data = _data_collections()
    cols["portfolio_trade_plans"] = _Collection(
        [
            {"plan_id": "pla", "strategy_template_id": "stx", "user_id": "user-1", "base_date": "20250101", "status": "approved", "params_hash": "hx", "strategy_name": "L1"},
            {"plan_id": "plb", "strategy_template_id": "stx", "user_id": "user-1", "base_date": "20250125", "status": "approved", "params_hash": "hx", "strategy_name": "L1"},
        ]
    )
    fin["trade_signals"].docs = [
        {"order_id": "oa", "user_id": "user-1", "mode": "live", "plan_id": "pla", "symbol": "000001.SZ", "name": "Ping", "created_at": 1},
        {"order_id": "ob", "user_id": "user-1", "mode": "live", "plan_id": "plb", "symbol": "000001.SZ", "name": "Ping", "created_at": 2},
    ]
    fin["trade_executions"].docs = [
        {"order_id": "oa", "user_id": "user-1", "symbol": "000001.SZ", "side": "BUY", "filled_size": 100, "filled_price": 10.0, "timestamp": 1.0},
        {"order_id": "ob", "user_id": "user-1", "symbol": "000001.SZ", "side": "BUY", "filled_size": 100, "filled_price": 12.0, "timestamp": 2.0},
    ]
    _patch_lineage_live_env(monkeypatch, cols, fin, data)
    resp = asyncio.run(portfolio_plans.get_lineage_live_positions("pla", current_user={"id": "user-1"}))
    pos = resp["data"]["positions"]
    assert len(pos) == 1
    assert pos[0]["symbol"] == "000001.SZ"
    assert pos[0]["shares"] == 200
    assert pos[0]["avg_cost"] == 11.0


def test_lineage_live_positions_falls_back_when_realtime_is_stale(monkeypatch):
    cols = _collections()
    fin = _finance_collections()
    data = _data_collections()
    cols["portfolio_trade_plans"] = _Collection(
        [
            {"plan_id": "pla", "user_id": "user-1", "strategy_template_id": "stx", "params_hash": "h", "base_date": "20250101"},
        ]
    )
    fin["trade_signals"].docs = [
        {"order_id": "oa", "user_id": "user-1", "mode": "live", "plan_id": "pla", "symbol": "000001.SZ"},
    ]
    fin["trade_executions"].docs = [
        {"order_id": "oa", "user_id": "user-1", "symbol": "000001.SZ", "side": "BUY", "filled_size": 100, "filled_price": 10.0, "timestamp": 1.0},
    ]
    data["volume_price"].docs = [
        {"symbol": "000001.SZ", "trade_date": "20250110", "close": 12.0},
    ]
    data["volume_price_realtime_latest"].docs = [
        {"symbol": "000001.SZ", "trade_date": "20250109", "close": 99.0},
    ]

    _patch_lineage_live_env(monkeypatch, cols, fin, data)
    resp = asyncio.run(portfolio_plans.get_lineage_live_positions("pla", current_user={"id": "user-1"}))
    pos = resp["data"]["positions"][0]

    assert pos["last_price"] == 12.0
    assert pos["last_price_source"] == "market"
    assert pos["unrealized_pnl_pct"] == 0.2


def test_lineage_live_equity_dedupes_date_by_updated_at(monkeypatch):
    cols = _collections()
    fin = _finance_collections()
    data = _data_collections()
    cols["portfolio_trade_plans"] = _Collection(
        [
            {"plan_id": "pla", "strategy_template_id": "stx", "user_id": "user-1", "base_date": "20250101", "status": "approved", "params_hash": "hx"},
            {"plan_id": "plb", "strategy_template_id": "stx", "user_id": "user-1", "base_date": "20250125", "status": "approved", "params_hash": "hx"},
        ]
    )
    cols["portfolio_live_equity"] = _Collection(
        [
            {"user_id": "user-1", "plan_id": "pla", "date": "20250130", "equity": 1_000_000, "updated_at": "2025-01-30T10:00:00"},
            {"user_id": "user-1", "plan_id": "plb", "date": "20250130", "equity": 1_010_000, "updated_at": "2025-01-30T15:00:00"},
        ]
    )
    _patch_lineage_live_env(monkeypatch, cols, fin, data)
    resp = asyncio.run(
        portfolio_plans.get_lineage_live_equity("pla", start_date=None, end_date=None, current_user={"id": "user-1"})
    )
    rows = resp["data"]["rows"]
    assert len(rows) == 1
    assert rows[0]["plan_id"] == "plb"
    assert rows[0]["equity"] == 1_010_000
    assert "caveat" in resp["data"]
    # Continuous one-book equity is always present; with no live executions it
    # falls back to initial_capital rather than resetting per plan.
    book = resp["data"]["current_book_equity"]
    assert book["equity"] == book["initial_capital"]
    assert book["execution_count"] == 0


def test_lineage_live_equity_prefers_account_daily_equity(monkeypatch):
    cols = _collections()
    fin = _finance_collections()
    data = _data_collections()
    cols["portfolio_trade_plans"] = _Collection(
        [
            {"plan_id": "pla", "strategy_template_id": "stx", "user_id": "user-1", "base_date": "20250101", "status": "approved", "params_hash": "hx"},
            {"plan_id": "plb", "strategy_template_id": "stx", "user_id": "user-1", "base_date": "20250125", "status": "approved", "params_hash": "hx"},
        ]
    )
    cols["portfolio_live_equity"] = _Collection(
        [
            {"user_id": "user-1", "plan_id": "pla", "date": "20250130", "equity": 1_000_000, "updated_at": "2025-01-30T10:00:00"},
        ]
    )
    fin["trade_signals"].docs = [
        {"user_id": "user-1", "mode": "live", "plan_id": "pla", "securities_account_id": "acct-1"},
        {"user_id": "user-1", "mode": "live", "plan_id": "plb", "securities_account_id": "acct-1"},
    ]
    fin["trader_account_equity"].docs = [
        {"user_id": "user-1", "securities_account_id": "acct-1", "date": "20250129", "total_asset": 1_010_000, "cash": 100_000, "market_value": 910_000},
        {"user_id": "user-1", "securities_account_id": "acct-1", "date": "20250130", "total_asset": 1_020_000, "cash": 90_000, "market_value": 930_000},
    ]

    _patch_lineage_live_env(monkeypatch, cols, fin, data)
    resp = asyncio.run(
        portfolio_plans.get_lineage_live_equity("pla", start_date=None, end_date=None, current_user={"id": "user-1"})
    )

    assert resp["data"]["row_source"] == "trader_account_equity"
    assert resp["data"]["securities_account_ids"] == ["acct-1"]
    assert [row["date"] for row in resp["data"]["rows"]] == ["20250129", "20250130"]
    assert [row["equity"] for row in resp["data"]["rows"]] == [1_010_000, 1_020_000]
    assert resp["data"]["rows"][0]["account_ids"] == ["acct-1"]


def test_list_portfolios_includes_live_and_paper_lineages(monkeypatch):
    cols = _collections()
    fin = _finance_collections()
    data = _data_collections()
    cols["portfolio_trade_plans"] = _Collection(
        [
            {
                "plan_id": "live-a",
                "strategy_template_id": "st-live",
                "user_id": "user-1",
                "base_date": "20250120",
                "status": "executed_paper",
                "params_hash": "hx-live",
                "strategy_name": "LiveCombo",
            },
            {
                "plan_id": "live-b",
                "strategy_template_id": "st-live",
                "user_id": "user-1",
                "base_date": "20250125",
                "status": "executed_paper",
                "params_hash": "hx-live",
                "strategy_name": "LiveCombo",
            },
            {
                "plan_id": "paper-a",
                "strategy_template_id": "st-paper",
                "user_id": "user-1",
                "base_date": "20250118",
                "status": "executed_paper",
                "params_hash": "hx-paper",
                "strategy_name": "PaperOnly",
            },
            {
                "plan_id": "paper-b",
                "strategy_template_id": "st-paper",
                "user_id": "user-1",
                "base_date": "20250122",
                "status": "partially_executed",
                "params_hash": "hx-paper",
                "strategy_name": "PaperOnly",
            },
            {
                "plan_id": "inactive",
                "strategy_template_id": "st-inactive",
                "user_id": "user-1",
                "base_date": "20250110",
                "status": "generated",
                "params_hash": "hx-inactive",
                "strategy_name": "Inactive",
            },
        ]
    )
    fin["trade_signals"].docs = [
        {
            "order_id": "live-order",
            "user_id": "user-1",
            "mode": "live",
            "plan_id": "live-a",
            "securities_account_id": "acct-1",
        },
    ]
    fin["trade_executions"].docs = [
        {
            "order_id": "live-order",
            "user_id": "user-1",
            "symbol": "000001.SZ",
            "side": "BUY",
            "filled_size": 100,
            "filled_price": 10.0,
            "timestamp": 1.0,
        },
    ]
    _patch_lineage_live_env(monkeypatch, cols, fin, data)
    resp = asyncio.run(portfolio_plans.list_portfolios(current_user={"id": "user-1"}))
    ports = resp["data"]["portfolios"]
    assert len(ports) == 2
    live = next(row for row in ports if row["params_hash"] == "hx-live")
    paper = next(row for row in ports if row["params_hash"] == "hx-paper")
    assert live["funding_mode"] == "live"
    assert live["latest_plan_id"] == "live-b"
    assert paper["funding_mode"] == "paper"
    assert paper["latest_plan_id"] == "paper-b"


def test_list_portfolios_treats_unfilled_live_signals_as_paper(monkeypatch):
    cols = _collections()
    fin = _finance_collections()
    data = _data_collections()
    cols["portfolio_trade_plans"] = _Collection(
        [
            {
                "plan_id": "signal-only",
                "strategy_template_id": "st-live",
                "user_id": "user-1",
                "base_date": "20250120",
                "status": "executed_paper",
                "params_hash": "hx-signal-only",
                "strategy_name": "SignalOnly",
            },
        ]
    )
    fin["trade_signals"].docs = [
        {
            "order_id": "pending-order",
            "user_id": "user-1",
            "mode": "live",
            "plan_id": "signal-only",
            "securities_account_id": "acct-1",
            "status": "cancelled",
        },
    ]
    _patch_lineage_live_env(monkeypatch, cols, fin, data)
    resp = asyncio.run(portfolio_plans.list_portfolios(current_user={"id": "user-1"}))
    ports = resp["data"]["portfolios"]
    assert len(ports) == 1
    assert ports[0]["funding_mode"] == "paper"
    assert ports[0]["securities_account_id"] == ""


def test_lineage_timeline_monitor_drift_and_action_required(monkeypatch):
    cols = _collections()
    fin = _finance_collections()
    data = _data_collections()
    data["volume_price"].docs = [
        {"trade_date": f"202501{d:02d}"} for d in range(1, 32)
    ]
    cols["portfolio_trade_plans"] = _Collection(
        [
            {
                "plan_id": "reb-1",
                "strategy_template_id": "stx",
                "user_id": "user-1",
                "base_date": "20250102",
                "status": "executed_paper",
                "params_hash": "hx",
                "strategy_name": "Combo",
                "plan_type": "rebalance",
                "is_rebalance_day": True,
                "previous_rebalance_date": None,
                "params_snapshot": {"rebalance_days": 20},
                "summary": {"equity": 1_000_000, "buy_count": 2, "estimated_turnover": 0.12},
                "executable": True,
            },
            {
                "plan_id": "mon-1",
                "strategy_template_id": "stx",
                "user_id": "user-1",
                "base_date": "20250110",
                "status": "generated",
                "params_hash": "hx",
                "strategy_name": "Combo",
                "plan_type": "monitor",
                "is_rebalance_day": False,
                "previous_rebalance_date": "20250102",
                "previous_rebalance_plan_id": "reb-1",
                "params_snapshot": {"rebalance_days": 20},
                "summary": {
                    "equity": 1_000_000,
                    "buy_count": 0,
                    "sell_count": 0,
                    "estimated_buy_amount": 0,
                    "estimated_turnover": 0,
                },
                "executable": False,
                "non_executable_reason": "monitor_no_trade",
            },
            {
                "plan_id": "reb-2",
                "strategy_template_id": "stx",
                "user_id": "user-1",
                "base_date": "20250125",
                "status": "needs_review",
                "params_hash": "hx",
                "strategy_name": "Combo",
                "plan_type": "rebalance",
                "is_rebalance_day": True,
                "previous_rebalance_date": "20250102",
                "previous_rebalance_plan_id": "reb-1",
                "params_snapshot": {"rebalance_days": 20},
                "summary": {"equity": 1_050_000, "buy_count": 1, "estimated_turnover": 0.08},
                "executable": True,
            },
        ]
    )
    cols["portfolio_trade_plan_items"].docs = [
        {
            "plan_id": "mon-1",
            "user_id": "user-1",
            "symbol": "000001.SZ",
            "drift_delta_shares": 100,
            "estimated_price": 10.0,
            "current_shares": 0,
            "target_shares": 100,
        },
        {
            "plan_id": "mon-1",
            "user_id": "user-1",
            "symbol": "600000.SH",
            "drift_delta_shares": -200,
            "estimated_price": 12.0,
            "current_shares": 200,
            "target_shares": 0,
        },
        {
            "plan_id": "reb-2",
            "user_id": "user-1",
            "symbol": "000001.SZ",
            "drift_delta_shares": 50,
            "estimated_price": 10.0,
            "current_shares": 100,
            "target_shares": 150,
        },
    ]
    _patch_lineage_live_env(monkeypatch, cols, fin, data)

    monitor_resp = asyncio.run(
        portfolio_plans.get_portfolio_plan_lineage_timeline(
            "mon-1", limit=60, offset=0, current_user={"id": "user-1"}
        )
    )
    monitor_data = monitor_resp["data"]
    assert monitor_data["action_required"] is True
    assert monitor_data["today_state"] == "调仓日：等待审核"
    drift = monitor_data["latest_drift_summary"]
    assert drift["plan_id"] == "reb-2"
    assert drift["buy_count"] == 1
    assert drift["estimated_buy_amount"] == 500.0
    monitor_node = next(row for row in monitor_data["timeline"] if row["plan_id"] == "mon-1")
    assert monitor_node["today_state"] == "观察日：不交易"
    assert monitor_node["action_required"] is False
    assert monitor_node["drift_brief"]["buy_count"] == 1
    assert monitor_node["drift_brief"]["sell_count"] == 1
    assert monitor_data["current_cycle"]["latest_rebalance_plan_id"] == "reb-2"
    assert monitor_data["current_cycle"]["latest_monitor_plan_id"] == "mon-1"

    review_resp = asyncio.run(
        portfolio_plans.get_portfolio_plan_lineage_timeline(
            "reb-2", limit=60, offset=0, current_user={"id": "user-1"}
        )
    )
    review_data = review_resp["data"]
    assert review_data["action_required"] is True
    assert review_data["today_state"] == "调仓日：等待审核"
    assert len(review_data["timeline"]) == 3
    assert review_data["timeline"][0]["plan_id"] == "reb-2"


def test_lineage_timeline_monitor_can_trade_label(monkeypatch):
    cols = _collections()
    fin = _finance_collections()
    data = _data_collections()
    cols["portfolio_trade_plans"] = _Collection(
        [
            {
                "plan_id": "mon-trade",
                "strategy_template_id": "stx",
                "user_id": "user-1",
                "base_date": "20250110",
                "status": "needs_review",
                "params_hash": "hx",
                "strategy_name": "Combo",
                "plan_type": "monitor",
                "is_rebalance_day": False,
                "monitor_can_trade": True,
                "params_snapshot": {"rebalance_days": 20, "monitor_can_trade": True},
                "summary": {"equity": 1_000_000},
                "executable": True,
            },
        ]
    )
    _patch_lineage_live_env(monkeypatch, cols, fin, data)
    resp = asyncio.run(
        portfolio_plans.get_portfolio_plan_lineage_timeline(
            "mon-trade", limit=60, offset=0, current_user={"id": "user-1"}
        )
    )
    assert resp["data"]["today_state"] == "观察日（已开启日内再平衡）：可交易，等待审核"
    assert resp["data"]["action_required"] is True


def test_lineage_timeline_drift_summary_uses_items_when_monitor_summary_zero(monkeypatch):
    cols = _collections()
    fin = _finance_collections()
    data = _data_collections()
    cols["portfolio_trade_plans"] = _Collection(
        [
            {
                "plan_id": "mon-latest",
                "strategy_template_id": "stx",
                "user_id": "user-1",
                "base_date": "20250110",
                "status": "generated",
                "params_hash": "hx",
                "strategy_name": "Combo",
                "plan_type": "monitor",
                "is_rebalance_day": False,
                "params_snapshot": {"rebalance_days": 20},
                "summary": {
                    "equity": 1_000_000,
                    "buy_count": 0,
                    "sell_count": 0,
                    "estimated_buy_amount": 0,
                    "estimated_turnover": 0,
                },
                "executable": False,
                "non_executable_reason": "monitor_no_trade",
            },
        ]
    )
    cols["portfolio_trade_plan_items"].docs = [
        {
            "plan_id": "mon-latest",
            "user_id": "user-1",
            "symbol": "000001.SZ",
            "drift_delta_shares": 100,
            "estimated_price": 10.0,
            "current_shares": 0,
            "target_shares": 100,
        },
    ]
    _patch_lineage_live_env(monkeypatch, cols, fin, data)
    resp = asyncio.run(
        portfolio_plans.get_portfolio_plan_lineage_timeline(
            "mon-latest", limit=60, offset=0, current_user={"id": "user-1"}
        )
    )
    drift = resp["data"]["latest_drift_summary"]
    assert drift["buy_count"] == 1
    assert drift["estimated_buy_amount"] == 1000.0
    assert drift["estimated_turnover"] == pytest.approx(0.001)
