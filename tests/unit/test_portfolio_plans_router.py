import asyncio

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
                if "$gte" in expected and not (value >= expected["$gte"]):
                    return False
                if "$lte" in expected and not (value <= expected["$lte"]):
                    return False
                continue
            if isinstance(value, list):
                if expected not in value:
                    return False
                continue
            if value != expected:
                return False
        return True


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
        "trader_accounts": _Collection([{"user_id": "user-1", "securities_account_id": "acct-1", "available_cash": 100000, "synced_at": 10}]),
        "trader_positions": _Collection([{"user_id": "user-1", "securities_account_id": "acct-1", "symbol": "600000", "can_use_volume": 500}]),
    }


def _data_collections():
    return {
        "stock_info": _Collection([{"symbol": "000001", "name": "平安银行"}, {"symbol": "600000", "name": "浦发银行"}]),
        "volume_price": _Collection(),
        "volume_price_realtime_latest": _Collection([{"symbol": "000001", "close": 10.0}, {"symbol": "600000", "close": 12.0}]),
    }


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
    sell_signal = response["data"]["signals"][1]
    assert sell_signal["execution_phase"] == "sell"
    assert sell_signal["execution_priority"] == -9998
    assert sell_signal["price_floor"] == 11.88
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
    finance["trade_executions"].docs.extend(
        [
            {
                "user_id": "user-1",
                "strategy_template_id": "s1",
                "securities_account_id": "acct-1",
                "order_id": "plan-order-1",
                "symbol": "000001",
                "action": "buy",
                "filled_size": 100,
                "mode": "live",
            },
            {
                "user_id": "user-1",
                "strategy_template_id": "other-strategy",
                "securities_account_id": "acct-1",
                "order_id": "other-strategy-order",
                "symbol": "000001",
                "action": "buy",
                "filled_size": 200,
                "mode": "live",
            },
            {
                "user_id": "user-1",
                "securities_account_id": "acct-1",
                "order_id": "manual-order",
                "symbol": "000001",
                "action": "buy",
                "filled_size": 300,
                "mode": "live",
            },
        ]
    )
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


def test_paper_executed_plan_suppresses_live_context(monkeypatch):
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

    assert response["data"]["execution_mode"] == "paper"
    context = response["data"]["live_execution_context"]
    assert context["has_live_signals"] is False
    assert context["signal_count"] == 0


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
