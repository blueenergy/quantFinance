import asyncio
from datetime import datetime

import pytest

from routers import portfolio_plans


def test_allowed_universe_indices_include_csi2000():
    assert "csi2000" in portfolio_plans.ALLOWED_UNIVERSE_INDICES


def test_normalize_live_target_shares_non_star_uses_legacy_lot_rounding():
    # No symbol -> legacy behavior (round-lot delta), unchanged.
    assert portfolio_plans._normalize_live_target_shares(0, 300, 100) == (300, [])
    assert portfolio_plans._normalize_live_target_shares(400, 450, 100) == (
        400,
        ["below_lot_size"],
    )


def test_normalize_live_target_shares_star_enforces_200_min_order():
    # STAR 100-share buy is bumped to 200 when cash is unknown/sufficient.
    assert portfolio_plans._normalize_live_target_shares(
        0, 100, 100, symbol="688627.SH", price=560.0
    ) == (200, [])
    # Skipped when the cash budget cannot cover a 200-share order.
    assert portfolio_plans._normalize_live_target_shares(
        0, 100, 100, symbol="688627.SH", price=560.0, available_cash=10_000
    ) == (0, ["below_star_min_order"])
    # A tiny sub-100 delta is skipped rather than inflated to a 200-share order.
    assert portfolio_plans._normalize_live_target_shares(
        0, 50, 100, symbol="688627.SH", price=560.0
    ) == (0, ["below_star_min_order"])
    # STAR sell leaving a sub-200 odd lot liquidates fully.
    assert portfolio_plans._normalize_live_target_shares(
        250, 100, 100, symbol="688627.SH", price=560.0
    ) == (0, [])


def test_live_publish_risk_report_batches_position_queries():
    trader_positions = _CountingCollection(
        [
            {
                "user_id": "user-1",
                "securities_account_id": "acct-1",
                "symbol": "000001.SZ",
                "qty": 100,
                "can_use_volume": 100,
                "market_value": 1000,
                "synced_at": 2,
            },
            {
                "user_id": "user-1",
                "securities_account_id": "acct-1",
                "symbol": "000002.SZ",
                "qty": 100,
                "can_use_volume": 50,
                "market_value": 1000,
                "synced_at": 2,
            },
        ]
    )
    finance = {
        "trader_accounts": _Collection(
            [
                {
                    "user_id": "user-1",
                    "securities_account_id": "acct-1",
                    "available_cash": 100000,
                    "total_assets": 100000,
                    "synced_at": 2,
                }
            ]
        ),
        "trade_signals": _Collection(),
        "trader_positions": trader_positions,
    }
    data = {
        "stock_info": _Collection(),
        "volume_price_realtime_latest": _Collection(),
        "volume_price": _Collection(),
    }

    report = portfolio_plans._build_live_publish_risk_report(
        finance_collections=finance,
        data_collections=data,
        account={
            "_id": "acct-1",
            "broker": "miniQMT",
            "account_id": "ACC",
            "live_trading_enabled": True,
            "risk_limits": {"max_position_pct": 10},
        },
        signals=[
            {
                "order_id": "buy-1",
                "symbol": "000001.SZ",
                "action": "buy",
                "size": 100,
                "price": 10,
                "securities_account_id": "acct-1",
            },
            {
                "order_id": "sell-1",
                "symbol": "000002.SZ",
                "action": "sell",
                "size": 100,
                "price": 10,
                "securities_account_id": "acct-1",
            },
        ],
        user_id="user-1",
    )

    assert trader_positions.find_count == 1
    assert report["blocked_count"] == 1
    assert report["items"][1]["blockers"] == ["insufficient_available_position"]


def test_live_publish_risk_report_blocks_sell_above_lineage_position(monkeypatch):
    monkeypatch.setenv("LIVE_SELL_SIZING_BASIS", "lineage")

    finance = {
        "trader_accounts": _Collection(
            [
                {
                    "user_id": "user-1",
                    "securities_account_id": "acct-1",
                    "available_cash": 100000,
                    "total_assets": 100000,
                    "synced_at": 2,
                }
            ]
        ),
        "trade_signals": _Collection(),
        "trader_positions": _Collection(
            [
                {
                    "user_id": "user-1",
                    "securities_account_id": "acct-1",
                    "symbol": "000002.SZ",
                    "qty": 100,
                    "can_use_volume": 100,
                    "synced_at": 2,
                }
            ]
        ),
    }
    trading = {
        "portfolio_live_snapshots": _Collection(
            [
                {
                    "user_id": "user-1",
                    "strategy_template_id": "s1",
                    "params_hash": "hash-1",
                    "positions": [{"symbol": "000002.SZ", "shares": 50}],
                }
            ]
        )
    }
    data = {
        "stock_info": _Collection(),
        "volume_price_realtime_latest": _Collection(),
        "volume_price": _Collection(),
    }

    report = portfolio_plans._build_live_publish_risk_report(
        finance_collections=finance,
        data_collections=data,
        account={
            "_id": "acct-1",
            "broker": "miniQMT",
            "account_id": "ACC",
            "live_trading_enabled": True,
        },
        signals=[
            {
                "order_id": "sell-1",
                "symbol": "000002.SZ",
                "action": "sell",
                "size": 100,
                "price": 10,
                "securities_account_id": "acct-1",
            }
        ],
        user_id="user-1",
        trading_collections=trading,
        plan={"strategy_template_id": "s1", "params_hash": "hash-1"},
    )

    assert report["blocked_count"] == 1
    assert report["items"][0]["blockers"] == ["insufficient_lineage_position"]


def test_live_publish_risk_report_falls_back_when_lineage_snapshot_missing(monkeypatch):
    monkeypatch.setenv("LIVE_SELL_SIZING_BASIS", "lineage")

    finance = {
        "trader_accounts": _Collection(
            [
                {
                    "user_id": "user-1",
                    "securities_account_id": "acct-1",
                    "available_cash": 100000,
                    "total_assets": 100000,
                    "synced_at": 2,
                }
            ]
        ),
        "trade_signals": _Collection(),
        "trader_positions": _Collection(
            [
                {
                    "user_id": "user-1",
                    "securities_account_id": "acct-1",
                    "symbol": "000002.SZ",
                    "qty": 100,
                    "can_use_volume": 100,
                    "synced_at": 2,
                }
            ]
        ),
    }
    data = {
        "stock_info": _Collection(),
        "volume_price_realtime_latest": _Collection(),
        "volume_price": _Collection(),
    }

    report = portfolio_plans._build_live_publish_risk_report(
        finance_collections=finance,
        data_collections=data,
        account={
            "_id": "acct-1",
            "broker": "miniQMT",
            "account_id": "ACC",
            "live_trading_enabled": True,
        },
        signals=[
            {
                "order_id": "sell-1",
                "symbol": "000002.SZ",
                "action": "sell",
                "size": 100,
                "price": 10,
                "securities_account_id": "acct-1",
            }
        ],
        user_id="user-1",
        trading_collections={"portfolio_live_snapshots": _Collection()},
        plan={"strategy_template_id": "s1", "params_hash": "hash-1"},
    )

    assert report["blocked_count"] == 0
    assert report["items"][0]["blockers"] == []


def test_load_volatility_features_batches_queries_and_keeps_recent_window(monkeypatch):
    from stock_data_access.indicators import volatility

    volume_price = _CountingCollection(
        [
            {
                "symbol": "000001.SZ",
                "trade_date": f"2024{i:04d}",
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10,
            }
            for i in range(1, 323)
        ]
        + [
            {
                "symbol": "000002.SZ",
                "trade_date": "20240101",
                "open": 20,
                "high": 21,
                "low": 19,
                "close": 20,
            },
            {
                "symbol": "000002.SZ",
                "trade_date": "20260101",
                "open": 20,
                "high": 21,
                "low": 19,
                "close": 20,
            },
        ]
    )
    seen = {}

    def fake_compute(rows):
        seen[rows[0]["symbol"]] = [row["trade_date"] for row in rows]
        return {"row_count": len(rows), "first_trade_date": rows[0]["trade_date"]}

    monkeypatch.setattr(volatility, "compute_volatility_features", fake_compute)

    features = portfolio_plans._load_volatility_features_by_symbol(
        {"volume_price": volume_price},
        ["000001.SZ", "000002.SZ"],
        base_date="2025-12-31",
    )

    assert volume_price.find_count == 1
    assert features["000001.SZ"] == {"row_count": 320, "first_trade_date": "20240003"}
    assert seen["000001.SZ"][-1] == "20240322"
    assert features["000002.SZ"] == {"row_count": 1, "first_trade_date": "20240101"}


def _sort_key(value):
    """Type-stable sort key so the fake can order mixed/missing values without
    raising on cross-type comparisons (mirrors a real Mongo multi-key sort)."""
    if value is None:
        return (0, 0.0)
    if isinstance(value, bool):
        return (1, float(value))
    if isinstance(value, (int, float)):
        return (1, float(value))
    return (2, str(value))


class _Cursor(list):
    def sort(self, field, direction=None):
        keys = field if isinstance(field, list) else [(field, direction)]
        rows = list(self)
        # Apply sort keys from least to most significant. Python's sort is
        # stable, so this reproduces a real multi-key sort (e.g. as_of_date desc
        # then updated_at desc to break same-day ties).
        for sort_field, sort_dir in reversed(keys):
            rows = sorted(
                rows,
                key=lambda row, f=sort_field: _sort_key(row.get(f)),
                reverse=sort_dir == -1,
            )
        return _Cursor(rows)

    def limit(self, n):
        return _Cursor(list(self)[:n])


class _UpdateResult:
    def __init__(self, modified_count=0):
        self.modified_count = modified_count


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

    def update_one(self, query, update, upsert=False):
        for doc in self.docs:
            if self._matches(doc, query):
                doc.update(update.get("$set", {}))
                for key in update.get("$unset", {}):
                    doc.pop(key, None)
                return _UpdateResult(1)
        if upsert:
            new_doc = dict(query)
            new_doc.update(update.get("$setOnInsert", {}))
            new_doc.update(update.get("$set", {}))
            self.docs.append(new_doc)
            return _UpdateResult(1)
        return _UpdateResult(0)

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

    def distinct(self, key, query=None):
        seen = []
        for doc in self.find(query or {}):
            value = self._get_value(doc, key)
            if value not in seen:
                seen.append(value)
        return seen

    def _get_value(self, doc, key):
        value = doc
        for part in str(key).split("."):
            if not isinstance(value, dict) or part not in value:
                return None
            value = value.get(part)
        return value

    def _matches(self, doc, query):
        for key, expected in query.items():
            if key == "$or":
                if not any(self._matches(doc, branch) for branch in expected):
                    return False
                continue
            value = self._get_value(doc, key)
            if isinstance(expected, dict):
                if "$in" in expected and value not in expected["$in"]:
                    return False
                if "$nin" in expected and value in expected["$nin"]:
                    return False
                if "$ne" in expected and value == expected["$ne"]:
                    return False
                if "$gte" in expected and not (value is not None and value >= expected["$gte"]):
                    return False
                if "$lte" in expected and not (value is not None and value <= expected["$lte"]):
                    return False
                if "$gt" in expected and not (value is not None and value > expected["$gt"]):
                    return False
                if "$lt" in expected and not (value is not None and value < expected["$lt"]):
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


class _CountingCollection(_Collection):
    def __init__(self, docs=None):
        super().__init__(docs)
        self.find_count = 0

    def find(self, query=None, projection=None):
        self.find_count += 1
        return super().find(query, projection)


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
        "portfolio_lineage_controls": _Collection(),
        "portfolio_live_snapshots": _Collection(),
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
        "trader_accounts": _Collection([{"user_id": "user-1", "securities_account_id": "acct-1", "available_cash": 100000, "total_asset": 1000000, "synced_at": 10}]),
        "trader_positions": _Collection([{"user_id": "user-1", "securities_account_id": "acct-1", "symbol": "600000", "can_use_volume": 500}]),
    }


def _data_collections():
    return {
        "stock_info": _Collection([{"symbol": "000001", "name": "平安银行"}, {"symbol": "600000", "name": "浦发银行"}]),
        "volume_price": _Collection(),
        "volume_price_realtime_latest": _Collection([{"symbol": "000001", "close": 10.0}, {"symbol": "600000", "close": 12.0}]),
    }


def _analyzer_collections():
    return {
        "portfolio_risk_llm": _Collection(),
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


def test_enqueue_portfolio_llm_risk_fans_out_by_industry_and_reuses_running_run(monkeypatch):
    collections = _collections()
    plan = collections["portfolio_trade_plans"].docs[0]
    collections["portfolio_trade_plan_items"].insert_many(
        [
            {
                "plan_id": plan["plan_id"],
                "user_id": "user-1",
                "symbol": "000001",
                "name": "平安银行",
                "industry": "银行",
                "action": "buy",
                "rank": 1,
                "target_weight": 0.06,
                "score_value": 88.0,
                "ai_risk": {"severity": "medium", "reasons": ["近期涨幅过大"]},
            },
            {
                "plan_id": plan["plan_id"],
                "user_id": "user-1",
                "symbol": "600000",
                "name": "浦发银行",
                "industry": "银行",
                "action": "hold",
                "rank": 2,
                "target_weight": 0.04,
                "score_value": 80.0,
                "ai_risk": {"severity": "none"},
            },
            {
                "plan_id": plan["plan_id"],
                "user_id": "user-1",
                "symbol": "300001",
                "name": "测试科技",
                "industry": "电子/半导体",
                "action": "buy",
                "rank": 3,
                "target_weight": 0.03,
                "score_value": 77.0,
                "ai_risk": {"severity": "low"},
            },
        ]
    )
    analysis_tasks = _Collection()
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)
    monkeypatch.setattr(portfolio_plans, "get_data_db", _data_collections)
    monkeypatch.setattr(portfolio_plans, "finance_db", {"analysis_tasks": analysis_tasks})

    response = asyncio.run(
        portfolio_plans.enqueue_portfolio_plan_llm_risk(
            plan["plan_id"],
            portfolio_plans.PortfolioLlmRiskReviewRequest(max_symbols_per_call=2),
            current_user={"id": "user-1"},
        )
    )

    data = response["data"]
    assert data["reused"] is False
    assert data["summary"]["total"] == 2
    assert {task["params"]["industry"] for task in analysis_tasks.docs} == {"银行", "电子"}
    assert all(task["priority"] == portfolio_plans.PORTFOLIO_RISK_LLM_PRIORITY for task in analysis_tasks.docs)
    assert all(task["type"] == portfolio_plans.PORTFOLIO_RISK_LLM_TASK_TYPE for task in analysis_tasks.docs)

    reused = asyncio.run(
        portfolio_plans.enqueue_portfolio_plan_llm_risk(
            plan["plan_id"],
            portfolio_plans.PortfolioLlmRiskReviewRequest(max_symbols_per_call=2),
            current_user={"id": "user-1"},
        )
    )
    assert reused["data"]["reused"] is True
    assert reused["data"]["run_id"] == data["run_id"]
    assert len(analysis_tasks.docs) == 2


def test_enqueue_portfolio_llm_risk_supports_bench_scope(monkeypatch):
    collections = _collections()
    data_collections = _data_collections()
    stock_scores = _Collection(
        [
            {"symbol": "000001.SZ", "name": "A", "industry": "Bank", "score_date": "20250110", "index_codes": "csi1000", "growth_score": 90, "cycle_score": 80},
            {"symbol": "000002.SZ", "name": "B", "industry": "Bank", "score_date": "20250110", "index_codes": "csi1000", "growth_score": 85, "cycle_score": 75},
            {"symbol": "000003.SZ", "name": "C", "industry": "Tech", "score_date": "20250110", "index_codes": "csi1000", "growth_score": 80, "cycle_score": 70},
        ]
    )
    plan = collections["portfolio_trade_plans"].docs[0]
    plan.update(
        {
            "score_date": "20250110",
            "params_snapshot": {
                "universe_index": "csi1000",
                "growth_weight": 0.3,
                "cycle_weight": 0.7,
                "top_n": 2,
            },
        }
    )
    analysis_tasks = _Collection()
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: collections)
    monkeypatch.setattr(portfolio_plans, "get_data_db", lambda: data_collections)
    monkeypatch.setattr(
        portfolio_plans,
        "_current_lineage_holdings",
        lambda *args, **kwargs: [
            {"symbol": "000001.SZ", "name": "A", "shares": 1000, "last_price": 10.0, "market_value": 10000.0},
            {"symbol": "000002.SZ", "name": "B", "shares": 800, "last_price": 11.0, "market_value": 8800.0},
        ],
    )
    monkeypatch.setattr(
        portfolio_plans,
        "finance_db",
        {"stock_scores": stock_scores, "analysis_tasks": analysis_tasks},
    )

    response = asyncio.run(
        portfolio_plans.enqueue_portfolio_plan_llm_risk(
            plan["plan_id"],
            portfolio_plans.PortfolioLlmRiskReviewRequest(target_scope="bench"),
            current_user={"id": "user-1"},
        )
    )

    data = response["data"]
    assert data["target_scope"] == "bench"
    assert data["summary"]["total"] == 1
    task = analysis_tasks.docs[0]
    assert task["params"]["target_scope"] == "bench"
    assert task["params"]["items"][0]["symbol"] == "000003.SZ"
    assert task["params"]["items"][0]["action"] == "bench"


def test_plan_detail_enriches_latest_completed_llm_risk(monkeypatch):
    collections = _collections()
    plan = collections["portfolio_trade_plans"].docs[0]
    collections["portfolio_trade_plan_items"].insert_one(
        {
            "plan_id": plan["plan_id"],
            "user_id": "user-1",
            "symbol": "000001",
            "name": "平安银行",
            "industry": "银行",
            "action": "buy",
            "rank": 1,
            "ai_risk": {"severity": "medium", "reasons": ["近期涨幅过大"]},
        }
    )
    finance_collections = _finance_collections()
    data_collections = _data_collections()
    analyzer_collections = _analyzer_collections()
    analyzer_collections["portfolio_risk_llm"].insert_many(
        [
            {
                "plan_id": plan["plan_id"],
                "run_id": "run-old",
                "scope": "symbol",
                "symbol": "000001",
                "industry": "银行",
                "severity": "low",
                "summary": "旧结果",
                "findings": [],
                "engine_version": "llm-v1",
                "model": "old",
                "analyzed_at": "2026-06-28T10:00:00",
            },
            {
                "plan_id": plan["plan_id"],
                "run_id": "run-new",
                "scope": "symbol",
                "symbol": "000001",
                "industry": "银行",
                "severity": "high",
                "summary": "监管问询风险",
                "findings": [{"title": "收到问询", "severity": "high"}],
                "engine_version": "llm-v1",
                "model": "new",
                "analyzed_at": "2026-06-29T10:00:00",
            },
            {
                "plan_id": plan["plan_id"],
                "run_id": "run-new",
                "scope": "industry",
                "industry": "银行",
                "severity": "medium",
                "summary": "行业风险偏中性",
                "findings": [],
                "engine_version": "llm-v1",
                "model": "new",
                "analyzed_at": "2026-06-29T10:00:00",
            },
        ]
    )
    analysis_tasks = _Collection(
        [
            {
                "type": portfolio_plans.PORTFOLIO_RISK_LLM_TASK_TYPE,
                "user_id": "user-1",
                "status": "completed",
                "created_at": datetime(2026, 6, 28),
                "params": {"plan_id": plan["plan_id"], "run_id": "run-old"},
            },
            {
                "type": portfolio_plans.PORTFOLIO_RISK_LLM_TASK_TYPE,
                "user_id": "user-1",
                "status": "completed",
                "created_at": datetime(2026, 6, 29),
                "params": {"plan_id": plan["plan_id"], "run_id": "run-new"},
            },
            {
                "type": portfolio_plans.PORTFOLIO_RISK_LLM_TASK_TYPE,
                "user_id": "user-1",
                "status": "failed",
                "created_at": datetime(2026, 6, 29),
                "params": {"plan_id": plan["plan_id"], "run_id": "run-new"},
            },
        ]
    )
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: finance_collections)
    monkeypatch.setattr(portfolio_plans, "get_data_db", lambda: data_collections)
    monkeypatch.setattr(portfolio_plans, "get_analyzer_db", lambda: analyzer_collections)
    monkeypatch.setattr(portfolio_plans, "finance_db", {"analysis_tasks": analysis_tasks})

    response = asyncio.run(
        portfolio_plans.get_portfolio_plan(plan["plan_id"], current_user={"id": "user-1"})
    )

    item = response["data"]["items"][0]
    assert item["ai_risk"]["llm"]["run_id"] == "run-new"
    assert item["ai_risk"]["llm"]["severity"] == "high"
    assert item["ai_risk"]["llm"]["summary"] == "监管问询风险"
    summary = response["data"]["plan"]["summary"]["ai_risk_llm_summary"]
    assert summary["run_id"] == "run-new"
    assert summary["status"] == "completed_with_failures"
    assert summary["industries"][0]["industry"] == "银行"

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
    finance_collections = _finance_collections()
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: finance_collections)

    response = portfolio_plans._review_plan("plan-user-1", "approved", "ok", {"id": "user-1", "username": "alice"})

    assert response["success"] is True
    assert collections["portfolio_plan_reviews"].docs[0]["user_id"] == "user-1"
    assert collections["portfolio_trade_plans"].find_one({"plan_id": "plan-user-1"})["status"] == "approved"


def test_rejected_plan_can_be_approved_without_execution_trace(monkeypatch):
    collections = _collections()
    finance_collections = _finance_collections()
    collections["portfolio_trade_plans"].docs[0]["status"] = "rejected"
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: finance_collections)

    response = portfolio_plans._review_plan("plan-user-1", "approved", "looks ok", {"id": "user-1", "username": "alice"})

    assert response["success"] is True
    assert collections["portfolio_trade_plans"].find_one({"plan_id": "plan-user-1"})["status"] == "approved"
    assert collections["portfolio_plan_reviews"].docs[0]["decision"] == "approved"


def test_rejected_plan_approval_rejects_live_signals(monkeypatch):
    collections = _collections()
    finance_collections = _finance_collections()
    collections["portfolio_trade_plans"].docs[0]["status"] = "rejected"
    finance_collections["trade_signals"].insert_one(
        {
            "order_id": "order-1",
            "plan_id": "plan-user-1",
            "user_id": "user-1",
            "mode": "live",
            "status": "cancelled",
        }
    )
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: finance_collections)

    with pytest.raises(portfolio_plans.HTTPException) as exc:
        portfolio_plans._review_plan("plan-user-1", "approved", "retry", {"id": "user-1", "username": "alice"})

    assert exc.value.status_code == 409
    assert "live signals" in exc.value.detail
    assert collections["portfolio_trade_plans"].find_one({"plan_id": "plan-user-1"})["status"] == "rejected"
    assert not collections["portfolio_plan_reviews"].docs


def test_review_plan_rejects_paper_executed_status_flip(monkeypatch):
    collections = _collections()
    finance_collections = _finance_collections()
    collections["portfolio_trade_plans"].docs[0]["status"] = "approved"
    collections["portfolio_paper_executions"].insert_one(
        {"plan_id": "plan-user-1", "user_id": "user-1", "symbol": "000001"}
    )
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: finance_collections)

    with pytest.raises(portfolio_plans.HTTPException) as exc:
        portfolio_plans._review_plan("plan-user-1", "rejected", "too late", {"id": "user-1", "username": "alice"})

    assert exc.value.status_code == 409
    assert "paper executions" in exc.value.detail
    assert collections["portfolio_trade_plans"].find_one({"plan_id": "plan-user-1"})["status"] == "approved"
    assert not collections["portfolio_plan_reviews"].docs


def test_reject_approved_plan_cancels_unfilled_live_signals(monkeypatch):
    collections = _collections()
    finance_collections = _finance_collections()
    collections["portfolio_trade_plans"].docs[0]["status"] = "approved"
    finance_collections["trade_signals"].insert_one(
        {
            "order_id": "order-1",
            "plan_id": "plan-user-1",
            "user_id": "user-1",
            "mode": "live",
            "status": "submitted",
        }
    )
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: finance_collections)

    response = portfolio_plans._review_plan("plan-user-1", "rejected", "bad idea", {"id": "user-1", "username": "alice"})

    assert response["success"] is True
    assert collections["portfolio_trade_plans"].find_one({"plan_id": "plan-user-1"})["status"] == "rejected"
    assert collections["portfolio_plan_reviews"].docs[0]["decision"] == "rejected"
    signal = finance_collections["trade_signals"].find_one({"order_id": "order-1"})
    assert signal["status"] == "cancelled"
    assert signal["cancel_reason"] == "bad idea"
    log = collections["portfolio_plan_operation_logs"].docs[0]
    assert log["operation_type"] == "reject_plan"
    assert log["payload"]["previous_status"] == "approved"
    assert log["payload"]["cancelled_signal_count"] == 1


def test_reject_approved_plan_rejects_live_fills(monkeypatch):
    collections = _collections()
    finance_collections = _finance_collections()
    collections["portfolio_trade_plans"].docs[0]["status"] = "approved"
    finance_collections["trade_signals"].insert_one(
        {
            "order_id": "order-1",
            "plan_id": "plan-user-1",
            "user_id": "user-1",
            "mode": "live",
            "status": "partial_filled",
            "filled_qty": 100,
        }
    )
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: finance_collections)

    with pytest.raises(portfolio_plans.HTTPException) as exc:
        portfolio_plans._review_plan("plan-user-1", "rejected", "too late", {"id": "user-1", "username": "alice"})

    assert exc.value.status_code == 409
    assert "live fills" in exc.value.detail
    assert collections["portfolio_trade_plans"].find_one({"plan_id": "plan-user-1"})["status"] == "approved"
    assert not collections["portfolio_plan_reviews"].docs


def test_cancel_plan_cancels_unfilled_live_signals(monkeypatch):
    collections = _collections()
    finance_collections = _finance_collections()
    collections["portfolio_trade_plans"].docs[0]["status"] = "approved"
    finance_collections["trade_signals"].insert_one(
        {
            "order_id": "order-1",
            "plan_id": "plan-user-1",
            "user_id": "user-1",
            "mode": "live",
            "status": "pending",
        }
    )
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: finance_collections)

    response = portfolio_plans._cancel_plan("plan-user-1", "clicked by mistake", {"id": "user-1", "username": "alice"})

    assert response["success"] is True
    assert collections["portfolio_trade_plans"].find_one({"plan_id": "plan-user-1"})["status"] == "cancelled"
    assert collections["portfolio_plan_reviews"].docs[0]["decision"] == "cancelled"
    assert finance_collections["trade_signals"].find_one({"order_id": "order-1"})["status"] == "cancelled"
    assert collections["portfolio_plan_operation_logs"].docs[0]["operation_type"] == "cancel_plan"


def test_cancel_plan_rejects_live_fills(monkeypatch):
    collections = _collections()
    finance_collections = _finance_collections()
    collections["portfolio_trade_plans"].docs[0]["status"] = "approved"
    finance_collections["trade_signals"].insert_one(
        {
            "order_id": "order-1",
            "plan_id": "plan-user-1",
            "user_id": "user-1",
            "mode": "live",
            "status": "partial_filled",
            "filled_qty": 100,
        }
    )
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: finance_collections)

    with pytest.raises(portfolio_plans.HTTPException) as exc:
        portfolio_plans._cancel_plan("plan-user-1", "too late", {"id": "user-1", "username": "alice"})

    assert exc.value.status_code == 409
    assert "live fills" in exc.value.detail
    assert collections["portfolio_trade_plans"].find_one({"plan_id": "plan-user-1"})["status"] == "approved"


def test_cancel_plan_allows_partially_executed_paper(monkeypatch):
    # A partially_executed (paper shortfall) plan always carries paper executions
    # but no live fills, so cancelling it must be allowed to escape the dead-end.
    collections = _collections()
    finance_collections = _finance_collections()
    plan_doc = collections["portfolio_trade_plans"].docs[0]
    plan_doc["status"] = "partially_executed"
    plan_doc["record_kind"] = "trade_plan"
    collections["portfolio_paper_executions"].insert_one(
        {"plan_id": "plan-user-1", "user_id": "user-1", "action": "buy", "quantity": 100}
    )
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: finance_collections)

    response = portfolio_plans._cancel_plan("plan-user-1", "abandon partial", {"id": "user-1", "username": "alice"})

    assert response["success"] is True
    assert collections["portfolio_trade_plans"].find_one({"plan_id": "plan-user-1"})["status"] == "cancelled"


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


def test_list_portfolio_plan_generation_tasks_uses_light_projection(monkeypatch):
    class _ProjectingCollection(_Collection):
        def find(self, query=None, projection=None):
            rows = super().find(query, projection)
            if not projection:
                return rows
            include_fields = {key for key, enabled in projection.items() if enabled and key != "_id"}
            return _Cursor([{key: row.get(key) for key in include_fields if key in row} for row in rows])

    collections = _collections()
    collections["portfolio_plan_generation_tasks"] = _ProjectingCollection(
        [
            {
                "task_id": "task-1",
                "task_type": "holdings_risk_review",
                "user_id": "user-1",
                "status": "completed",
                "worker_id": "worker-1",
                "created_at": "2025-01-01T10:00:00",
                "result": {"positions": [{"symbol": "000001.SZ"}] * 1000},
            }
        ]
    )
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)

    response = asyncio.run(
        portfolio_plans.list_portfolio_plan_generation_tasks(
            status=None,
            limit=20,
            current_user={"id": "user-1"},
        )
    )

    task = response["data"][0]
    assert task["task_id"] == "task-1"
    assert task["worker_id"] == "worker-1"
    assert "result" not in task


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
    assert ctx["signal_count"] == 2
    assert ctx["active_signal_count"] == 1
    assert ctx["status_summary"] == {"filled": 1, "pending": 1}
    plan_doc = trading["portfolio_trade_plans"].find_one({"plan_id": "plan-user-1"})
    assert plan_doc.get("sell_settled_at") is not None


def test_plan_execution_context_distinguishes_terminal_live_history():
    finance = _finance_collections()
    finance["trade_signals"] = _Collection(
        [
            {"plan_id": "plan-user-1", "user_id": "user-1", "mode": "live", "execution_phase": "sell", "status": "cancelled"},
            {"plan_id": "plan-user-1", "user_id": "user-1", "mode": "live", "execution_phase": "buy", "status": "failed"},
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
    assert ctx["signal_count"] == 2
    assert ctx["active_signal_count"] == 0
    assert ctx["status_summary"] == {"cancelled": 1, "failed": 1}


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


def _run_execution_context(finance, plan):
    import routers.portfolio_plans as pp

    trading = {
        "portfolio_paper_executions": _Collection(),
        "portfolio_trade_plans": _Collection([dict(plan)]),
    }
    orig = pp.get_trading_db_collections
    pp.get_trading_db_collections = lambda: trading
    try:
        ctx = pp._plan_execution_context(finance, plan, "user-1")
    finally:
        pp.get_trading_db_collections = orig
    return ctx, trading


def test_plan_execution_context_stamps_executed_live_when_done():
    finance = _finance_collections()
    finance["trade_signals"] = _Collection(
        [
            {"plan_id": "plan-user-1", "user_id": "user-1", "mode": "live", "execution_phase": "sell", "status": "filled", "filled_qty": 100},
            {"plan_id": "plan-user-1", "user_id": "user-1", "mode": "live", "execution_phase": "buy", "status": "filled", "filled_qty": 200},
        ]
    )
    plan = {"plan_id": "plan-user-1", "user_id": "user-1", "record_kind": "trade_plan", "status": "approved"}
    ctx, trading = _run_execution_context(finance, plan)

    assert ctx["execution_phase_state"] == "done"
    assert ctx["active_signal_count"] == 0
    # approved -> executed_live persisted and reflected on the in-memory plan.
    assert plan["status"] == "executed_live"
    assert plan.get("live_executed_at") is not None
    assert trading["portfolio_trade_plans"].find_one({"plan_id": "plan-user-1"})["status"] == "executed_live"


def test_plan_execution_context_keeps_approved_while_signals_active():
    finance = _finance_collections()
    finance["trade_signals"] = _Collection(
        [
            {"plan_id": "plan-user-1", "user_id": "user-1", "mode": "live", "execution_phase": "sell", "status": "filled"},
            {"plan_id": "plan-user-1", "user_id": "user-1", "mode": "live", "execution_phase": "buy", "status": "pending"},
        ]
    )
    plan = {"plan_id": "plan-user-1", "user_id": "user-1", "record_kind": "trade_plan", "status": "approved"}
    _ctx, trading = _run_execution_context(finance, plan)

    assert plan["status"] == "approved"
    assert trading["portfolio_trade_plans"].find_one({"plan_id": "plan-user-1"})["status"] == "approved"


def test_plan_execution_context_does_not_stamp_executed_live_without_fills():
    # All live signals terminal but zero fills (cancelled/rejected) -> the plan
    # never executed, so it must stay approved, not flip to executed_live.
    finance = _finance_collections()
    finance["trade_signals"] = _Collection(
        [
            {"plan_id": "plan-user-1", "user_id": "user-1", "mode": "live", "execution_phase": "sell", "status": "cancelled"},
            {"plan_id": "plan-user-1", "user_id": "user-1", "mode": "live", "execution_phase": "buy", "status": "rejected"},
        ]
    )
    plan = {"plan_id": "plan-user-1", "user_id": "user-1", "record_kind": "trade_plan", "status": "approved"}
    ctx, trading = _run_execution_context(finance, plan)

    assert ctx["execution_phase_state"] == "done"
    assert ctx["active_signal_count"] == 0
    assert plan["status"] == "approved"
    assert trading["portfolio_trade_plans"].find_one({"plan_id": "plan-user-1"})["status"] == "approved"


def test_plan_execution_context_reverts_executed_live_without_signals():
    finance = _finance_collections()
    finance["trade_signals"] = _Collection()
    plan = {
        "plan_id": "plan-user-1",
        "user_id": "user-1",
        "record_kind": "trade_plan",
        "status": "executed_live",
        "live_executed_at": "2026-06-26T00:00:00",
    }
    ctx, trading = _run_execution_context(finance, plan)

    assert ctx["execution_mode"] == "not_executed"
    assert plan["status"] == "approved"
    assert "live_executed_at" not in plan
    plan_doc = trading["portfolio_trade_plans"].find_one({"plan_id": "plan-user-1"})
    assert plan_doc["status"] == "approved"
    assert "live_executed_at" not in plan_doc


def test_plan_execution_context_fixes_extman_executed_live_to_filled():
    finance = _finance_collections()
    finance["trade_signals"] = _Collection()
    plan = {
        "plan_id": "growth_cycle_index_enhanced_hash_user_extman_20260624192646767ef3",
        "user_id": "user-1",
        "record_kind": "trade_plan",
        "plan_type": "rebalance",
        "status": "executed_live",
        "live_executed_at": "2026-06-26T00:00:00",
    }
    ctx, trading = _run_execution_context(finance, plan)

    assert ctx["execution_mode"] == "not_executed"
    assert plan["status"] == "filled"
    assert plan["record_kind"] == "ledger_event"
    assert plan["plan_type"] == "manual"
    assert plan["origin"] == "manual"
    assert plan["manual_action"] == "external_manual"
    plan_doc = trading["portfolio_trade_plans"].find_one({"plan_id": plan["plan_id"]})
    assert plan_doc["status"] == "filled"
    assert plan_doc["record_kind"] == "ledger_event"
    assert plan_doc["plan_type"] == "manual"


def test_plan_execution_context_does_not_stamp_executed_live_for_observation():
    finance = _finance_collections()
    finance["trade_signals"] = _Collection(
        [
            {"plan_id": "plan-user-1", "user_id": "user-1", "mode": "live", "execution_phase": "sell", "status": "filled", "filled_qty": 100},
            {"plan_id": "plan-user-1", "user_id": "user-1", "mode": "live", "execution_phase": "buy", "status": "filled", "filled_qty": 200},
        ]
    )
    plan = {"plan_id": "plan-user-1", "user_id": "user-1", "record_kind": "observation", "status": "approved"}
    _ctx, _trading = _run_execution_context(finance, plan)

    # Only trade_plans run the lifecycle state machine.
    assert plan["status"] == "approved"


def test_publish_live_signals_dry_run_builds_contract(monkeypatch):
    collections = _collections()
    collections["portfolio_trade_plans"].docs[0].update(
        {
            "status": "approved",
            "summary": {
                "baseline_equity": 1_200_000,
                "baseline_cash": 320_000,
                "capital_basis": "account_equity",
            },
        }
    )
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
                "current_shares": 200,
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
    assert signal["max_slippage_bps"] == 30
    assert signal["price_ceiling"] == 10.03
    assert signal["effective_limit_price"] == 10.03
    assert signal["activate_after"]
    assert signal["valid_until"]
    activate_after = datetime.fromisoformat(signal["activate_after"])
    valid_until = datetime.fromisoformat(signal["valid_until"])
    assert (valid_until - activate_after).total_seconds() == 600
    assert signal["phase_barrier"] == {"type": "plan_sells_terminal", "plan_id": "plan-user-1"}
    sell_signal = response["data"]["signals"][1]
    assert sell_signal["execution_phase"] == "sell"
    assert sell_signal["execution_priority"] == -9998
    assert sell_signal["price_floor"] == 11.96
    assert sell_signal["phase_barrier"] is None
    assert response["data"]["risk_report"]["blocked_count"] == 0
    assert response["data"]["reprice_summary"]["equity_used"] == 1_200_000
    assert response["data"]["reprice_summary"]["plan_equity_used"] == 1_200_000
    assert response["data"]["reprice_summary"]["plan_cash_used"] == 320_000
    assert response["data"]["reprice_summary"]["account_available_cash"] == 100_000
    assert response["data"]["reprice_summary"]["plan_equity_source"] == "summary.baseline_equity"
    assert response["data"]["reprice_summary"]["plan_cash_source"] == "summary.baseline_cash"
    assert response["data"]["reprice_summary"]["capital_basis"] == "account_equity"
    assert len(response["data"]["reprice_summary"]["items"]) == 2


def test_publish_live_signals_uses_plan_equity_not_account_total_asset(monkeypatch):
    collections = _collections()
    collections["portfolio_trade_plans"].docs[0].update(
        {
            "status": "approved",
            "summary": {"baseline_equity": 1_200_000, "baseline_cash": 500_000},
        }
    )
    collections["portfolio_trade_plan_items"].docs.append(
        {
            "plan_id": "plan-user-1",
            "user_id": "user-1",
            "symbol": "000001",
            "action": "buy",
            "delta_shares": 100,
            "target_shares": 100,
            "target_weight": 0.1,
            "estimated_price": 10.0,
            "rank": 1,
        }
    )
    finance = _finance_collections()
    finance["trader_accounts"].docs[0]["total_asset"] = 5_000_000
    finance["securities_accounts"].docs[0]["risk_limits"]["max_order_amount"] = 500_000
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

    assert response["data"]["reprice_summary"]["equity_used"] == 1_200_000
    assert response["data"]["reprice_summary"]["account_equity"] == 5_000_000
    row = response["data"]["reprice_summary"]["items"][0]
    assert row["new_target_shares"] == 12_000
    assert row["new_delta_shares"] == 12_000


def test_publish_live_signals_falls_back_to_initial_capital_when_plan_baseline_missing(monkeypatch):
    collections = _collections()
    collections["portfolio_trade_plans"].docs[0].update(
        {
            "status": "approved",
            "params_snapshot": {"initial_capital": 300_000, "lot_size": 100},
            "summary": {},
        }
    )
    collections["portfolio_trade_plan_items"].docs.append(
        {
            "plan_id": "plan-user-1",
            "user_id": "user-1",
            "symbol": "000001",
            "action": "buy",
            "delta_shares": 100,
            "target_shares": 100,
            "target_weight": 0.1,
            "estimated_price": 10.0,
            "rank": 1,
        }
    )
    finance = _finance_collections()
    finance["trader_accounts"].docs[0]["total_asset"] = 5_000_000
    finance["securities_accounts"].docs[0]["risk_limits"]["max_order_amount"] = 500_000
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

    assert response["data"]["reprice_summary"]["equity_used"] == 300_000
    assert response["data"]["reprice_summary"]["plan_equity_source"] == "params_snapshot.initial_capital"
    assert response["data"]["reprice_summary"]["plan_cash_source"] == "params_snapshot.initial_capital"
    row = response["data"]["reprice_summary"]["items"][0]
    assert row["new_target_shares"] == 3_000
    assert row["new_delta_shares"] == 3_000


def test_publish_live_signals_blocks_confirm_when_plan_capital_missing(monkeypatch):
    collections = _collections()
    collections["portfolio_trade_plans"].docs[0].update(
        {
            "status": "approved",
            "params_snapshot": {"lot_size": 100},
            "summary": {},
            "initial_capital": None,
        }
    )
    collections["portfolio_trade_plan_items"].docs.append(
        {
            "plan_id": "plan-user-1",
            "user_id": "user-1",
            "symbol": "000001",
            "action": "buy",
            "delta_shares": 100,
            "target_shares": 100,
            "target_weight": 0.1,
            "estimated_price": 10.0,
            "rank": 1,
        }
    )
    finance = _finance_collections()
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: finance)
    monkeypatch.setattr(portfolio_plans, "get_data_db", _data_collections)

    preview = asyncio.run(
        portfolio_plans.publish_portfolio_plan_live_signals(
            "plan-user-1",
            portfolio_plans.PublishLiveSignalsRequest(securities_account_id="acct-1", dry_run=True),
            current_user={"id": "user-1"},
        )
    )
    assert "missing_plan_equity" in preview["data"]["reprice_summary"]["blockers"]
    assert "missing_plan_cash" in preview["data"]["reprice_summary"]["blockers"]

    with pytest.raises(portfolio_plans.HTTPException) as exc:
        asyncio.run(
            portfolio_plans.publish_portfolio_plan_live_signals(
                "plan-user-1",
                portfolio_plans.PublishLiveSignalsRequest(securities_account_id="acct-1", dry_run=False),
                current_user={"id": "user-1"},
            )
        )
    assert exc.value.status_code == 409
    assert "missing plan capital baseline" in exc.value.detail["message"]


def test_publish_live_signals_dry_run_blocked_flag_reflects_capital_blocker(monkeypatch):
    # A sell-to-zero builds a signal even without a capital baseline, but the
    # dry-run response must still report blocked=True so the preview matches the
    # confirm-time 409.
    collections = _collections()
    collections["portfolio_trade_plans"].docs[0].update(
        {"status": "approved", "params_snapshot": {"lot_size": 100}, "summary": {}}
    )
    collections["portfolio_trade_plan_items"].docs.append(
        {
            "plan_id": "plan-user-1",
            "user_id": "user-1",
            "symbol": "600000",
            "action": "sell",
            "delta_shares": -200,
            "current_shares": 200,
            "target_shares": 0,
            "target_weight": 0,
            "estimated_price": 12.0,
            "rank": 1,
        }
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

    assert len(response["data"]["signals"]) == 1
    assert response["data"]["blocked"] is True
    assert "missing_plan_equity" in response["data"]["reprice_summary"]["blockers"]


def test_publish_live_signals_reprice_blocks_missing_price(monkeypatch):
    collections = _collections()
    collections["portfolio_trade_plans"].docs[0].update(
        {"status": "approved", "params_snapshot": {"initial_capital": 1_000_000, "lot_size": 100}}
    )
    collections["portfolio_trade_plan_items"].docs.append(
        {
            "plan_id": "plan-user-1",
            "user_id": "user-1",
            "symbol": "000001",
            "action": "buy",
            "delta_shares": 100,
            "target_shares": 100,
            "target_weight": 0.01,
            "estimated_price": 10.0,
            "rank": 1,
        }
    )
    finance = _finance_collections()
    data = _data_collections()
    data["volume_price_realtime_latest"] = _Collection([])
    data["volume_price"] = _Collection([])
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: finance)
    monkeypatch.setattr(portfolio_plans, "get_data_db", lambda: data)

    preview = asyncio.run(
        portfolio_plans.publish_portfolio_plan_live_signals(
            "plan-user-1",
            portfolio_plans.PublishLiveSignalsRequest(securities_account_id="acct-1", dry_run=True),
            current_user={"id": "user-1"},
        )
    )
    assert preview["data"]["blocked"] is True
    assert preview["data"]["reprice_summary"]["missing_price_symbols"] == ["000001"]

    with pytest.raises(portfolio_plans.HTTPException) as exc:
        asyncio.run(
            portfolio_plans.publish_portfolio_plan_live_signals(
                "plan-user-1",
                portfolio_plans.PublishLiveSignalsRequest(securities_account_id="acct-1", dry_run=False),
                current_user={"id": "user-1"},
            )
        )
    assert "missing usable prices" in str(exc.value.detail["message"])


def test_publish_live_signals_allows_missing_price_market_sell(monkeypatch):
    collections = _collections()
    collections["portfolio_trade_plans"].docs[0].update(
        {"status": "approved", "params_snapshot": {"initial_capital": 1_000_000, "lot_size": 100}}
    )
    collections["portfolio_trade_plan_items"].docs.append(
        {
            "plan_id": "plan-user-1",
            "user_id": "user-1",
            "symbol": "600000",
            "action": "sell",
            "delta_shares": -200,
            "current_shares": 200,
            "target_shares": 0,
            "target_weight": 0,
            "estimated_price": 12.0,
            "rank": 1,
        }
    )
    finance = _finance_collections()
    data = _data_collections()
    data["volume_price_realtime_latest"] = _Collection([])
    data["volume_price"] = _Collection([])
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: finance)
    monkeypatch.setattr(portfolio_plans, "get_data_db", lambda: data)

    response = asyncio.run(
        portfolio_plans.publish_portfolio_plan_live_signals(
            "plan-user-1",
            portfolio_plans.PublishLiveSignalsRequest(securities_account_id="acct-1", dry_run=True),
            current_user={"id": "user-1"},
        )
    )

    assert response["data"]["blocked"] is False
    assert response["data"]["signals"][0]["action"] == "sell"
    assert response["data"]["signals"][0]["order_type"] == "market"
    assert response["data"]["signals"][0]["price"] is None
    assert response["data"]["reprice_summary"]["missing_price_symbols"] == []


def test_publish_live_signals_risk_failure_does_not_persist_reprice(monkeypatch):
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
            "target_weight": 0.1,
            "estimated_price": 8.0,
            "rank": 1,
        }
    )
    finance = _finance_collections()
    finance["securities_accounts"].docs[0]["live_trading_enabled"] = False
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: finance)
    monkeypatch.setattr(portfolio_plans, "get_data_db", _data_collections)

    with pytest.raises(portfolio_plans.HTTPException):
        asyncio.run(
            portfolio_plans.publish_portfolio_plan_live_signals(
                "plan-user-1",
                portfolio_plans.PublishLiveSignalsRequest(securities_account_id="acct-1", dry_run=False),
                current_user={"id": "user-1"},
            )
        )

    item = collections["portfolio_trade_plan_items"].find_one({"plan_id": "plan-user-1", "symbol": "000001"})
    plan = collections["portfolio_trade_plans"].find_one({"plan_id": "plan-user-1"})
    assert item["estimated_price"] == 8.0
    assert "estimated_price_at_approval" not in item
    assert "repriced_at" not in plan


def test_publish_live_signals_write_is_idempotent(monkeypatch):
    collections = _collections()
    collections["portfolio_trade_plans"].docs[0].update(
        {"status": "approved", "params_snapshot": {"initial_capital": 1_000_000, "lot_size": 100}}
    )
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


def test_publish_live_signals_records_publish_duration_ms(monkeypatch):
    collections = _collections()
    collections["portfolio_trade_plans"].docs[0].update(
        {"status": "approved", "params_snapshot": {"initial_capital": 1_000_000, "lot_size": 100}}
    )
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
    asyncio.run(
        portfolio_plans.publish_portfolio_plan_live_signals(
            "plan-user-1", request, current_user={"id": "user-1"}
        )
    )

    reprice_logs = [
        d
        for d in collections["portfolio_plan_operation_logs"].docs
        if d.get("operation_type") == "publish_reprice"
    ]
    assert len(reprice_logs) == 1
    payload = reprice_logs[0]["payload"]
    assert payload["signal_count"] == 1
    assert isinstance(payload["publish_duration_ms"], (int, float))
    assert payload["publish_duration_ms"] >= 0


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
                "current_shares": 100,
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
    assert buy_signal["price_ceiling"] == 10.03
    assert buy_signal["effective_limit_price"] == 10.03
    assert sell_signal["price_floor"] == 11.96
    assert sell_signal["effective_limit_price"] == 11.96


def test_publish_live_signals_rejects_empty_signal_set(monkeypatch):
    collections = _collections()
    collections["portfolio_trade_plans"].docs[0].update(
        {"status": "approved", "params_snapshot": {"initial_capital": 1_000_000, "lot_size": 100}}
    )
    collections["portfolio_trade_plan_items"].docs.append(
        {
            "plan_id": "plan-user-1",
            "user_id": "user-1",
            "symbol": "000001",
            "action": "hold",
            "delta_shares": 0,
            "current_shares": 100,
            "target_shares": 100,
            "estimated_price": 10.0,
            "rank": 1,
        }
    )
    finance = _finance_collections()
    finance["trader_positions"].docs.append(
        {"user_id": "user-1", "securities_account_id": "acct-1", "symbol": "000001", "shares": 100, "can_use_volume": 100}
    )
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
    assert exc.value.detail["message"] == "no live signals could be built"


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


def test_execute_paper_requires_execute_date(monkeypatch):
    collections = _collections()
    collections["portfolio_trade_plans"].docs[0].update(
        {
            "status": "approved",
            "params_snapshot": {"initial_capital": 1_000_000, "lot_size": 100},
        }
    )
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: _finance_collections())

    with pytest.raises(portfolio_plans.HTTPException) as exc:
        portfolio_plans._apply_paper_execution(
            collections,
            data_collections=_data_collections(),
            plan_id="plan-user-1",
            user_id="user-1",
        )

    assert exc.value.status_code == 400
    assert "缺少 execute_date" in exc.value.detail


def test_paper_execution_status_missing_execute_date_is_not_ready():
    collections = _collections()
    plan = {
        "plan_id": "plan-user-1",
        "user_id": "user-1",
        "status": "approved",
    }
    items = [{"symbol": "000001", "delta_shares": 100}]

    status = portfolio_plans._paper_execution_status(collections, _data_collections(), plan, items)

    assert status["backend_execution_status"] == "missing_execute_date"
    assert status["missing_execute_date"] is True
    assert status["open_price_ready"] is False
    assert status["effective_execute_date"] is None


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


def test_execute_paper_sells_before_buys_avoids_spurious_partial(monkeypatch):
    # A rank-1 buy whose only funding is a rank-2 sell must fully fill: processing
    # sells first credits the cash before the buy is attempted. A naive rank-order
    # pass would starve the buy and report a spurious partially_executed.
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
            "cash": 1_000,  # not enough for the buy on its own
            "equity": 11_000,
            "positions": [{"symbol": "000002", "name": "卖出标的", "shares": 1000, "cost_basis": 10.0}],
        }
    )
    collections["portfolio_trade_plan_items"].docs.extend(
        [
            {"plan_id": "child-plan", "user_id": "user-1", "symbol": "000001", "name": "买入标的", "rank": 1, "delta_shares": 500},
            {"plan_id": "child-plan", "user_id": "user-1", "symbol": "000002", "name": "卖出标的", "rank": 2, "delta_shares": -1000},
        ]
    )
    data = _data_collections()
    data["volume_price"].docs.extend(
        [
            {"symbol": "000001", "trade_date": "20250103", "open": 20.0, "close": 20.0},
            {"symbol": "000002", "trade_date": "20250103", "open": 10.0, "close": 10.0},
        ]
    )
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: _finance_collections())

    result = portfolio_plans._apply_paper_execution(
        collections,
        data_collections=data,
        plan_id="child-plan",
        user_id="user-1",
    )

    assert result["status"] == "executed_paper"
    buy_exec = next(e for e in result["executions"] if e["symbol"] == "000001")
    assert buy_exec["action"] == "buy"
    assert buy_exec["quantity"] == 500


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


def test_plan_detail_fills_missing_item_metadata_from_score_snapshot(monkeypatch):
    collections = _collections()
    collections["portfolio_trade_plans"].docs[0].update(
        {
            "score_date": "20250102",
            "params_snapshot": {
                "universe_index": "csi1000",
                "score_type": "growth_cycle_weighted",
                "growth_weight": 0.3,
                "cycle_weight": 0.7,
            },
        }
    )
    collections["portfolio_trade_plan_items"].docs.append(
        {
            "plan_id": "plan-user-1",
            "user_id": "user-1",
            "strategy_template_id": "s1",
            "symbol": "002897.SZ",
            "name": "",
            "industry": "",
            "score_value": None,
            "action": "sell",
            "current_shares": 100,
            "target_shares": 0,
            "delta_shares": -100,
            "estimated_price": 84.5,
            "estimated_amount": None,
            "rank": None,
        }
    )
    finance = _finance_collections()
    data = _data_collections()
    score_rows = _Collection(
        [
            {
                "score_date": "20250102",
                "symbol": "002897.SZ",
                "index_codes": ["csi1000"],
                "name": "意华股份",
                "industry": "元器件",
                "growth_score": 79.5,
                "cycle_score": 85.95,
            }
        ]
    )
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: finance)
    monkeypatch.setattr(portfolio_plans, "get_data_db", lambda: data)
    monkeypatch.setattr(portfolio_plans, "finance_db", {"stock_scores": score_rows})

    response = asyncio.run(portfolio_plans.get_portfolio_plan("plan-user-1", current_user={"id": "user-1"}))

    item = response["data"]["items"][0]
    assert item["name"] == "意华股份"
    assert item["industry"] == "元器件"
    assert item["score_value"] == pytest.approx(84.015)
    assert item["estimated_amount"] == pytest.approx(8450.0)
    assert item["rank"] is None


def test_score_snapshot_batch_falls_back_to_any_index(monkeypatch):
    # One symbol is scored under the plan's universe; another only exists under
    # a different index. The batched lookup must resolve both (universe-scoped
    # first, then the any-index fallback) without per-symbol round-trips.
    plan = {
        "plan_id": "p-fallback",
        "score_date": "20250102",
        "params_snapshot": {
            "universe_index": "csi1000",
            "score_type": "growth_cycle_weighted",
            "growth_weight": 0.3,
            "cycle_weight": 0.7,
        },
    }
    items = [
        {"symbol": "000001.SZ", "name": "", "industry": "", "score_value": None},
        {"symbol": "600000.SH", "name": "", "industry": "", "score_value": None},
    ]
    score_rows = _Collection(
        [
            {"score_date": "20250102", "symbol": "000001.SZ", "index_codes": ["csi1000"], "name": "平安银行", "industry": "银行", "growth_score": 70.0, "cycle_score": 80.0},
            {"score_date": "20250102", "symbol": "600000.SH", "index_codes": ["csi300"], "name": "浦发银行", "industry": "银行", "growth_score": 60.0, "cycle_score": 90.0},
        ]
    )
    monkeypatch.setattr(portfolio_plans, "finance_db", {"stock_scores": score_rows})

    enriched = portfolio_plans._enrich_plan_items_with_score_snapshot(
        data_collections=_data_collections(),
        plan=plan,
        items=items,
    )
    by_symbol = {row["symbol"]: row for row in enriched}
    assert by_symbol["000001.SZ"]["name"] == "平安银行"
    assert by_symbol["600000.SH"]["name"] == "浦发银行"  # resolved via any-index fallback
    assert by_symbol["600000.SH"]["industry"] == "银行"


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
    collections["portfolio_trade_plans"].docs[0].update(
        {"status": "approved", "params_snapshot": {"initial_capital": 1_000_000, "lot_size": 100}}
    )
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


def _partial_publish_collections():
    """Two buys: 000001 trips max_order_amount (30000 > 20000 cap), 600000 passes."""
    collections = _collections()
    collections["portfolio_trade_plans"].docs[0].update(
        {"status": "approved", "params_snapshot": {"initial_capital": 1_000_000, "lot_size": 100}}
    )
    collections["portfolio_trade_plan_items"].docs.extend(
        [
            {
                "plan_id": "plan-user-1",
                "user_id": "user-1",
                "symbol": "000001",
                "action": "buy",
                "delta_shares": 3000,
                "target_shares": 3000,
                "current_shares": 0,
                "estimated_price": 10.0,
                "rank": 1,
            },
            {
                "plan_id": "plan-user-1",
                "user_id": "user-1",
                "symbol": "600000",
                "action": "buy",
                "delta_shares": 100,
                "target_shares": 100,
                "current_shares": 0,
                "estimated_price": 12.0,
                "rank": 2,
            },
        ]
    )
    return collections


def test_publish_live_signals_all_or_nothing_blocks_whole_plan(monkeypatch):
    collections = _partial_publish_collections()
    finance = _finance_collections()
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
    # Default (all-or-nothing): one blocked symbol stops the whole publish.
    assert not finance["trade_signals"].docs


def test_publish_live_signals_partial_publishes_passing_and_skips_blocked(monkeypatch):
    collections = _partial_publish_collections()
    finance = _finance_collections()
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: finance)
    monkeypatch.setattr(portfolio_plans, "get_data_db", _data_collections)

    response = asyncio.run(
        portfolio_plans.publish_portfolio_plan_live_signals(
            "plan-user-1",
            portfolio_plans.PublishLiveSignalsRequest(
                securities_account_id="acct-1", dry_run=False, allow_partial=True
            ),
            current_user={"id": "user-1"},
        )
    )

    data = response["data"]
    assert data["partial"] is True
    assert data["blocked"] is False
    assert data["inserted_count"] == 1
    assert data["publishable_count"] == 1
    assert [doc["symbol"] for doc in finance["trade_signals"].docs] == ["600000"]
    skipped = {row["symbol"] for row in data["skipped_by_risk"]}
    assert skipped == {"000001"}
    assert "max_order_amount_exceeded" in data["skipped_by_risk"][0]["blockers"]
    plan = collections["portfolio_trade_plans"].docs[0]
    assert plan["live_signal_count"] == 1
    assert plan["live_signal_inserted_count"] == 1


def test_publish_live_signals_partial_dry_run_previews_partition(monkeypatch):
    collections = _partial_publish_collections()
    finance = _finance_collections()
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: finance)
    monkeypatch.setattr(portfolio_plans, "get_data_db", _data_collections)

    response = asyncio.run(
        portfolio_plans.publish_portfolio_plan_live_signals(
            "plan-user-1",
            portfolio_plans.PublishLiveSignalsRequest(
                securities_account_id="acct-1", dry_run=True, allow_partial=True
            ),
            current_user={"id": "user-1"},
        )
    )

    data = response["data"]
    assert data["dry_run"] is True
    assert data["inserted_count"] == 0
    assert data["publishable_count"] == 1
    assert data["blocked"] is False
    assert {row["symbol"] for row in data["skipped_by_risk"]} == {"000001"}
    assert not finance["trade_signals"].docs


def test_publish_live_signals_partial_all_blocked_still_409(monkeypatch):
    collections = _collections()
    collections["portfolio_trade_plans"].docs[0].update(
        {"status": "approved", "params_snapshot": {"initial_capital": 1_000_000, "lot_size": 100}}
    )
    collections["portfolio_trade_plan_items"].docs.append(
        {
            "plan_id": "plan-user-1",
            "user_id": "user-1",
            "symbol": "000001",
            "action": "buy",
            "delta_shares": 3000,
            "target_shares": 3000,
            "current_shares": 0,
            "estimated_price": 10.0,
            "rank": 1,
        }
    )
    finance = _finance_collections()
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: finance)
    monkeypatch.setattr(portfolio_plans, "get_data_db", _data_collections)

    with pytest.raises(portfolio_plans.HTTPException) as exc:
        asyncio.run(
            portfolio_plans.publish_portfolio_plan_live_signals(
                "plan-user-1",
                portfolio_plans.PublishLiveSignalsRequest(
                    securities_account_id="acct-1", dry_run=False, allow_partial=True
                ),
                current_user={"id": "user-1"},
            )
        )

    assert exc.value.status_code == 409
    assert "nothing to publish" in exc.value.detail["message"]
    assert not finance["trade_signals"].docs


def test_publish_live_signals_partial_account_block_is_hard(monkeypatch):
    # An account-level block (live trading disabled) applies to every signal, so
    # partial mode cannot rescue it -- confirm must still 409.
    collections = _partial_publish_collections()
    finance = _finance_collections()
    finance["securities_accounts"].docs[0]["live_trading_enabled"] = False
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: finance)
    monkeypatch.setattr(portfolio_plans, "get_data_db", _data_collections)

    with pytest.raises(portfolio_plans.HTTPException) as exc:
        asyncio.run(
            portfolio_plans.publish_portfolio_plan_live_signals(
                "plan-user-1",
                portfolio_plans.PublishLiveSignalsRequest(
                    securities_account_id="acct-1", dry_run=False, allow_partial=True
                ),
                current_user={"id": "user-1"},
            )
        )

    assert exc.value.status_code == 409
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


def test_replan_remainder_partial_publishes_passing_and_skips_blocked(monkeypatch):
    collections, finance = _remainder_setup(monkeypatch)
    collections["portfolio_trade_plans"].docs[0]["params_snapshot"] = {
        "lot_size": 100,
        "initial_capital": 1_000_000,
    }
    # A second top-up target that trips max_order_amount (3000 * 10 = 30000 > 20000).
    collections["portfolio_trade_plan_items"].docs.append(
        {
            "plan_id": "plan-user-1",
            "user_id": "user-1",
            "symbol": "600000",
            "name": "浦发银行",
            "action": "buy",
            "delta_shares": 3000,
            "target_shares": 3000,
            "current_shares": 0,
            "estimated_price": 10.0,
            "rank": 2,
        }
    )

    request = portfolio_plans.ReplanRemainderRequest(
        securities_account_id="acct-1", dry_run=False, allow_partial=True
    )
    response = asyncio.run(
        portfolio_plans.replan_portfolio_plan_remainder("plan-user-1", request, current_user={"id": "user-1"})
    )

    data = response["data"]
    assert data["partial"] is True
    assert data["inserted_count"] == 1
    assert data["publishable_count"] == 1
    assert {row["symbol"] for row in data["skipped_by_risk"]} == {"600000"}
    # original cancelled signal + one inserted top-up for 000001.
    inserted_symbols = [doc["symbol"] for doc in finance["trade_signals"].docs if doc.get("source") == "plan_topup"]
    assert inserted_symbols == ["000001"]


def _lineage_cash_remainder_setup(monkeypatch, *, broker_available_cash=100_000, active_buy=None):
    collections, finance = _remainder_setup(monkeypatch)
    plan = collections["portfolio_trade_plans"].docs[0]
    plan["params_snapshot"] = {"initial_capital": 100_000, "lot_size": 100}
    plan["params_hash"] = "hash-1"
    plan["strategy_template_id"] = "s1"

    finance["trader_accounts"].docs[0]["available_cash"] = broker_available_cash
    finance["trade_executions"].docs.append(
        {
            "user_id": "user-1",
            "order_id": "live-plan-orig",
            "symbol": "000001",
            "side": "BUY",
            "filled_size": 400,
            "filled_qty": 400,
            "avg_price": 10.0,
            "price": 10.0,
            "timestamp": 1,
        }
    )
    if active_buy is not None:
        finance["trade_signals"].docs.append(active_buy)
    return collections, finance, plan


def test_resolve_lineage_available_cash_without_executions(monkeypatch):
    collections = _collections()
    plan = collections["portfolio_trade_plans"].docs[0]
    plan.update({"params_snapshot": {"initial_capital": 250_000}})
    finance = _finance_collections()
    data = _data_collections()
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: finance)

    snapshot = portfolio_plans._resolve_lineage_available_cash_for_remainder(
        trading_collections=collections,
        finance_collections=finance,
        data_collections=data,
        plan=plan,
        user_id="user-1",
    )

    assert snapshot["lineage_book_cash"] == 250_000
    assert snapshot["frozen_buy_cash"] == 0
    assert snapshot["plan_available_cash"] == 250_000
    assert snapshot["cash_basis"] == "lineage_book"


def test_resolve_lineage_available_cash_deducts_executed_buy_and_freezes_active_buy(monkeypatch):
    collections = _collections()
    plan = collections["portfolio_trade_plans"].docs[0]
    plan.update(
        {
            "params_snapshot": {"initial_capital": 100_000},
            "params_hash": "hash-1",
            "strategy_template_id": "s1",
            "live_signal_count": 1,
            "live_signals_published_at": 1,
        }
    )
    finance = _finance_collections()
    finance["trade_signals"].docs.extend(
        [
            {
                "order_id": "live-plan-orig",
                "user_id": "user-1",
                "plan_id": "plan-user-1",
                "mode": "live",
                "action": "buy",
                "status": "filled",
                "size": 400,
                "filled_qty": 400,
                "symbol": "000001",
            },
            {
                "order_id": "live-plan-active",
                "user_id": "user-1",
                "plan_id": "plan-user-1",
                "mode": "live",
                "action": "buy",
                "status": "submitted",
                "size": 600,
                "filled_qty": 0,
                "symbol": "000001",
                "effective_limit_price": 10.0,
            },
        ]
    )
    finance["trade_executions"].docs.append(
        {
            "user_id": "user-1",
            "order_id": "live-plan-orig",
            "symbol": "000001",
            "side": "BUY",
            "filled_size": 400,
            "avg_price": 10.0,
            "timestamp": 1,
        }
    )
    data = _data_collections()
    data["volume_price_realtime_latest"].docs.append({"symbol": "000001", "close": 10.0})
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: finance)
    monkeypatch.setattr(portfolio_plans, "get_data_db", lambda: data)

    snapshot = portfolio_plans._resolve_lineage_available_cash_for_remainder(
        trading_collections=collections,
        finance_collections=finance,
        data_collections=data,
        plan=plan,
        user_id="user-1",
    )

    assert snapshot["lineage_book_cash"] == 96_000
    assert snapshot["frozen_buy_cash"] == 6_000
    assert snapshot["plan_available_cash"] == 90_000


def test_resolve_lineage_available_cash_partial_filled_active_buy_only_freezes_remainder(monkeypatch):
    collections = _collections()
    plan = collections["portfolio_trade_plans"].docs[0]
    plan.update({"params_snapshot": {"initial_capital": 100_000}})
    finance = _finance_collections()
    finance["trade_signals"].docs.append(
        {
            "order_id": "live-plan-active",
            "user_id": "user-1",
            "plan_id": "plan-user-1",
            "mode": "live",
            "action": "buy",
            "status": "partial_filled",
            "size": 1000,
            "filled_qty": 400,
            "symbol": "000001",
            "price_ceiling": 10.0,
        }
    )
    data = _data_collections()
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: finance)
    monkeypatch.setattr(portfolio_plans, "get_data_db", lambda: data)

    snapshot = portfolio_plans._resolve_lineage_available_cash_for_remainder(
        trading_collections=collections,
        finance_collections=finance,
        data_collections=data,
        plan=plan,
        user_id="user-1",
    )

    assert snapshot["frozen_buy_cash"] == 6_000
    assert snapshot["plan_available_cash"] == 94_000


def test_resolve_lineage_available_cash_missing_frozen_price_blocks(monkeypatch):
    collections = _collections()
    plan = collections["portfolio_trade_plans"].docs[0]
    plan.update({"params_snapshot": {"initial_capital": 100_000}})
    finance = _finance_collections()
    finance["trade_signals"].docs.append(
        {
            "order_id": "live-plan-active",
            "user_id": "user-1",
            "plan_id": "plan-user-1",
            "mode": "live",
            "action": "buy",
            "status": "submitted",
            "size": 100,
            "filled_qty": 0,
            "symbol": "000001",
        }
    )
    data = _data_collections()
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: collections)
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: finance)
    monkeypatch.setattr(portfolio_plans, "get_data_db", lambda: data)

    snapshot = portfolio_plans._resolve_lineage_available_cash_for_remainder(
        trading_collections=collections,
        finance_collections=finance,
        data_collections=data,
        plan=plan,
        user_id="user-1",
    )

    assert snapshot["missing_frozen_price_symbols"] == ["000001"]
    assert "missing_frozen_price" in snapshot["blockers"]


def test_replan_remainder_uses_plan_cash_not_broker_aggregate(monkeypatch):
    collections, finance, _plan = _lineage_cash_remainder_setup(monkeypatch, broker_available_cash=5_000_000)

    response = asyncio.run(
        portfolio_plans.replan_portfolio_plan_remainder(
            "plan-user-1",
            portfolio_plans.ReplanRemainderRequest(securities_account_id="acct-1", dry_run=True),
            current_user={"id": "user-1"},
        )
    )

    data = response["data"]
    assert data["cash_basis"] == "lineage_book"
    assert data["lineage_book_cash"] == 96_000
    assert data["broker_available_cash"] == 5_000_000
    assert data["plan_available_cash"] == 96_000
    assert data["available_cash"] == 96_000
    assert data["signals"][0]["size"] == 600


def test_replan_remainder_cash_capped_by_plan_available(monkeypatch):
    collections, finance, plan = _lineage_cash_remainder_setup(monkeypatch)
    plan["params_snapshot"] = {"initial_capital": 5_000, "lot_size": 100}
    finance["trade_executions"].docs[0]["filled_size"] = 400
    finance["trade_executions"].docs[0]["filled_qty"] = 400
    finance["trade_signals"].docs[0]["filled_qty"] = 400
    finance["trader_positions"].docs[0]["qty"] = 400
    finance["trader_positions"].docs[0]["can_use_volume"] = 400

    response = asyncio.run(
        portfolio_plans.replan_portfolio_plan_remainder(
            "plan-user-1",
            portfolio_plans.ReplanRemainderRequest(securities_account_id="acct-1", dry_run=True),
            current_user={"id": "user-1"},
        )
    )

    data = response["data"]
    remainder = data["remainder"][0]
    assert remainder["gap_shares"] == 600
    assert remainder["topup_shares"] == 100
    assert data["plan_available_cash"] == 1_000
    assert "cash_capped" in remainder["reasons"]


def test_replan_remainder_blocked_when_broker_cash_insufficient(monkeypatch):
    collections, finance, _plan = _lineage_cash_remainder_setup(monkeypatch, broker_available_cash=1_000)

    response = asyncio.run(
        portfolio_plans.replan_portfolio_plan_remainder(
            "plan-user-1",
            portfolio_plans.ReplanRemainderRequest(securities_account_id="acct-1", dry_run=True),
            current_user={"id": "user-1"},
        )
    )

    data = response["data"]
    assert data["plan_available_cash"] == 96_000
    assert data["broker_available_cash"] == 1_000
    assert data["blocked"] is True
    assert "insufficient_cash" in data["risk_report"]["items"][0]["blockers"]


def test_replan_remainder_dry_run_blocked_when_active_buy_missing_frozen_price(monkeypatch):
    collections, finance, _plan = _lineage_cash_remainder_setup(
        monkeypatch,
        active_buy={
            "order_id": "live-plan-active",
            "user_id": "user-1",
            "plan_id": "plan-user-1",
            "mode": "live",
            "action": "buy",
            "status": "submitted",
            "size": 100,
            "filled_qty": 0,
            "symbol": "000001",
        },
    )

    response = asyncio.run(
        portfolio_plans.replan_portfolio_plan_remainder(
            "plan-user-1",
            portfolio_plans.ReplanRemainderRequest(securities_account_id="acct-1", dry_run=True),
            current_user={"id": "user-1"},
        )
    )

    data = response["data"]
    assert data["blocked"] is True
    assert "missing_frozen_price" in data["cash_blockers"]


def test_replan_remainder_confirm_blocked_when_active_buy_missing_frozen_price(monkeypatch):
    collections, finance, _plan = _lineage_cash_remainder_setup(
        monkeypatch,
        active_buy={
            "order_id": "live-plan-active",
            "user_id": "user-1",
            "plan_id": "plan-user-1",
            "mode": "live",
            "action": "buy",
            "status": "submitted",
            "size": 100,
            "filled_qty": 0,
            "symbol": "000002",
        },
    )

    with pytest.raises(portfolio_plans.HTTPException) as exc:
        asyncio.run(
            portfolio_plans.replan_portfolio_plan_remainder(
                "plan-user-1",
                portfolio_plans.ReplanRemainderRequest(securities_account_id="acct-1", dry_run=False),
                current_user={"id": "user-1"},
            )
        )

    assert exc.value.status_code == 409
    assert "missing_frozen_price" in exc.value.detail["cash_blockers"]
    # Nothing inserted: only the seed original + active buy remain.
    assert all(doc.get("source") != "plan_topup" for doc in finance["trade_signals"].docs)


def test_replan_remainder_reports_frozen_cash_when_active_buy_blocks_symbol(monkeypatch):
    collections, finance, _plan = _lineage_cash_remainder_setup(
        monkeypatch,
        active_buy={
            "order_id": "live-plan-active",
            "user_id": "user-1",
            "plan_id": "plan-user-1",
            "mode": "live",
            "action": "buy",
            "status": "submitted",
            "size": 600,
            "filled_qty": 0,
            "symbol": "000001",
            "effective_limit_price": 10.0,
        },
    )

    response = asyncio.run(
        portfolio_plans.replan_portfolio_plan_remainder(
            "plan-user-1",
            portfolio_plans.ReplanRemainderRequest(securities_account_id="acct-1", dry_run=True),
            current_user={"id": "user-1"},
        )
    )

    data = response["data"]
    assert data["frozen_buy_cash"] == 6_000
    assert data["plan_available_cash"] == 90_000
    assert data["actionable_count"] == 0
    assert "live_signal_active" in data["remainder"][0]["reasons"]


def _patch_lineage_live_env(monkeypatch, cols, fin, data):
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: cols)
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: fin)
    monkeypatch.setattr(portfolio_plans, "get_data_db", lambda: data)
    monkeypatch.setattr(portfolio_plans, "_attach_signal_names", lambda _dc, sigs: sigs)


def test_resilient_record_kind_prefers_stamp_then_derives_then_none():
    # Stamped value wins.
    assert portfolio_plans._resilient_record_kind({"record_kind": "observation", "plan_type": "rebalance"}) == "observation"
    # External manual ids are ledger events even if legacy data was wrongly
    # stamped as a trade_plan.
    assert (
        portfolio_plans._resilient_record_kind(
            {
                "plan_id": "growth_cycle_index_enhanced_hash_user_extman_20260624192646767ef3",
                "record_kind": "trade_plan",
                "plan_type": "rebalance",
            }
        )
        == "ledger_event"
    )
    # Missing stamp (e.g. a worker still on pre-stamp code) -> derive from shape.
    assert portfolio_plans._resilient_record_kind({"plan_type": "rebalance"}) == "trade_plan"
    assert portfolio_plans._resilient_record_kind({"plan_type": "monitor"}) == "observation"
    assert portfolio_plans._resilient_record_kind({"manual_action": "external_manual", "plan_type": "rebalance"}) == "ledger_event"
    # Genuinely unclassifiable -> None (never raises, so the list view survives).
    assert portfolio_plans._resilient_record_kind({"plan_id": "x"}) is None


def test_list_portfolios_survives_unstamped_plan(monkeypatch):
    # A freshly generated plan that missed record_kind stamping must NOT 500 the
    # whole portfolios list; it is counted via derivation instead.
    cols = _collections()
    fin = _finance_collections()
    data = _data_collections()
    cols["portfolio_trade_plans"] = _Collection(
        [
            {"plan_id": "stamped", "strategy_template_id": "stx", "user_id": "user-1", "base_date": "20250101", "status": "executed_paper", "params_hash": "hx", "plan_type": "rebalance", "record_kind": "trade_plan"},
            {"plan_id": "unstamped", "strategy_template_id": "stx", "user_id": "user-1", "base_date": "20250125", "status": "executed_paper", "params_hash": "hx", "plan_type": "rebalance"},
        ]
    )
    _patch_lineage_live_env(monkeypatch, cols, fin, data)
    resp = asyncio.run(portfolio_plans.list_portfolios(current_user={"id": "user-1"}))
    ports = resp["data"]["portfolios"]
    assert len(ports) == 1
    assert ports[0]["plan_count"] == 2


def test_list_live_portfolios_finds_lineage_with_live_signals(monkeypatch):
    cols = _collections()
    fin = _finance_collections()
    data = _data_collections()
    cols["portfolio_trade_plans"] = _Collection(
        [
            {"plan_id": "pla", "strategy_template_id": "stx", "user_id": "user-1", "base_date": "20250101", "status": "approved", "params_hash": "hx", "strategy_name": "L1", "record_kind": "trade_plan"},
            {"plan_id": "plb", "strategy_template_id": "stx", "user_id": "user-1", "base_date": "20250125", "status": "approved", "params_hash": "hx", "strategy_name": "L1", "record_kind": "trade_plan"},
            {"plan_id": "plz", "strategy_template_id": "sty", "user_id": "user-1", "base_date": "20250110", "status": "approved", "params_hash": "hy", "strategy_name": "NoLive", "record_kind": "trade_plan"},
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
    trades = resp["data"]["trades"]
    assert len(trades) == 2
    assert {trade["symbol"] for trade in trades} == {"000001.SZ", "600000.SH"}
    assert resp["data"]["execution_count"] == 2


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


def test_get_portfolio_plan_live_summary_consolidates_payload(monkeypatch):
    cols = _collections()
    fin = _finance_collections()
    data = _data_collections()
    cols["portfolio_trade_plans"] = _Collection(
        [
            {
                "plan_id": "pla",
                "strategy_template_id": "stx",
                "user_id": "user-1",
                "base_date": "20250101",
                "status": "approved",
                "params_hash": "hx",
                "params_snapshot": {"initial_capital": 1_000_000},
            }
        ]
    )
    fin["trade_signals"].docs = [
        {
            "order_id": "oa",
            "user_id": "user-1",
            "mode": "live",
            "plan_id": "pla",
            "symbol": "000001.SZ",
            "name": "Ping",
            "securities_account_id": "acct-1",
            "created_at": 1,
        }
    ]
    fin["trade_executions"].docs = [
        {
            "order_id": "oa",
            "user_id": "user-1",
            "securities_account_id": "acct-1",
            "symbol": "000001.SZ",
            "side": "BUY",
            "filled_size": 100,
            "filled_price": 10.0,
            "timestamp": 1.0,
        }
    ]
    fin["trader_positions"].docs = [
        {"user_id": "user-1", "securities_account_id": "acct-1", "symbol": "000001", "qty": 100}
    ]
    _patch_lineage_live_env(monkeypatch, cols, fin, data)
    resp = asyncio.run(portfolio_plans.get_portfolio_plan_live_summary("pla", current_user={"id": "user-1"}))
    payload = resp["data"]
    assert payload["positions"]["positions"][0]["shares"] == 100
    assert payload["trades"]
    assert payload["equity"]["current_book_equity"]["initial_capital"] == 1_000_000
    assert payload["reconcile"]["applicable"] is True


def test_list_portfolios_includes_live_and_paper_lineages(monkeypatch):
    cols = _collections()
    fin = _finance_collections()
    data = _data_collections()
    cols["portfolio_trade_plans"] = _Collection(
        [
            {
                "plan_id": "live-a",
                "record_kind": "trade_plan",
                "strategy_template_id": "st-live",
                "user_id": "user-1",
                "base_date": "20250120",
                "status": "executed_paper",
                "params_hash": "hx-live",
                "strategy_name": "LiveCombo",
                "params_snapshot": {"top_n": 20, "rebalance_days": 20, "construction_mode": "top_n", "universe_index": "csi1000"},
            },
            {
                "plan_id": "live-b",
                "record_kind": "trade_plan",
                "strategy_template_id": "st-live",
                "user_id": "user-1",
                "base_date": "20250125",
                "status": "executed_paper",
                "params_hash": "hx-live",
                "strategy_name": "LiveCombo",
                "params_snapshot": {"top_n": 20, "rebalance_days": 20, "construction_mode": "top_n", "universe_index": "csi1000"},
            },
            {
                "plan_id": "paper-a",
                "record_kind": "trade_plan",
                "strategy_template_id": "st-paper",
                "user_id": "user-1",
                "base_date": "20250118",
                "status": "executed_paper",
                "params_hash": "hx-paper",
                "strategy_name": "PaperOnly",
                "params_snapshot": {"top_n": 30, "rebalance_days": 10, "construction_mode": "industry_capped", "universe_index": "csi500"},
            },
            {
                "plan_id": "paper-b",
                "record_kind": "trade_plan",
                "strategy_template_id": "st-paper",
                "user_id": "user-1",
                "base_date": "20250122",
                "status": "partially_executed",
                "params_hash": "hx-paper",
                "strategy_name": "PaperOnly",
                "params_snapshot": {"top_n": 30, "rebalance_days": 10, "construction_mode": "industry_capped", "universe_index": "csi500"},
            },
            {
                "plan_id": "inactive",
                "record_kind": "trade_plan",
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
    assert live["execution_venue"] == "live"
    assert live["latest_plan_id"] == "live-b"
    assert live["param_summary"] == "csi1000 · Top20 · 20d · top_n"
    assert paper["execution_venue"] == "paper"
    assert paper["latest_plan_id"] == "paper-b"
    assert paper["param_summary"] == "csi500 · Top30 · 10d · industry_capped"


def test_list_portfolios_treats_unfilled_live_signals_as_live(monkeypatch):
    cols = _collections()
    fin = _finance_collections()
    data = _data_collections()
    cols["portfolio_trade_plans"] = _Collection(
        [
            {
                "plan_id": "signal-only",
                "record_kind": "trade_plan",
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
    assert ports[0]["execution_venue"] == "live"
    assert ports[0]["securities_account_id"] == "acct-1"


class _RaisingSignalCollection(_Collection):
    """Signals collection that fails if scanned, proving the caller reads the
    materialized lineage control instead of replaying signals/executions."""

    def find(self, query=None, projection=None):
        raise AssertionError("signals must not be scanned when control is materialized")

    def distinct(self, key, query=None):
        raise AssertionError("signals must not be distinct-scanned when control is materialized")


def test_list_portfolios_reads_venue_and_account_from_control_without_replay(monkeypatch):
    cols = _collections()
    fin = _finance_collections()
    data = _data_collections()
    cols["portfolio_trade_plans"] = _Collection(
        [
            {"plan_id": "pl1", "strategy_template_id": "stc", "user_id": "user-1", "base_date": "20250101", "status": "approved", "params_hash": "hc", "record_kind": "trade_plan"},
        ]
    )
    cols["portfolio_lineage_controls"] = _Collection(
        [
            {"user_id": "user-1", "strategy_template_id": "stc", "params_hash": "hc", "execution_venue": "live", "securities_account_id": "acct-mat"},
        ]
    )
    fin["trade_signals"] = _RaisingSignalCollection()
    fin["trade_executions"] = _FailingFindCollection()
    _patch_lineage_live_env(monkeypatch, cols, fin, data)
    resp = asyncio.run(portfolio_plans.list_portfolios(current_user={"id": "user-1"}))
    ports = resp["data"]["portfolios"]
    assert len(ports) == 1
    assert ports[0]["execution_venue"] == "live"
    assert ports[0]["securities_account_id"] == "acct-mat"


def test_lineage_has_live_fills_uses_existence_probe(monkeypatch):
    cols = _collections()
    fin = _finance_collections()
    cols["portfolio_trade_plans"] = _Collection(
        [{"plan_id": "pl1", "strategy_template_id": "stf", "user_id": "user-1", "base_date": "20250101", "params_hash": "hf"}]
    )
    fin["trade_signals"].docs = [{"order_id": "o1", "user_id": "user-1", "mode": "live", "plan_id": "pl1"}]
    fin["trade_executions"].docs = [{"order_id": "o1", "user_id": "user-1", "filled_size": 100}]
    plan = {"plan_id": "pl1", "strategy_template_id": "stf", "params_hash": "hf"}
    assert portfolio_plans._lineage_has_live_fills(cols, fin, plan, "user-1") is True
    fin["trade_executions"].docs = [{"order_id": "o1", "user_id": "user-1", "filled_size": 0}]
    assert portfolio_plans._lineage_has_live_fills(cols, fin, plan, "user-1") is False


def test_lineage_live_account_id_prefers_control(monkeypatch):
    cols = _collections()
    fin = _finance_collections()
    cols["portfolio_trade_plans"] = _Collection(
        [{"plan_id": "pl1", "strategy_template_id": "sta", "user_id": "user-1", "base_date": "20250101", "params_hash": "ha"}]
    )
    cols["portfolio_lineage_controls"] = _Collection(
        [{"user_id": "user-1", "strategy_template_id": "sta", "params_hash": "ha", "securities_account_id": "acct-ctrl"}]
    )
    fin["trade_signals"] = _RaisingSignalCollection()
    fin["trade_executions"] = _FailingFindCollection()
    plan = {"plan_id": "pl1", "strategy_template_id": "sta", "params_hash": "ha"}
    assert portfolio_plans._lineage_live_account_id_from_fills(cols, fin, plan, "user-1") == "acct-ctrl"


def test_lineage_paper_positions_uses_latest_snapshot(monkeypatch):
    cols = _collections()
    fin = _finance_collections()
    data = _data_collections()
    cols["portfolio_trade_plans"] = _Collection(
        [
            {
                "plan_id": "paper-old",
                "strategy_template_id": "stx",
                "user_id": "user-1",
                "base_date": "20250110",
                "status": "executed_paper",
                "params_hash": "hx",
            },
            {
                "plan_id": "paper-new",
                "strategy_template_id": "stx",
                "user_id": "user-1",
                "base_date": "20250120",
                "status": "generated",
                "params_hash": "hx",
                "plan_type": "monitor",
            },
        ]
    )
    cols["portfolio_paper_positions"].docs = [
        {
            "strategy_template_id": "stx",
            "user_id": "user-1",
            "params_hash": "hx",
            "plan_id": "paper-old",
            "as_of_date": "20250111",
            "equity": 1_000_000,
            "cash": 100_000,
            "positions": [
                {"symbol": "000001.SZ", "name": "Ping", "shares": 100, "cost_basis": 10.0, "mark_price": 11.0, "market_value": 1100},
            ],
        },
    ]
    _patch_lineage_live_env(monkeypatch, cols, fin, data)
    resp = asyncio.run(
        portfolio_plans.get_lineage_paper_positions("paper-new", current_user={"id": "user-1"})
    )
    assert resp["data"]["as_of_date"] == "20250111"
    assert resp["data"]["source_plan_id"] == "paper-old"
    assert len(resp["data"]["positions"]) == 1
    assert resp["data"]["positions"][0]["shares"] == 100


def test_lineage_paper_positions_breaks_as_of_date_ties_by_updated_at(monkeypatch):
    # Same-day intraday rebalance: a manual snapshot shares as_of_date with the
    # morning strategy mark and must win the tie via the newer updated_at, so the
    # holdings view reflects the rebalance instead of the stale strategy snapshot.
    cols = _collections()
    fin = _finance_collections()
    data = _data_collections()
    cols["portfolio_trade_plans"] = _Collection(
        [
            {
                "plan_id": "paper-anchor",
                "strategy_template_id": "st-tie",
                "user_id": "user-1",
                "base_date": "20250120",
                "status": "generated",
                "params_hash": "hx-tie",
            },
        ]
    )
    cols["portfolio_paper_positions"].docs = [
        {
            "strategy_template_id": "st-tie",
            "user_id": "user-1",
            "params_hash": "hx-tie",
            "plan_id": "paper-strategy-mark",
            "as_of_date": "20250120",
            "equity": 1_000_000,
            "cash": 100_000,
            "updated_at": datetime(2025, 1, 20, 10, 30, 0),
            "positions": [
                {"symbol": "000001.SZ", "name": "Ping", "shares": 100, "cost_basis": 10.0, "mark_price": 11.0, "market_value": 1100},
            ],
        },
        {
            "strategy_template_id": "st-tie",
            "user_id": "user-1",
            "params_hash": "hx-tie",
            "plan_id": "paper-manual-rebalance",
            "as_of_date": "20250120",
            "equity": 1_050_000,
            "cash": 1_050_000,
            "updated_at": datetime(2025, 1, 20, 14, 45, 0),
            "positions": [],
        },
    ]
    _patch_lineage_live_env(monkeypatch, cols, fin, data)
    resp = asyncio.run(
        portfolio_plans.get_lineage_paper_positions("paper-anchor", current_user={"id": "user-1"})
    )
    assert resp["data"]["as_of_date"] == "20250120"
    assert resp["data"]["source_plan_id"] == "paper-manual-rebalance"
    assert resp["data"]["positions"] == []
    assert resp["data"]["summary"]["holding_count"] == 0


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
                "record_kind": "trade_plan",
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
                "record_kind": "observation",
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
                "record_kind": "trade_plan",
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
            "name": "PingAn",
            "rank": 1,
            "action": "buy",
            "delta_shares": 50,
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
    assert monitor_data["operation_plan"]["plan_id"] == "reb-2"
    assert monitor_data["operation_plan"]["status"] == "needs_review"
    drift = monitor_data["latest_drift_summary"]
    assert drift["plan_id"] == "reb-2"
    assert drift["buy_count"] == 1
    assert drift["estimated_buy_amount"] == 500.0
    monitor_node = next(row for row in monitor_data["timeline"] if row["plan_id"] == "mon-1")
    assert monitor_node["today_state"] == "观察日：不交易"
    assert monitor_node["action_required"] is False
    assert monitor_node["drift_brief"]["buy_count"] == 1
    assert monitor_node["drift_brief"]["sell_count"] == 1
    assert monitor_node["drift_brief"]["mode"] == "drift"
    assert len(monitor_node["drift_brief"]["items"]) == 2
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
    assert review_data["operation_plan"]["plan_id"] == "reb-2"
    assert len(review_data["timeline"]) == 3
    assert review_data["timeline"][0]["plan_id"] == "reb-2"
    reb2_brief = review_data["timeline"][0]["drift_brief"]
    assert reb2_brief["mode"] == "executable"
    assert len(reb2_brief["items"]) == 1
    assert reb2_brief["items"][0]["symbol"] == "000001.SZ"
    assert reb2_brief["items"][0]["delta_shares"] == 50


def test_lineage_timeline_ignores_manual_records_for_rebalance_cadence(monkeypatch):
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
                "status": "executed",
                "params_hash": "hx",
                "strategy_name": "Combo",
                "origin": "strategy",
                "plan_type": "rebalance",
                "record_kind": "trade_plan",
                "is_rebalance_day": True,
                "params_snapshot": {"rebalance_days": 10},
                "summary": {"equity": 1_000_000},
                "executable": True,
            },
            {
                "plan_id": "manual-clear",
                "strategy_template_id": "stx",
                "user_id": "user-1",
                "base_date": "20250110",
                "status": "filled",
                "params_hash": "hx",
                "strategy_name": "Combo",
                "origin": "manual",
                "manual_action": "external_manual",
                "plan_type": "rebalance",
                "record_kind": "ledger_event",
                "is_rebalance_day": True,
                "is_liquidation": True,
                "params_snapshot": {"rebalance_days": 10},
                "summary": {"equity": 1_000_000},
                "executable": False,
            },
        ]
    )
    _patch_lineage_live_env(monkeypatch, cols, fin, data)

    resp = asyncio.run(
        portfolio_plans.get_portfolio_plan_lineage_timeline(
            "manual-clear", limit=60, offset=0, current_user={"id": "user-1"}
        )
    )

    payload = resp["data"]
    assert payload["today_state"] == "已补录手工清仓"
    assert payload["current_cycle"]["latest_rebalance_plan_id"] == "reb-1"
    # 20250102 anchor + 10 trading days (now that the fake Mongo honours $gt).
    assert payload["current_cycle"]["next_rebalance_estimate"] == "20250112"
    manual_node = next(row for row in payload["timeline"] if row["plan_id"] == "manual-clear")
    assert manual_node["node_type"] == "manual"


def test_lineage_timeline_manual_record_does_not_mask_pending_rebalance(monkeypatch):
    cols = _collections()
    fin = _finance_collections()
    data = _data_collections()
    data["volume_price"].docs = [{"trade_date": f"202501{d:02d}"} for d in range(1, 32)]
    cols["portfolio_trade_plans"] = _Collection(
        [
            {
                "plan_id": "reb-pending",
                "strategy_template_id": "stx",
                "user_id": "user-1",
                "base_date": "20250110",
                "status": "needs_review",
                "params_hash": "hx",
                "strategy_name": "Combo",
                "origin": "strategy",
                "plan_type": "rebalance",
                "record_kind": "trade_plan",
                "is_rebalance_day": True,
                "params_snapshot": {"rebalance_days": 10},
                "summary": {"equity": 1_000_000},
                "executable": True,
            },
            {
                "plan_id": "manual-same-day",
                "strategy_template_id": "stx",
                "user_id": "user-1",
                "base_date": "20250110",
                "status": "filled",
                "params_hash": "hx",
                "strategy_name": "Combo",
                "origin": "manual",
                "manual_action": "external_manual",
                "plan_type": "manual",
                "record_kind": "ledger_event",
                "is_rebalance_day": False,
                "is_liquidation": False,
                "params_snapshot": {"rebalance_days": 10},
                "summary": {"equity": 1_000_000},
                "executable": False,
            },
        ]
    )
    _patch_lineage_live_env(monkeypatch, cols, fin, data)

    resp = asyncio.run(
        portfolio_plans.get_portfolio_plan_lineage_timeline(
            "manual-same-day", limit=60, offset=0, current_user={"id": "user-1"}
        )
    )

    payload = resp["data"]
    # A same-day manual ledger record must not hide a rebalance awaiting review.
    assert payload["action_required"] is True
    assert "手工" not in payload["today_state"]
    assert payload["operation_plan"]["plan_id"] == "reb-pending"
    assert payload["operation_plan"]["status"] == "needs_review"


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
                "record_kind": "observation",
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
                "record_kind": "observation",
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


def test_lineage_timeline_exposes_record_kind_and_treats_manual_rebalance_as_plan(monkeypatch):
    cols = _collections()
    fin = _finance_collections()
    data = _data_collections()
    data["volume_price"].docs = [{"trade_date": f"202501{d:02d}"} for d in range(1, 32)]
    cols["portfolio_trade_plans"] = _Collection(
        [
            {
                "plan_id": "manual-reb",
                "strategy_template_id": "stx",
                "user_id": "user-1",
                "base_date": "20250115",
                "status": "needs_review",
                "params_hash": "hx",
                "strategy_name": "Combo",
                "origin": "manual",
                "manual_action": "rebalance",
                "plan_type": "rebalance",
                "record_kind": "trade_plan",
                "is_rebalance_day": True,
                "params_snapshot": {"rebalance_days": 20},
                "summary": {"equity": 1_000_000},
                "executable": True,
            },
        ]
    )
    _patch_lineage_live_env(monkeypatch, cols, fin, data)
    resp = asyncio.run(
        portfolio_plans.get_portfolio_plan_lineage_timeline(
            "manual-reb", limit=60, offset=0, current_user={"id": "user-1"}
        )
    )
    payload = resp["data"]
    # A manual rebalance is a trade_plan, so it renders as a "rebalance" node
    # (not a "manual" ledger node) and carries its record_kind explicitly.
    node = payload["timeline"][0]
    assert node["record_kind"] == "trade_plan"
    assert node["node_type"] == "rebalance"
    assert payload["operation_plan"]["record_kind"] == "trade_plan"


def test_lineage_timeline_fails_loud_when_record_kind_missing(monkeypatch):
    cols = _collections()
    fin = _finance_collections()
    data = _data_collections()
    cols["portfolio_trade_plans"] = _Collection(
        [
            {
                "plan_id": "no-kind",
                "strategy_template_id": "stx",
                "user_id": "user-1",
                "base_date": "20250115",
                "status": "needs_review",
                "params_hash": "hx",
                "strategy_name": "Combo",
                "plan_type": "rebalance",
                "is_rebalance_day": True,
                "params_snapshot": {"rebalance_days": 20},
                "summary": {"equity": 1_000_000},
                "executable": True,
            },
        ]
    )
    _patch_lineage_live_env(monkeypatch, cols, fin, data)
    with pytest.raises(ValueError):
        asyncio.run(
            portfolio_plans.get_portfolio_plan_lineage_timeline(
                "no-kind", limit=60, offset=0, current_user={"id": "user-1"}
            )
        )


def test_list_portfolios_includes_param_summary_and_paper_holdings(monkeypatch):
    cols = _collections()
    fin = _finance_collections()
    data = _data_collections()
    cols["portfolio_trade_plans"] = _Collection(
        [
            {
                "plan_id": "paper-a",
                "strategy_template_id": "st-paper",
                "user_id": "user-1",
                "base_date": "20250118",
                "status": "executed_paper",
                "params_hash": "hx-a",
                "strategy_name": "ComboA",
                "plan_type": "rebalance",
                "record_kind": "trade_plan",
                "params_snapshot": {
                    "universe_index": "csi1000",
                    "top_n": 20,
                    "rebalance_days": 20,
                    "construction_mode": "top_n",
                },
            },
            {
                "plan_id": "paper-b",
                "strategy_template_id": "st-paper",
                "user_id": "user-1",
                "base_date": "20250125",
                "status": "executed_paper",
                "params_hash": "hx-b",
                "strategy_name": "ComboB",
                "plan_type": "monitor",
                "record_kind": "observation",
                "params_snapshot": {
                    "universe_index": "csi500",
                    "top_n": 30,
                    "rebalance_days": 10,
                    "construction_mode": "industry_capped",
                },
            },
        ]
    )
    cols["portfolio_paper_positions"].docs = [
        {
            "strategy_template_id": "st-paper",
            "user_id": "user-1",
            "params_hash": "hx-a",
            "plan_id": "paper-a",
            "as_of_date": "20250118",
            "equity": 1_000_000,
            "cash": 100_000,
            "positions": [
                {"symbol": "000001.SZ", "name": "Ping", "shares": 100, "cost_basis": 10.0, "mark_price": 11.0, "market_value": 1100},
            ],
        },
    ]
    _patch_lineage_live_env(monkeypatch, cols, fin, data)
    resp = asyncio.run(portfolio_plans.list_portfolios(current_user={"id": "user-1"}))
    ports = {row["params_hash"]: row for row in resp["data"]["portfolios"]}
    assert ports["hx-a"]["param_summary"] == "csi1000 · Top20 · 20d · top_n"
    assert "paper_holding_count" not in ports["hx-a"]
    assert "paper_execution_mode" not in ports["hx-a"]
    assert ports["hx-b"]["param_summary"] == "csi500 · Top30 · 10d · industry_capped"
    summary_resp = asyncio.run(
        portfolio_plans.get_portfolio_plan_summary("paper-a", current_user={"id": "user-1"})
    )
    summary = summary_resp["data"]
    assert summary["paper_holding_count"] == 1
    assert summary["paper_execution_mode"] == "auto_shadow"


def test_lineage_paper_positions_returns_latest_snapshot(monkeypatch):
    cols = _collections()
    fin = _finance_collections()
    data = _data_collections()
    cols["portfolio_trade_plans"] = _Collection(
        [
            {
                "plan_id": "paper-a",
                "strategy_template_id": "st-paper",
                "user_id": "user-1",
                "base_date": "20250118",
                "status": "executed_paper",
                "params_hash": "hx-a",
            },
            {
                "plan_id": "paper-b",
                "strategy_template_id": "st-paper",
                "user_id": "user-1",
                "base_date": "20250125",
                "status": "generated",
                "params_hash": "hx-a",
            },
        ]
    )
    cols["portfolio_paper_positions"].docs = [
        {
            "strategy_template_id": "st-paper",
            "user_id": "user-1",
            "params_hash": "hx-a",
            "plan_id": "paper-a",
            "as_of_date": "20250118",
            "equity": 1_000_000,
            "cash": 100_000,
            "positions": [
                {"symbol": "000001.SZ", "name": "Ping", "shares": 100, "cost_basis": 10.0, "mark_price": 11.0, "market_value": 1100},
            ],
        },
    ]
    _patch_lineage_live_env(monkeypatch, cols, fin, data)
    resp = asyncio.run(portfolio_plans.get_lineage_paper_positions("paper-b", current_user={"id": "user-1"}))
    data = resp["data"]
    assert data["as_of_date"] == "20250118"
    assert data["source_plan_id"] == "paper-a"
    assert len(data["positions"]) == 1
    assert data["summary"]["holding_count"] == 1


def test_lineage_paper_executions_returns_fifo_trades(monkeypatch):
    cols = _collections()
    fin = _finance_collections()
    data = _data_collections()
    cols["portfolio_trade_plans"] = _Collection(
        [
            {
                "plan_id": "paper-a",
                "strategy_template_id": "st-paper",
                "user_id": "user-1",
                "base_date": "20250106",
                "plan_type": "rebalance",
                "status": "executed_paper",
                "params_hash": "hx-a",
                "execute_date": "20250107",
            },
            {
                "plan_id": "paper-b",
                "strategy_template_id": "st-paper",
                "user_id": "user-1",
                "base_date": "20250120",
                "plan_type": "rebalance",
                "status": "executed_paper",
                "params_hash": "hx-a",
                "execute_date": "20250121",
            },
        ]
    )
    # Trade ledger is derived from the actual paper fills, so buy/sell prices
    # match each holding's cost basis.
    cols["portfolio_paper_executions"].docs = [
        {
            "execution_id": "ex-1",
            "plan_id": "paper-a",
            "user_id": "user-1",
            "symbol": "000001.SZ",
            "name": "Ping",
            "action": "buy",
            "quantity": 100,
            "price": 10.0,
            "amount": 1000.0,
            "execute_date": "20250107",
            "created_at": "2025-01-07T01:30:00",
        },
        {
            "execution_id": "ex-2",
            "plan_id": "paper-b",
            "user_id": "user-1",
            "symbol": "000001.SZ",
            "name": "Ping",
            "action": "sell",
            "quantity": 40,
            "price": 12.0,
            "amount": 480.0,
            "execute_date": "20250121",
            "created_at": "2025-01-21T01:30:00",
        },
    ]
    _patch_lineage_live_env(monkeypatch, cols, fin, data)
    resp = asyncio.run(
        portfolio_plans.get_lineage_paper_executions("paper-b", current_user={"id": "user-1"})
    )
    payload = resp["data"]
    assert payload["trade_source"] == "paper_executions"
    assert payload["execution_count"] == 2
    assert payload["trade_count"] == 2
    statuses = {row["status"] for row in payload["trades"]}
    assert statuses == {"closed", "open"}
    closed = next(row for row in payload["trades"] if row["status"] == "closed")
    assert closed["quantity"] == 40
    assert closed["buy_price"] == 10.0
    assert closed["buy_date"] == "20250107"
    assert closed["sell_date"] == "20250121"
    assert closed["rebalance_date"] == "20250106"
    open_row = next(row for row in payload["trades"] if row["status"] == "open")
    assert open_row["quantity"] == 60
    assert open_row["buy_price"] == 10.0


def _live_rebalance_env():
    """Lineage (plan 'pla') holding 500 shares of 600000.SH from a live buy fill."""
    cols = _collections()
    fin = _finance_collections()
    data = _data_collections()
    data["volume_price_realtime_latest"] = _Collection(
        [
            {"symbol": "600000", "close": 12.0, "trade_date": "20250131"},
            {"symbol": "600000.SH", "close": 12.0, "trade_date": "20250131"},
        ]
    )
    cols["portfolio_trade_plans"] = _Collection(
        [
            {
                "plan_id": "pla",
                "strategy_template_id": "stx",
                "user_id": "user-1",
                "params_hash": "hx",
                "base_date": "20250101",
                "status": "approved",
                "strategy_name": "L1",
                "params_snapshot": {"initial_capital": 1_000_000, "lot_size": 100},
            }
        ]
    )
    fin["trade_signals"].docs = [
        {"order_id": "o-buy-1", "user_id": "user-1", "mode": "live", "plan_id": "pla", "symbol": "600000.SH", "name": "浦发银行", "created_at": 1},
    ]
    fin["trade_executions"].docs = [
        {"order_id": "o-buy-1", "user_id": "user-1", "symbol": "600000.SH", "side": "BUY", "status": "filled", "filled_size": 500, "filled_price": 11.0, "timestamp": 1.0},
    ]
    # Broker can sell the full 500 shares -> no insufficient_available_position.
    fin["trader_positions"].docs = [
        {"user_id": "user-1", "securities_account_id": "acct-1", "symbol": "600000.SH", "qty": 500, "can_use_volume": 500}
    ]
    return cols, fin, data


def test_live_rebalance_dry_run_builds_sell_signals(monkeypatch):
    cols, fin, data = _live_rebalance_env()
    _patch_lineage_live_env(monkeypatch, cols, fin, data)

    resp = asyncio.run(
        portfolio_plans.live_rebalance_portfolio(
            "pla",
            portfolio_plans.LiveRebalanceRequest(
                securities_account_id="acct-1", targets={"600000.SH": 0}, dry_run=True
            ),
            current_user={"id": "user-1"},
        )
    )

    data_out = resp["data"]
    assert data_out["dry_run"] is True
    assert data_out["manual_action"] == "liquidate"
    assert data_out["changed_symbols"] == ["600000.SH"]
    assert len(data_out["signals"]) == 1
    signal = data_out["signals"][0]
    assert signal["action"] == "sell"
    assert signal["size"] == 500
    assert signal["mode"] == "live"
    assert signal["execution_phase"] == "sell"
    # Nothing persisted on a dry run: only the seed buy signal remains.
    assert len(fin["trade_signals"].docs) == 1
    assert fin["trade_signals"].docs[0]["order_id"] == "o-buy-1"
    assert not [p for p in cols["portfolio_trade_plans"].docs if p.get("origin") == "manual"]


def test_live_rebalance_skips_sell_when_account_position_zero(monkeypatch):
    cols, fin, data = _live_rebalance_env()
    fin["trader_positions"].docs = [
        {"user_id": "user-1", "securities_account_id": "acct-1", "symbol": "600000.SH", "qty": 0, "can_use_volume": 0}
    ]
    _patch_lineage_live_env(monkeypatch, cols, fin, data)

    with pytest.raises(portfolio_plans.HTTPException) as exc:
        asyncio.run(
            portfolio_plans.live_rebalance_portfolio(
                "pla",
                portfolio_plans.LiveRebalanceRequest(
                    securities_account_id="acct-1", targets={"600000.SH": 0}, dry_run=True
                ),
                current_user={"id": "user-1"},
            )
        )

    assert exc.value.status_code == 400
    assert "no executable changes" in exc.value.detail


def test_live_rebalance_caps_sell_to_account_available(monkeypatch):
    cols, fin, data = _live_rebalance_env()
    fin["trader_positions"].docs = [
        {"user_id": "user-1", "securities_account_id": "acct-1", "symbol": "600000.SH", "qty": 200, "can_use_volume": 200}
    ]
    _patch_lineage_live_env(monkeypatch, cols, fin, data)

    resp = asyncio.run(
        portfolio_plans.live_rebalance_portfolio(
            "pla",
            portfolio_plans.LiveRebalanceRequest(
                securities_account_id="acct-1", targets={"600000.SH": 0}, dry_run=True
            ),
            current_user={"id": "user-1"},
        )
    )

    data_out = resp["data"]
    signal = data_out["signals"][0]
    assert signal["action"] == "sell"
    assert signal["size"] == 200
    item = data_out["items"][0]
    assert item["delta_shares"] == -200
    assert item["target_shares"] == 300
    assert item["account_current_shares"] == 200
    assert item["account_available_shares"] == 200
    assert "account_position_capped" in item["blockers"]


def test_live_rebalance_liquidation_publishes_and_creates_manual_plan(monkeypatch):
    cols, fin, data = _live_rebalance_env()
    _patch_lineage_live_env(monkeypatch, cols, fin, data)

    resp = asyncio.run(
        portfolio_plans.live_rebalance_portfolio(
            "pla",
            portfolio_plans.LiveRebalanceRequest(
                securities_account_id="acct-1", targets={"600000.SH": 0}, dry_run=False
            ),
            current_user={"id": "user-1"},
        )
    )

    data_out = resp["data"]
    assert data_out["dry_run"] is False
    assert data_out["inserted_count"] == 1
    # Seed buy signal + the new sell signal.
    assert len(fin["trade_signals"].docs) == 2
    new_sell = next(d for d in fin["trade_signals"].docs if d.get("action") == "sell")
    assert new_sell["size"] == 500
    assert new_sell["mode"] == "live"
    # Manual liquidation plan + item persisted for audit.
    manual_plans = [p for p in cols["portfolio_trade_plans"].docs if p.get("origin") == "manual"]
    assert len(manual_plans) == 1
    plan = manual_plans[0]
    assert plan["manual_action"] == "liquidate"
    assert plan["is_liquidation"] is True
    assert plan["status"] == "approved"
    assert plan["plan_type"] == "rebalance"
    assert plan["live_signals_published_at"]
    items = [i for i in cols["portfolio_trade_plan_items"].docs if i["plan_id"] == plan["plan_id"]]
    assert len(items) == 1
    assert items[0]["action"] == "sell"
    assert items[0]["target_shares"] == 0
    assert items[0]["delta_shares"] == -500


def test_external_manual_record_full_sell_clears_book_without_pausing_by_default(monkeypatch):
    cols, fin, data = _live_rebalance_env()
    _patch_lineage_live_env(monkeypatch, cols, fin, data)

    resp = asyncio.run(
        portfolio_plans.record_external_manual_record(
            "pla",
            portfolio_plans.ExternalManualRecordRequest(
                securities_account_id="acct-1",
                fills=[
                    portfolio_plans.ExternalManualFill(
                        symbol="600000.SH",
                        action="sell",
                        filled_size=500,
                        filled_price=12.2,
                        timestamp=2.0,
                        commission=5.0,
                        broker_order_id="qmt-manual-1",
                    )
                ],
                external_batch_id="manual-clear-20250131",
            ),
            current_user={"id": "user-1"},
        )
    )

    data_out = resp["data"]
    assert data_out["manual_action"] == "external_liquidation"
    assert data_out["is_liquidation"] is True
    assert data_out["inserted_signal_count"] == 1
    assert data_out["inserted_execution_count"] == 1
    assert data_out["lineage_effects"] == {}

    manual_plan = next(p for p in cols["portfolio_trade_plans"].docs if p.get("manual_action") == "external_manual")
    assert manual_plan["origin"] == "manual"
    assert manual_plan["plan_type"] == "manual"
    assert manual_plan["is_rebalance_day"] is False
    assert manual_plan["status"] == "filled"
    assert manual_plan["trading_enabled"] is False
    assert manual_plan["external_batch_id"] == "manual-clear-20250131"

    sell_signal = next(s for s in fin["trade_signals"].docs if s.get("source") == "external_manual_record")
    assert sell_signal["status"] == "filled"
    assert sell_signal["is_executable"] is False
    assert sell_signal["plan_id"] == manual_plan["plan_id"]

    sell_execution = next(e for e in fin["trade_executions"].docs if e.get("source") == "external_manual_record")
    assert sell_execution["side"] == "SELL"
    assert sell_execution["filled_size"] == 500
    assert sell_execution["broker_order_id"] == "qmt-manual-1"

    positions_resp = asyncio.run(
        portfolio_plans.get_lineage_live_positions("pla", current_user={"id": "user-1"})
    )
    positive_positions = [row for row in positions_resp["data"]["positions"] if row["shares"] > 0]
    assert positive_positions == []
    assert manual_plan["plan_id"] in positions_resp["data"]["lineage_plan_ids"]


def test_external_manual_record_partial_sell_keeps_remainder(monkeypatch):
    cols, fin, data = _live_rebalance_env()
    _patch_lineage_live_env(monkeypatch, cols, fin, data)

    resp = asyncio.run(
        portfolio_plans.record_external_manual_record(
            "pla",
            portfolio_plans.ExternalManualRecordRequest(
                securities_account_id="acct-1",
                fills=[
                    portfolio_plans.ExternalManualFill(
                        symbol="600000.SH",
                        action="sell",
                        filled_size=200,
                        filled_price=12.2,
                    )
                ],
            ),
            current_user={"id": "user-1"},
        )
    )

    data_out = resp["data"]
    assert data_out["manual_action"] == "external_manual"
    assert data_out["is_liquidation"] is False
    # Partial reduce leaves a position, so the lineage is not paused.
    assert "paused" not in (data_out["lineage_effects"] or {})

    positions_resp = asyncio.run(
        portfolio_plans.get_lineage_live_positions("pla", current_user={"id": "user-1"})
    )
    pos = next(row for row in positions_resp["data"]["positions"] if row["symbol"] == "600000.SH")
    assert pos["shares"] == 300


def test_external_manual_record_rejects_oversell(monkeypatch):
    cols, fin, data = _live_rebalance_env()
    _patch_lineage_live_env(monkeypatch, cols, fin, data)

    with pytest.raises(portfolio_plans.HTTPException) as excinfo:
        asyncio.run(
            portfolio_plans.record_external_manual_record(
                "pla",
                portfolio_plans.ExternalManualRecordRequest(
                    securities_account_id="acct-1",
                    fills=[
                        portfolio_plans.ExternalManualFill(
                            symbol="600000.SH",
                            action="sell",
                            filled_size=600,
                            filled_price=12.2,
                        )
                    ],
                ),
                current_user={"id": "user-1"},
            )
        )

    assert excinfo.value.status_code == 409
    assert not [p for p in cols["portfolio_trade_plans"].docs if p.get("manual_action") == "external_manual"]
    assert not [s for s in fin["trade_signals"].docs if s.get("source") == "external_manual_record"]


def test_live_rebalance_partial_reduce(monkeypatch):
    cols, fin, data = _live_rebalance_env()
    _patch_lineage_live_env(monkeypatch, cols, fin, data)

    resp = asyncio.run(
        portfolio_plans.live_rebalance_portfolio(
            "pla",
            portfolio_plans.LiveRebalanceRequest(
                securities_account_id="acct-1", targets={"600000.SH": 200}, dry_run=False
            ),
            current_user={"id": "user-1"},
        )
    )

    data_out = resp["data"]
    assert data_out["manual_action"] == "rebalance"
    sells = [d for d in fin["trade_signals"].docs if d.get("action") == "sell"]
    assert len(sells) == 1
    assert sells[0]["size"] == 300  # 500 -> 200
    plan = next(p for p in cols["portfolio_trade_plans"].docs if p.get("origin") == "manual")
    item = next(i for i in cols["portfolio_trade_plan_items"].docs if i["plan_id"] == plan["plan_id"])
    assert item["action"] == "sell"
    assert item["target_shares"] == 200
    assert item["delta_shares"] == -300


def test_live_rebalance_add_position_builds_buy(monkeypatch):
    cols, fin, data = _live_rebalance_env()
    _patch_lineage_live_env(monkeypatch, cols, fin, data)

    resp = asyncio.run(
        portfolio_plans.live_rebalance_portfolio(
            "pla",
            portfolio_plans.LiveRebalanceRequest(
                securities_account_id="acct-1", targets={"600000.SH": 700}, dry_run=False
            ),
            current_user={"id": "user-1"},
        )
    )

    data_out = resp["data"]
    assert data_out["manual_action"] == "rebalance"
    assert data_out["blocked"] is False
    buys = [d for d in fin["trade_signals"].docs if d.get("action") == "buy"]
    assert len(buys) == 1
    assert buys[0]["size"] == 200  # 500 -> 700
    item = next(
        i
        for i in cols["portfolio_trade_plan_items"].docs
        if i.get("action") == "buy"
    )
    assert item["target_shares"] == 700
    assert item["delta_shares"] == 200


def test_live_rebalance_hard_block_when_live_disabled(monkeypatch):
    cols, fin, data = _live_rebalance_env()
    _patch_lineage_live_env(monkeypatch, cols, fin, data)

    with pytest.raises(portfolio_plans.HTTPException) as excinfo:
        asyncio.run(
            portfolio_plans.live_rebalance_portfolio(
                "pla",
                portfolio_plans.LiveRebalanceRequest(
                    securities_account_id="acct-off", targets={"600000.SH": 0}, dry_run=False
                ),
                current_user={"id": "user-1"},
            )
        )

    assert excinfo.value.status_code == 409
    # Nothing persisted when blocked by account configuration.
    assert len(fin["trade_signals"].docs) == 1
    assert not [p for p in cols["portfolio_trade_plans"].docs if p.get("origin") == "manual"]


def test_live_rebalance_no_targets_returns_400(monkeypatch):
    cols, fin, data = _live_rebalance_env()
    _patch_lineage_live_env(monkeypatch, cols, fin, data)

    with pytest.raises(portfolio_plans.HTTPException) as excinfo:
        asyncio.run(
            portfolio_plans.live_rebalance_portfolio(
                "pla",
                portfolio_plans.LiveRebalanceRequest(securities_account_id="acct-1", targets={}, dry_run=False),
                current_user={"id": "user-1"},
            )
        )

    assert excinfo.value.status_code == 400


def test_live_rebalance_no_change_returns_400(monkeypatch):
    cols, fin, data = _live_rebalance_env()
    _patch_lineage_live_env(monkeypatch, cols, fin, data)

    with pytest.raises(portfolio_plans.HTTPException) as excinfo:
        asyncio.run(
            portfolio_plans.live_rebalance_portfolio(
                "pla",
                portfolio_plans.LiveRebalanceRequest(
                    securities_account_id="acct-1", targets={"600000.SH": 500}, dry_run=False
                ),
                current_user={"id": "user-1"},
            )
        )

    assert excinfo.value.status_code == 400


def _paper_rebalance_env():
    """Paper lineage (anchor 'pla') with a prior snapshot: 500 shares 600000.SH, 880k cash."""
    cols = _collections()
    fin = _finance_collections()
    data = _data_collections()
    data["volume_price_realtime_latest"] = _Collection(
        [
            {"symbol": "600000", "close": 12.0, "trade_date": "20250131"},
            {"symbol": "600000.SH", "close": 12.0, "trade_date": "20250131"},
            {"symbol": "000001", "close": 10.0, "trade_date": "20250131"},
            {"symbol": "000001.SZ", "close": 10.0, "trade_date": "20250131"},
        ]
    )
    cols["portfolio_trade_plans"] = _Collection(
        [
            {
                "plan_id": "pla",
                "strategy_template_id": "stx",
                "user_id": "user-1",
                "params_hash": "hx",
                "base_date": "20250101",
                "status": "executed_paper",
                "strategy_name": "L1",
                "params_snapshot": {"initial_capital": 1_000_000, "lot_size": 100},
            }
        ]
    )
    # Prior paper snapshot lives under a *different* plan_id in the same lineage so
    # _latest_lineage_position_snapshot (which excludes the anchor plan_id) sees it.
    cols["portfolio_paper_positions"] = _Collection(
        [
            {
                "plan_id": "pla-exec",
                "strategy_template_id": "stx",
                "user_id": "user-1",
                "params_hash": "hx",
                "as_of_date": "20250105",
                "cash": 880_000.0,
                "market_value": 120_000.0,
                "equity": 1_000_000.0,
                "positions": [{"symbol": "600000.SH", "name": "浦发银行", "shares": 500, "cost_basis": 11.0}],
            }
        ]
    )
    return cols, fin, data


def test_paper_rebalance_clear_dry_run_does_not_persist(monkeypatch):
    cols, fin, data = _paper_rebalance_env()
    _patch_lineage_live_env(monkeypatch, cols, fin, data)

    resp = asyncio.run(
        portfolio_plans.paper_rebalance_portfolio(
            "pla",
            portfolio_plans.PaperRebalanceRequest(targets={"600000.SH": 0}, dry_run=True),
            current_user={"id": "user-1"},
        )
    )

    data_out = resp["data"]
    assert data_out["dry_run"] is True
    assert data_out["manual_action"] == "liquidate"
    assert data_out["status"] == "executed_paper"
    sells = [ex for ex in data_out["executions"] if ex["action"] == "sell"]
    assert len(sells) == 1 and sells[0]["quantity"] == 500
    assert data_out["position_snapshot"]["positions"] == []
    # 880k cash + 500*12 sale = 886k
    assert data_out["position_snapshot"]["equity"] == 886_000.0
    # Nothing persisted on dry run.
    assert len(cols["portfolio_paper_positions"].docs) == 1
    assert not [p for p in cols["portfolio_trade_plans"].docs if p.get("origin") == "manual"]


def test_paper_rebalance_clear_persists_snapshot_and_plan(monkeypatch):
    cols, fin, data = _paper_rebalance_env()
    _patch_lineage_live_env(monkeypatch, cols, fin, data)

    resp = asyncio.run(
        portfolio_plans.paper_rebalance_portfolio(
            "pla",
            portfolio_plans.PaperRebalanceRequest(targets={"600000.SH": 0}, dry_run=False),
            current_user={"id": "user-1"},
        )
    )

    plan_id = resp["data"]["plan_id"]
    manual_plans = [p for p in cols["portfolio_trade_plans"].docs if p.get("origin") == "manual"]
    assert len(manual_plans) == 1
    plan = manual_plans[0]
    assert plan["status"] == "executed_paper"
    assert plan["manual_action"] == "liquidate"
    assert plan["paper_executed_at"]
    # New snapshot + equity row written under the new plan_id.
    snap = next(s for s in cols["portfolio_paper_positions"].docs if s["plan_id"] == plan_id)
    assert snap["positions"] == []
    assert snap["equity"] == 886_000.0
    assert any(e["plan_id"] == plan_id for e in cols["portfolio_paper_equity"].docs)
    assert [ex for ex in cols["portfolio_paper_executions"].docs if ex["plan_id"] == plan_id]


def test_paper_rebalance_partial_reduce(monkeypatch):
    cols, fin, data = _paper_rebalance_env()
    _patch_lineage_live_env(monkeypatch, cols, fin, data)

    resp = asyncio.run(
        portfolio_plans.paper_rebalance_portfolio(
            "pla",
            portfolio_plans.PaperRebalanceRequest(targets={"600000.SH": 200}, dry_run=False),
            current_user={"id": "user-1"},
        )
    )

    data_out = resp["data"]
    assert data_out["manual_action"] == "rebalance"
    pos = data_out["position_snapshot"]["positions"]
    held = next(p for p in pos if p["symbol"] == "600000.SH")
    assert held["shares"] == 200
    sells = [ex for ex in data_out["executions"] if ex["action"] == "sell"]
    assert sells[0]["quantity"] == 300


def test_paper_rebalance_add_new_symbol_builds_buy(monkeypatch):
    cols, fin, data = _paper_rebalance_env()
    _patch_lineage_live_env(monkeypatch, cols, fin, data)

    resp = asyncio.run(
        portfolio_plans.paper_rebalance_portfolio(
            "pla",
            portfolio_plans.PaperRebalanceRequest(targets={"000001.SZ": 300}, dry_run=False),
            current_user={"id": "user-1"},
        )
    )

    data_out = resp["data"]
    pos = {p["symbol"]: p for p in data_out["position_snapshot"]["positions"]}
    assert pos["000001.SZ"]["shares"] == 300
    assert pos["600000.SH"]["shares"] == 500  # untouched
    buys = [ex for ex in data_out["executions"] if ex["action"] == "buy"]
    assert buys[0]["quantity"] == 300


def test_paper_rebalance_no_targets_returns_400(monkeypatch):
    cols, fin, data = _paper_rebalance_env()
    _patch_lineage_live_env(monkeypatch, cols, fin, data)

    with pytest.raises(portfolio_plans.HTTPException) as excinfo:
        asyncio.run(
            portfolio_plans.paper_rebalance_portfolio(
                "pla",
                portfolio_plans.PaperRebalanceRequest(targets={}, dry_run=False),
                current_user={"id": "user-1"},
            )
        )

    assert excinfo.value.status_code == 400


def test_paper_rebalance_no_change_returns_400(monkeypatch):
    cols, fin, data = _paper_rebalance_env()
    _patch_lineage_live_env(monkeypatch, cols, fin, data)

    with pytest.raises(portfolio_plans.HTTPException) as excinfo:
        asyncio.run(
            portfolio_plans.paper_rebalance_portfolio(
                "pla",
                portfolio_plans.PaperRebalanceRequest(targets={"600000.SH": 500}, dry_run=False),
                current_user={"id": "user-1"},
            )
        )

    assert excinfo.value.status_code == 400


def test_weighted_growth_cycle_score_partial_weights():
    assert portfolio_plans._weighted_growth_cycle_score(80.0, None, 0.3, 0.7) == pytest.approx(80.0)
    assert portfolio_plans._weighted_growth_cycle_score(None, 60.0, 0.3, 0.7) == pytest.approx(60.0)
    assert portfolio_plans._weighted_growth_cycle_score(80.0, 60.0, 0.3, 0.7) == pytest.approx(66.0)


def test_get_portfolio_plan_bench_splits_starters_and_bench(monkeypatch):
    cols = _collections()
    data = _data_collections()
    stock_scores = _Collection(
        [
            {"symbol": "000001.SZ", "name": "A", "industry": "Bank", "score_date": "20250110", "index_codes": "csi1000", "growth_score": 90, "cycle_score": 80},
            {"symbol": "000002.SZ", "name": "B", "industry": "Bank", "score_date": "20250110", "index_codes": "csi1000", "growth_score": 85, "cycle_score": 75},
            {"symbol": "000003.SZ", "name": "C", "industry": "Tech", "score_date": "20250110", "index_codes": "csi1000", "growth_score": 80, "cycle_score": 70},
            {"symbol": "000004.SZ", "name": "D", "industry": "Tech", "score_date": "20250110", "index_codes": "csi1000", "growth_score": 70, "cycle_score": 60},
            {"symbol": "000005.SZ", "name": "E", "industry": "Tech", "score_date": "20250110", "index_codes": "csi1000", "growth_score": 60, "cycle_score": 50},
        ]
    )
    data["volume_price"].docs = [
        {"symbol": "000003.SZ", "trade_date": "20250110", "close": 15.5},
        {"symbol": "000004.SZ", "trade_date": "20250110", "close": 12.0},
    ]
    cols["portfolio_trade_plans"] = _Collection(
        [
            {
                "plan_id": "bench-plan",
                "strategy_template_id": "s1",
                "user_id": "user-1",
                "base_date": "20250110",
                "score_date": "20250110",
                "params_hash": "hx",
                "params_snapshot": {
                    "universe_index": "csi1000",
                    "growth_weight": 0.3,
                    "cycle_weight": 0.7,
                    "top_n": 2,
                    "rebalance_days": 10,
                },
            }
        ]
    )
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: cols)
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: cols)
    monkeypatch.setattr(portfolio_plans, "get_data_db", lambda: data)
    monkeypatch.setattr(portfolio_plans, "finance_db", {"stock_scores": stock_scores})
    # Starters mirror real holdings; 000001 carries the larger market value so it
    # sorts first.
    monkeypatch.setattr(
        portfolio_plans,
        "_current_lineage_holdings",
        lambda *args, **kwargs: [
            {"symbol": "000001.SZ", "name": "A", "shares": 1000, "last_price": 10.0, "market_value": 10000.0},
            {"symbol": "000002.SZ", "name": "B", "shares": 800, "last_price": 11.0, "market_value": 8800.0},
        ],
    )

    resp = asyncio.run(
        portfolio_plans.get_portfolio_plan_bench(
            "bench-plan",
            bench_multiplier=1.5,
            current_user={"id": "user-1"},
        )
    )

    payload = resp["data"]
    assert payload["pool_size"] == 3
    assert [row["symbol"] for row in payload["starters"]] == ["000001.SZ", "000002.SZ"]
    assert payload["starters"][0]["current_shares"] == 1000
    assert payload["starters"][0]["estimated_price"] == 10.0
    assert [row["symbol"] for row in payload["bench"]] == ["000003.SZ"]
    assert payload["bench"][0]["rank"] == 3
    assert payload["bench"][0]["latest_close"] == 15.5


def test_get_portfolio_plan_bench_enriches_latest_bench_llm_risk(monkeypatch):
    cols = _collections()
    data = _data_collections()
    stock_scores = _Collection(
        [
            {"symbol": "000001.SZ", "name": "A", "industry": "Bank", "score_date": "20250110", "index_codes": "csi1000", "growth_score": 90, "cycle_score": 80},
            {"symbol": "000002.SZ", "name": "B", "industry": "Bank", "score_date": "20250110", "index_codes": "csi1000", "growth_score": 85, "cycle_score": 75},
            {"symbol": "000003.SZ", "name": "C", "industry": "Tech", "score_date": "20250110", "index_codes": "csi1000", "growth_score": 80, "cycle_score": 70},
        ]
    )
    cols["portfolio_trade_plans"] = _Collection(
        [
            {
                "plan_id": "bench-plan",
                "strategy_template_id": "s1",
                "user_id": "user-1",
                "base_date": "20250110",
                "score_date": "20250110",
                "params_hash": "hx",
                "params_snapshot": {
                    "universe_index": "csi1000",
                    "growth_weight": 0.3,
                    "cycle_weight": 0.7,
                    "top_n": 2,
                },
            }
        ]
    )
    analyzer = _analyzer_collections()
    analyzer["portfolio_risk_llm"].insert_one(
        {
            "plan_id": "bench-plan",
            "run_id": "bench-run",
            "scope": "symbol",
            "symbol": "000003.SZ",
            "industry": "Tech",
            "severity": "high",
            "summary": "替补候选存在事件风险",
            "findings": [{"title": "测试公告", "severity": "high"}],
            "engine_version": "llm-v1",
            "model": "test-model",
            "analyzed_at": "2026-06-29T10:00:00",
        }
    )
    analysis_tasks = _Collection(
        [
            {
                "type": portfolio_plans.PORTFOLIO_RISK_LLM_TASK_TYPE,
                "user_id": "user-1",
                "status": "completed",
                "created_at": datetime(2026, 6, 30),
                "params": {"plan_id": "bench-plan", "run_id": "plan-run", "target_scope": "plan"},
            },
            {
                "type": portfolio_plans.PORTFOLIO_RISK_LLM_TASK_TYPE,
                "user_id": "user-1",
                "status": "completed",
                "created_at": datetime(2026, 6, 29),
                "params": {"plan_id": "bench-plan", "run_id": "bench-run", "target_scope": "bench"},
            },
        ]
    )
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: cols)
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: cols)
    monkeypatch.setattr(portfolio_plans, "get_data_db", lambda: data)
    monkeypatch.setattr(portfolio_plans, "get_analyzer_db", lambda: analyzer)
    monkeypatch.setattr(
        portfolio_plans,
        "finance_db",
        {"stock_scores": stock_scores, "analysis_tasks": analysis_tasks},
    )
    monkeypatch.setattr(
        portfolio_plans,
        "_current_lineage_holdings",
        lambda *args, **kwargs: [
            {"symbol": "000001.SZ", "name": "A", "shares": 1000, "last_price": 10.0, "market_value": 10000.0},
            {"symbol": "000002.SZ", "name": "B", "shares": 800, "last_price": 11.0, "market_value": 8800.0},
        ],
    )

    resp = asyncio.run(
        portfolio_plans.get_portfolio_plan_bench(
            "bench-plan",
            bench_multiplier=1.5,
            current_user={"id": "user-1"},
        )
    )

    payload = resp["data"]
    assert payload["bench"][0]["ai_risk"]["llm"]["run_id"] == "bench-run"
    assert payload["bench"][0]["ai_risk"]["llm"]["severity"] == "high"
    assert payload["ai_risk_llm_summary"]["run_id"] == "bench-run"


def test_get_portfolio_plan_bench_enriches_running_bench_llm_risk(monkeypatch):
    cols = _collections()
    data = _data_collections()
    stock_scores = _Collection(
        [
            {"symbol": "000001.SZ", "name": "A", "industry": "Bank", "score_date": "20250110", "index_codes": "csi1000", "growth_score": 90, "cycle_score": 80},
            {"symbol": "000002.SZ", "name": "B", "industry": "Bank", "score_date": "20250110", "index_codes": "csi1000", "growth_score": 85, "cycle_score": 75},
            {"symbol": "000003.SZ", "name": "C", "industry": "Tech", "score_date": "20250110", "index_codes": "csi1000", "growth_score": 80, "cycle_score": 70},
        ]
    )
    cols["portfolio_trade_plans"] = _Collection(
        [
            {
                "plan_id": "bench-running-plan",
                "strategy_template_id": "s1",
                "user_id": "user-1",
                "base_date": "20250110",
                "score_date": "20250110",
                "params_hash": "hx",
                "params_snapshot": {
                    "universe_index": "csi1000",
                    "growth_weight": 0.3,
                    "cycle_weight": 0.7,
                    "top_n": 2,
                },
            }
        ]
    )
    analyzer = _analyzer_collections()
    analyzer["portfolio_risk_llm"].insert_one(
        {
            "plan_id": "bench-running-plan",
            "run_id": "bench-running-run",
            "scope": "symbol",
            "symbol": "000003.SZ",
            "industry": "Tech",
            "severity": "low",
            "summary": "替补候选已有部分 LLM 结果",
            "findings": [],
            "engine_version": "llm-v1",
            "model": "test-model",
            "analyzed_at": "2026-06-29T10:00:00",
        }
    )
    analysis_tasks = _Collection(
        [
            {
                "type": portfolio_plans.PORTFOLIO_RISK_LLM_TASK_TYPE,
                "user_id": "user-1",
                "status": "completed",
                "created_at": datetime(2026, 6, 29),
                "params": {"plan_id": "bench-running-plan", "run_id": "bench-running-run", "target_scope": "bench"},
            },
            {
                "type": portfolio_plans.PORTFOLIO_RISK_LLM_TASK_TYPE,
                "user_id": "user-1",
                "status": "pending",
                "created_at": datetime(2026, 6, 29),
                "params": {"plan_id": "bench-running-plan", "run_id": "bench-running-run", "target_scope": "bench"},
            },
        ]
    )
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: cols)
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: cols)
    monkeypatch.setattr(portfolio_plans, "get_data_db", lambda: data)
    monkeypatch.setattr(portfolio_plans, "get_analyzer_db", lambda: analyzer)
    monkeypatch.setattr(
        portfolio_plans,
        "finance_db",
        {"stock_scores": stock_scores, "analysis_tasks": analysis_tasks},
    )
    monkeypatch.setattr(
        portfolio_plans,
        "_current_lineage_holdings",
        lambda *args, **kwargs: [
            {"symbol": "000001.SZ", "name": "A", "shares": 1000, "last_price": 10.0, "market_value": 10000.0},
            {"symbol": "000002.SZ", "name": "B", "shares": 800, "last_price": 11.0, "market_value": 8800.0},
        ],
    )

    resp = asyncio.run(
        portfolio_plans.get_portfolio_plan_bench(
            "bench-running-plan",
            bench_multiplier=1.5,
            current_user={"id": "user-1"},
        )
    )

    payload = resp["data"]
    assert payload["bench"][0]["ai_risk"]["llm"]["run_id"] == "bench-running-run"
    assert payload["bench"][0]["ai_risk"]["llm"]["summary"] == "替补候选已有部分 LLM 结果"
    assert payload["ai_risk_llm_summary"]["status"] == "running"


def test_current_lineage_holdings_live_empty_does_not_fallback_to_paper():
    cols = _collections()
    fin = _finance_collections()
    data = _data_collections()
    plan = {
        "plan_id": "live-plan",
        "strategy_template_id": "s1",
        "user_id": "user-1",
        "base_date": "20250110",
        "params_hash": "hx-live",
    }
    cols["portfolio_trade_plans"] = _Collection([plan])
    cols["portfolio_paper_positions"] = _Collection(
        [
            {
                "plan_id": "paper-plan",
                "strategy_template_id": "s1",
                "user_id": "user-1",
                "params_hash": "hx-live",
                "as_of_date": "20250109",
                "positions": [
                    {"symbol": "000001.SZ", "shares": 100, "market_value": 1000.0},
                ],
            }
        ]
    )
    fin["trade_signals"] = _Collection(
        [
            {
                "plan_id": "live-plan",
                "user_id": "user-1",
                "mode": "live",
                "order_id": "order-1",
                "symbol": "000001.SZ",
                "action": "buy",
            }
        ]
    )
    fin["trade_executions"] = _Collection()

    assert portfolio_plans._portfolio_execution_venue(cols, fin, plan, "user-1") == "live"
    assert portfolio_plans._current_lineage_holdings(cols, fin, data, plan, "user-1") == []


def test_current_lineage_holdings_live_reads_snapshot_without_replay():
    # Live holdings must come from the materialized snapshot, not a full replay
    # of the lineage signals/executions bundle. trade_signals raises if scanned.
    cols = _collections()
    fin = _finance_collections()
    data = _data_collections()
    plan = {
        "plan_id": "live-plan",
        "strategy_template_id": "s1",
        "user_id": "user-1",
        "base_date": "20250110",
        "params_hash": "hx-live",
    }
    cols["portfolio_trade_plans"] = _Collection([plan])
    cols["portfolio_lineage_controls"] = _Collection(
        [{"user_id": "user-1", "strategy_template_id": "s1", "params_hash": "hx-live", "execution_venue": "live"}]
    )
    cols["portfolio_live_snapshots"] = _Collection(
        [
            {
                "user_id": "user-1",
                "strategy_template_id": "s1",
                "params_hash": "hx-live",
                "securities_account_id": "acct-x",
                "positions": [
                    {"symbol": "000001.SZ", "name": "PAB", "shares": 100, "avg_cost": 10.0},
                    {"symbol": "600000.SH", "name": "SPD", "shares": 0, "avg_cost": 11.0},
                ],
                "watermark": {"execution_count": 0, "signal_count": 0},
            }
        ]
    )
    fin["trade_executions"] = _Collection()
    fin["trade_signals"] = _RaisingSignalCollection()

    holdings = portfolio_plans._current_lineage_holdings(cols, fin, data, plan, "user-1")
    assert {row["symbol"] for row in holdings} == {"000001.SZ"}  # shares>0 only, from snapshot


def test_enqueue_portfolio_bench_risk_enqueues_bench_symbols(monkeypatch):
    cols = _collections()
    data = _data_collections()
    stock_scores = _Collection(
        [
            {"symbol": "000001.SZ", "name": "A", "industry": "Bank", "score_date": "20250110", "index_codes": "csi1000", "growth_score": 90, "cycle_score": 80},
            {"symbol": "000002.SZ", "name": "B", "industry": "Bank", "score_date": "20250110", "index_codes": "csi1000", "growth_score": 85, "cycle_score": 75},
            {"symbol": "000003.SZ", "name": "C", "industry": "Tech", "score_date": "20250110", "index_codes": "csi1000", "growth_score": 80, "cycle_score": 70},
            {"symbol": "000004.SZ", "name": "D", "industry": "Tech", "score_date": "20250110", "index_codes": "csi1000", "growth_score": 70, "cycle_score": 60},
        ]
    )
    data["volume_price"].docs = [
        {"symbol": "000003.SZ", "trade_date": "20250110", "close": 15.5},
    ]
    cols["portfolio_trade_plans"] = _Collection(
        [
            {
                "plan_id": "bench-plan",
                "strategy_template_id": "s1",
                "user_id": "user-1",
                "base_date": "20250110",
                "score_date": "20250110",
                "params_hash": "hx",
                "params_snapshot": {
                    "universe_index": "csi1000",
                    "growth_weight": 0.3,
                    "cycle_weight": 0.7,
                    "top_n": 2,
                    "rebalance_days": 10,
                },
            }
        ]
    )
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: cols)
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: cols)
    monkeypatch.setattr(portfolio_plans, "get_data_db", lambda: data)
    monkeypatch.setattr(portfolio_plans, "finance_db", {"stock_scores": stock_scores})
    monkeypatch.setattr(
        portfolio_plans,
        "_current_lineage_holdings",
        lambda *args, **kwargs: [
            {"symbol": "000001.SZ", "name": "A", "shares": 1000, "last_price": 10.0, "market_value": 10000.0},
            {"symbol": "000002.SZ", "name": "B", "shares": 800, "last_price": 11.0, "market_value": 8800.0},
        ],
    )

    resp = asyncio.run(
        portfolio_plans.enqueue_portfolio_bench_risk(
            "bench-plan",
            bench_multiplier=1.5,
            current_user={"id": "user-1"},
        )
    )

    data_payload = resp["data"]
    assert data_payload["symbol_count"] == 1
    assert data_payload["base_date"] == "20250110"
    tasks = cols["portfolio_plan_generation_tasks"].docs
    assert len(tasks) == 1
    task = tasks[0]
    assert task["task_type"] == "bench_risk_review"
    assert task["status"] == "pending"
    # Held starters (000001/000002) stay off the bench review; only the
    # scored-but-unheld candidate is queued.
    assert task["symbols"] == ["000003.SZ"]
    assert task["base_date"] == "20250110"
    assert task["task_id"] == data_payload["task_id"]


def test_get_portfolio_plan_bench_excludes_live_holdings(monkeypatch):
    cols = _collections()
    data = _data_collections()
    stock_scores = _Collection(
        [
            {"symbol": "000001.SZ", "name": "A", "industry": "Bank", "score_date": "20250110", "index_codes": "csi1000", "growth_score": 90, "cycle_score": 80},
            {"symbol": "000002.SZ", "name": "B", "industry": "Bank", "score_date": "20250110", "index_codes": "csi1000", "growth_score": 85, "cycle_score": 75},
            {"symbol": "000003.SZ", "name": "C", "industry": "Tech", "score_date": "20250110", "index_codes": "csi1000", "growth_score": 80, "cycle_score": 70},
            {"symbol": "000004.SZ", "name": "D", "industry": "Tech", "score_date": "20250110", "index_codes": "csi1000", "growth_score": 70, "cycle_score": 60},
        ]
    )
    data["volume_price"].docs = [
        {"symbol": "000003.SZ", "trade_date": "20250110", "close": 15.5},
    ]
    cols["portfolio_trade_plans"] = _Collection(
        [
            {
                "plan_id": "bench-plan",
                "strategy_template_id": "s1",
                "user_id": "user-1",
                "base_date": "20250110",
                "score_date": "20250110",
                "params_hash": "hx",
                "params_snapshot": {
                    "universe_index": "csi1000",
                    "growth_weight": 0.3,
                    "cycle_weight": 0.7,
                    "top_n": 2,
                    "rebalance_days": 10,
                },
            }
        ]
    )
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: cols)
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: cols)
    monkeypatch.setattr(portfolio_plans, "get_data_db", lambda: data)
    monkeypatch.setattr(portfolio_plans, "finance_db", {"stock_scores": stock_scores})
    # Hold 000001/000002 as starters plus 000003 with the *unsuffixed* code to
    # prove suffix-insensitive matching keeps a held name off the bench even when
    # formats differ.
    monkeypatch.setattr(
        portfolio_plans,
        "_current_lineage_holdings",
        lambda *args, **kwargs: [
            {"symbol": "000001.SZ", "shares": 1000, "last_price": 10.0, "market_value": 10000.0},
            {"symbol": "000002.SZ", "shares": 800, "last_price": 11.0, "market_value": 8800.0},
            {"symbol": "000003", "shares": 500, "last_price": 15.5, "market_value": 7750.0},
        ],
    )

    resp = asyncio.run(
        portfolio_plans.get_portfolio_plan_bench(
            "bench-plan",
            bench_multiplier=1.5,
            current_user={"id": "user-1"},
        )
    )

    payload = resp["data"]
    starter_symbols = {row["symbol"] for row in payload["starters"]}
    assert {"000001.SZ", "000002.SZ", "000003"} <= starter_symbols
    # 000003 is held -> must not appear on the bench despite the suffix mismatch.
    assert all(row["symbol"] != "000003.SZ" for row in payload["bench"])


def test_reconcile_portfolio_holdings_flags_mismatch(monkeypatch):
    cols = _collections()
    cols["portfolio_trade_plans"] = _Collection(
        [
            {
                "plan_id": "rec-plan",
                "strategy_template_id": "s1",
                "user_id": "user-1",
                "base_date": "20250110",
                "params_hash": "hx",
            }
        ]
    )
    fin = {
        "trader_positions": _Collection(
            [
                {"user_id": "user-1", "securities_account_id": "acct-1", "symbol": "600000", "volume": 0, "synced_at": 1700.0},
                {"user_id": "user-1", "securities_account_id": "acct-1", "symbol": "000002.SZ", "volume": 500, "synced_at": 1700.0},
            ]
        )
    }
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: cols)
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: fin)
    monkeypatch.setattr(portfolio_plans, "get_data_db", lambda: _data_collections())
    # Ledger still thinks we hold 600000; broker shows it sold (0) -> mismatch.
    monkeypatch.setattr(
        portfolio_plans,
        "_lineage_live_account_id_from_fills",
        lambda *a, **k: "acct-1",
    )
    monkeypatch.setattr(
        portfolio_plans,
        "_account_level_live_holdings",
        lambda *a, **k: [{"symbol": "600000.SH", "name": "PAB", "shares": 1000}],
    )

    resp = asyncio.run(
        portfolio_plans.reconcile_portfolio_holdings("rec-plan", current_user={"id": "user-1"})
    )

    data = resp["data"]
    assert data["applicable"] is True
    assert data["in_sync"] is False
    diff_by_symbol = {row["symbol"]: row for row in data["diffs"]}
    assert diff_by_symbol["600000.SH"]["ledger_shares"] == 1000
    assert diff_by_symbol["600000.SH"]["account_shares"] == 0
    assert diff_by_symbol["600000.SH"]["diff"] == -1000
    # 000002 only on the broker side -> also a divergence to flag.
    assert diff_by_symbol["000002.SZ"]["account_shares"] == 500


def test_reconcile_portfolio_holdings_in_sync(monkeypatch):
    cols = _collections()
    cols["portfolio_trade_plans"] = _Collection(
        [{"plan_id": "rec-plan", "strategy_template_id": "s1", "user_id": "user-1", "base_date": "20250110", "params_hash": "hx"}]
    )
    fin = {
        "trader_positions": _Collection(
            [{"user_id": "user-1", "securities_account_id": "acct-1", "symbol": "600000", "volume": 1000, "synced_at": 1700.0}]
        )
    }
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: cols)
    monkeypatch.setattr(portfolio_plans, "get_db_collections", lambda: fin)
    monkeypatch.setattr(portfolio_plans, "get_data_db", lambda: _data_collections())
    monkeypatch.setattr(
        portfolio_plans,
        "_lineage_live_account_id_from_fills",
        lambda *a, **k: "acct-1",
    )
    monkeypatch.setattr(
        portfolio_plans,
        "_account_level_live_holdings",
        lambda *a, **k: [{"symbol": "600000.SH", "name": "PAB", "shares": 1000}],
    )

    resp = asyncio.run(
        portfolio_plans.reconcile_portfolio_holdings("rec-plan", current_user={"id": "user-1"})
    )

    data = resp["data"]
    assert data["applicable"] is True
    assert data["in_sync"] is True
    assert data["diffs"] == []


def _reconcile_shared_account_fixtures():
    cols = _collections()
    fin = _finance_collections()
    data = _data_collections()
    cols["portfolio_trade_plans"] = _Collection(
        [
            {
                "plan_id": "plan-a",
                "strategy_template_id": "stx",
                "user_id": "user-1",
                "base_date": "20250101",
                "status": "approved",
                "params_hash": "hash-a",
            },
            {
                "plan_id": "plan-b",
                "strategy_template_id": "sty",
                "user_id": "user-1",
                "base_date": "20250110",
                "status": "approved",
                "params_hash": "hash-b",
            },
        ]
    )
    return cols, fin, data


def test_reconcile_live_holdings_shared_account_in_sync(monkeypatch):
    cols, fin, data = _reconcile_shared_account_fixtures()
    fin["trade_signals"].docs = [
        {
            "order_id": "oa",
            "user_id": "user-1",
            "mode": "live",
            "plan_id": "plan-a",
            "securities_account_id": "acct-1",
            "created_at": 1,
        },
        {
            "order_id": "ob",
            "user_id": "user-1",
            "mode": "live",
            "plan_id": "plan-b",
            "securities_account_id": "acct-1",
            "created_at": 2,
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
        {
            "order_id": "ob",
            "user_id": "user-1",
            "symbol": "600000.SH",
            "side": "BUY",
            "filled_size": 200,
            "filled_price": 12.0,
            "timestamp": 2.0,
        },
    ]
    fin["trader_positions"].docs = [
        {
            "user_id": "user-1",
            "securities_account_id": "acct-1",
            "symbol": "000001.SZ",
            "volume": 100,
            "synced_at": 1700.0,
        },
        {
            "user_id": "user-1",
            "securities_account_id": "acct-1",
            "symbol": "600000.SH",
            "volume": 200,
            "synced_at": 1700.0,
        },
    ]
    _patch_lineage_live_env(monkeypatch, cols, fin, data)
    plan = cols["portfolio_trade_plans"].find_one({"plan_id": "plan-a"})

    result = portfolio_plans._reconcile_live_holdings(cols, fin, data, plan, "user-1")

    assert result["applicable"] is True
    assert result["in_sync"] is True
    assert result["diffs"] == []


def test_reconcile_live_holdings_same_symbol_aggregated_across_plans(monkeypatch):
    cols, fin, data = _reconcile_shared_account_fixtures()
    fin["trade_signals"].docs = [
        {
            "order_id": "oa",
            "user_id": "user-1",
            "mode": "live",
            "plan_id": "plan-a",
            "securities_account_id": "acct-1",
            "created_at": 1,
        },
        {
            "order_id": "ob",
            "user_id": "user-1",
            "mode": "live",
            "plan_id": "plan-b",
            "securities_account_id": "acct-1",
            "created_at": 2,
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
        {
            "order_id": "ob",
            "user_id": "user-1",
            "symbol": "000001.SZ",
            "side": "BUY",
            "filled_size": 100,
            "filled_price": 12.0,
            "timestamp": 2.0,
        },
    ]
    fin["trader_positions"].docs = [
        {
            "user_id": "user-1",
            "securities_account_id": "acct-1",
            "symbol": "000001.SZ",
            "volume": 200,
            "synced_at": 1700.0,
        },
    ]
    _patch_lineage_live_env(monkeypatch, cols, fin, data)
    plan = cols["portfolio_trade_plans"].find_one({"plan_id": "plan-b"})

    result = portfolio_plans._reconcile_live_holdings(cols, fin, data, plan, "user-1")

    assert result["applicable"] is True
    assert result["in_sync"] is True
    assert result["diffs"] == []


def test_reconcile_live_holdings_flags_real_account_drift(monkeypatch):
    cols, fin, data = _reconcile_shared_account_fixtures()
    fin["trade_signals"].docs = [
        {
            "order_id": "oa",
            "user_id": "user-1",
            "mode": "live",
            "plan_id": "plan-a",
            "securities_account_id": "acct-1",
            "created_at": 1,
        },
        {
            "order_id": "ob",
            "user_id": "user-1",
            "mode": "live",
            "plan_id": "plan-b",
            "securities_account_id": "acct-1",
            "created_at": 2,
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
        {
            "order_id": "ob",
            "user_id": "user-1",
            "symbol": "000001.SZ",
            "side": "BUY",
            "filled_size": 100,
            "filled_price": 12.0,
            "timestamp": 2.0,
        },
    ]
    fin["trader_positions"].docs = [
        {
            "user_id": "user-1",
            "securities_account_id": "acct-1",
            "symbol": "000001.SZ",
            "volume": 150,
            "synced_at": 1700.0,
        },
    ]
    _patch_lineage_live_env(monkeypatch, cols, fin, data)
    plan = cols["portfolio_trade_plans"].find_one({"plan_id": "plan-a"})

    result = portfolio_plans._reconcile_live_holdings(cols, fin, data, plan, "user-1")

    assert result["applicable"] is True
    assert result["in_sync"] is False
    diff_by_symbol = {row["symbol"]: row for row in result["diffs"]}
    assert diff_by_symbol["000001.SZ"]["ledger_shares"] == 200
    assert diff_by_symbol["000001.SZ"]["account_shares"] == 150
    assert diff_by_symbol["000001.SZ"]["diff"] == -50


def test_reconcile_live_holdings_suppresses_recent_fill_before_sync(monkeypatch):
    # Simulate an in-app add: the ledger already reflects the fresh fill but the
    # broker position snapshot is stale (synced before the fill). This must NOT
    # raise a mismatch; it is surfaced as a settling diff instead.
    cols, fin, data = _reconcile_shared_account_fixtures()
    fin["trade_signals"].docs = [
        {
            "order_id": "oa",
            "user_id": "user-1",
            "mode": "live",
            "plan_id": "plan-a",
            "securities_account_id": "acct-1",
            "status": "filled",
            "created_at": 1,
        },
    ]
    fin["trade_executions"].docs = [
        {
            "order_id": "oa",
            "user_id": "user-1",
            "symbol": "000001.SZ",
            "side": "BUY",
            "filled_size": 200,
            "filled_price": 10.0,
            "timestamp": 1800.0,
        },
    ]
    fin["trader_positions"].docs = [
        {
            "user_id": "user-1",
            "securities_account_id": "acct-1",
            "symbol": "000001.SZ",
            "volume": 100,
            "synced_at": 1700.0,
        },
    ]
    _patch_lineage_live_env(monkeypatch, cols, fin, data)
    plan = cols["portfolio_trade_plans"].find_one({"plan_id": "plan-a"})

    result = portfolio_plans._reconcile_live_holdings(cols, fin, data, plan, "user-1")

    assert result["applicable"] is True
    assert result["in_sync"] is True
    assert result["diffs"] == []
    settling_by_symbol = {row["symbol"]: row for row in result["settling_diffs"]}
    assert settling_by_symbol["000001.SZ"]["diff"] == -100


def test_reconcile_live_holdings_suppresses_inflight_signal(monkeypatch):
    # A working (non-terminal) order still settling at the broker must not flag a
    # mismatch even when the broker snapshot is fresher than the order timestamp.
    cols, fin, data = _reconcile_shared_account_fixtures()
    fin["trade_signals"].docs = [
        {
            "order_id": "oa",
            "user_id": "user-1",
            "mode": "live",
            "plan_id": "plan-a",
            "securities_account_id": "acct-1",
            "symbol": "000001.SZ",
            "status": "pending",
            "created_at": 1,
        },
    ]
    fin["trade_executions"].docs = [
        {
            "order_id": "oa",
            "user_id": "user-1",
            "symbol": "000001.SZ",
            "side": "BUY",
            "filled_size": 200,
            "filled_price": 10.0,
            "timestamp": 1.0,
        },
    ]
    fin["trader_positions"].docs = [
        {
            "user_id": "user-1",
            "securities_account_id": "acct-1",
            "symbol": "000001.SZ",
            "volume": 100,
            "synced_at": 1700.0,
        },
    ]
    _patch_lineage_live_env(monkeypatch, cols, fin, data)
    plan = cols["portfolio_trade_plans"].find_one({"plan_id": "plan-a"})

    result = portfolio_plans._reconcile_live_holdings(cols, fin, data, plan, "user-1")

    assert result["applicable"] is True
    assert result["in_sync"] is True
    assert result["diffs"] == []
    assert {row["symbol"] for row in result["settling_diffs"]} == {"000001.SZ"}


def test_force_rebalance_enqueues_rebalance_task_with_lineage_params(monkeypatch):
    cols = _collections()
    cols["portfolio_trade_plans"] = _Collection(
        [
            {
                "plan_id": "anchor-plan",
                "strategy_template_id": "s1",
                "user_id": "user-1",
                "base_date": "20250102",
                "params_hash": "hash-abc",
                "preset_id": "preset-1",
                "params_snapshot": {
                    "universe_index": "csi1000",
                    "growth_weight": 0.3,
                    "cycle_weight": 0.7,
                    "top_n": 10,
                    "rebalance_days": 10,
                    "initial_capital": 500_000,
                    "lot_size": 100,
                    "cash_buffer": 0.03,
                    "candidate_pool_enabled": True,
                    "candidate_pool_multiplier": 3,
                    "monitor_can_trade": True,
                },
            }
        ]
    )
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: cols)

    resp = asyncio.run(
        portfolio_plans.force_rebalance_portfolio_plan(
            "anchor-plan",
            portfolio_plans.ForceRebalanceRequest(base_date="20250110"),
            current_user={"id": "user-1"},
        )
    )

    task = resp["data"]
    assert task["task_type"] == "generate_portfolio_plan"
    assert task["mode"] == "rebalance"
    assert task["force"] is True
    assert task["base_date"] == "20250110"
    assert task["params_override"]["top_n"] == 10
    assert task["params_override"]["initial_capital"] == 500_000
    assert task["params_override"]["lot_size"] == 100
    assert task["params_override"]["candidate_pool_enabled"] is True
    assert task["params_override"]["candidate_pool_multiplier"] == 3
    assert "monitor_can_trade" not in task["params_override"]
    assert len(cols["portfolio_plan_generation_tasks"].docs) == 1


def test_force_rebalance_defaults_base_date_to_beijing_business_day(monkeypatch):
    cols = _collections()
    cols["portfolio_trade_plans"] = _Collection(
        [
            {
                "plan_id": "anchor-plan",
                "strategy_template_id": "s1",
                "user_id": "user-1",
                "base_date": "20250102",
                "params_hash": "hash-abc",
                "params_snapshot": {"top_n": 10, "rebalance_days": 10},
                "status": "executed_live",
                "plan_type": "rebalance",
                "record_kind": portfolio_plans.RECORD_KIND_TRADE_PLAN,
            }
        ]
    )
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: cols)
    monkeypatch.setattr(portfolio_plans, "_business_today_yyyymmdd", lambda: "20260706")

    resp = asyncio.run(
        portfolio_plans.force_rebalance_portfolio_plan(
            "anchor-plan",
            portfolio_plans.ForceRebalanceRequest(),
            current_user={"id": "user-1"},
        )
    )

    assert resp["data"]["base_date"] == "20260706"
    assert cols["portfolio_plan_generation_tasks"].docs[0]["base_date"] == "20260706"


@pytest.mark.parametrize("open_status", ["needs_review", "approved"])
def test_force_rebalance_blocks_when_lineage_has_open_trade_plan(monkeypatch, open_status):
    cols = _collections()
    cols["portfolio_trade_plans"] = _Collection(
        [
            {
                "plan_id": "anchor-plan",
                "strategy_template_id": "s1",
                "user_id": "user-1",
                "base_date": "20250102",
                "params_hash": "hash-abc",
                "params_snapshot": {"top_n": 10},
                "status": "executed_live",
                "plan_type": "rebalance",
                "record_kind": portfolio_plans.RECORD_KIND_TRADE_PLAN,
            },
            {
                "plan_id": "open-plan",
                "strategy_template_id": "s1",
                "user_id": "user-1",
                "base_date": "20250103",
                "params_hash": "hash-abc",
                "params_snapshot": {"top_n": 10},
                "status": open_status,
                "plan_type": "rebalance",
                "record_kind": portfolio_plans.RECORD_KIND_TRADE_PLAN,
            },
        ]
    )
    monkeypatch.setattr(portfolio_plans, "get_trading_db_collections", lambda: cols)

    with pytest.raises(portfolio_plans.HTTPException) as exc:
        asyncio.run(
            portfolio_plans.force_rebalance_portfolio_plan(
                "anchor-plan",
                portfolio_plans.ForceRebalanceRequest(base_date="20250110"),
                current_user={"id": "user-1"},
            )
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["plan_id"] == "open-plan"
    assert exc.value.detail["status"] == open_status
    assert not cols["portfolio_plan_generation_tasks"].docs
