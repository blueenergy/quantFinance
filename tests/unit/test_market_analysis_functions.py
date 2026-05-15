"""Unit tests for market analysis cache fetch and scheduler task enqueue."""
import asyncio
import pytest
from datetime import date, datetime
from unittest.mock import MagicMock


def _patch_market_date(monkeypatch, market_module, fake_today: date):
    """Replace market.date with an object that supports today() and fromisoformat()."""

    class FakeDate:
        @staticmethod
        def today():
            return fake_today

        @staticmethod
        def fromisoformat(s: str):
            return date.fromisoformat(s)

    monkeypatch.setattr(market_module, "date", FakeDate)


@pytest.mark.unit
class TestFetchMarketAnalysisResult:
    """Tests for routers.market.fetch_market_analysis_result (cache-only)."""

    def test_trading_day_with_cache(self, monkeypatch):
        """On a trading day, if cache exists, return it."""
        from routers import market

        fake_today = date(2025, 12, 22)
        _patch_market_date(monkeypatch, market, fake_today)

        monkeypatch.setattr(
            market.market_analysis_cache,
            "is_trading_day",
            lambda d: True,
        )

        cached_data = {
            "analysis_result": {
                "success": True,
                "mood": "乐观",
                "summary": "市场表现强劲",
            },
            "analysis_date": "2025-12-22",
            "created_at": datetime(2025, 12, 22, 10, 0, 0),
        }
        monkeypatch.setattr(
            market.market_analysis_cache,
            "get_cached_analysis",
            lambda date_str, time_slot=None: cached_data,
        )
        monkeypatch.setattr(
            market.market_analysis_cache,
            "list_slots_for_date",
            lambda d: [],
        )
        monkeypatch.setattr(
            market.market_analysis_cache,
            "get_nearest_previous_snapshot",
            lambda d, s, max_lookback_days=7: None,
        )

        result = market.fetch_market_analysis_result()

        assert result["success"] is True
        assert result["analysis_date"] == "2025-12-22"
        assert result["cache_info"]["from_cache"] is True
        assert result["cache_info"]["reason"] == "返回当日缓存的大盘分析结果"
        assert result["mood"] == "乐观"

    def test_trading_day_no_cache(self, monkeypatch):
        """Trading day, no cache → tell user analysis is not ready yet."""
        from routers import market

        fake_today = date(2025, 12, 22)
        _patch_market_date(monkeypatch, market, fake_today)

        monkeypatch.setattr(
            market.market_analysis_cache,
            "is_trading_day",
            lambda d: True,
        )
        monkeypatch.setattr(
            market.market_analysis_cache,
            "get_cached_analysis",
            lambda date_str, time_slot=None: None,
        )
        monkeypatch.setattr(
            market.market_analysis_cache,
            "get_latest_trading_day_analysis",
            lambda: None,
        )
        monkeypatch.setattr(
            market.market_analysis_cache,
            "list_slots_for_date",
            lambda d: [],
        )

        result = market.fetch_market_analysis_result()

        assert result["success"] is False
        assert result["error"] == "今日 的大盘分析尚未生成"
        assert result["is_trading_day"] is True
        assert result["is_expected"] is True
        assert result["expected_schedule"] == ["10:00", "11:30", "15:30"]

    def test_non_trading_day_with_latest_analysis(self, monkeypatch):
        """No cache for calendar today → fall back to latest trading day analysis."""
        from routers import market

        fake_today = date(2025, 12, 21)
        _patch_market_date(monkeypatch, market, fake_today)

        monkeypatch.setattr(
            market.market_analysis_cache,
            "is_trading_day",
            lambda d: False,
        )

        latest_data = {
            "analysis_result": {
                "success": True,
                "mood": "谨慎",
                "summary": "周五收盘总结",
            },
            "analysis_date": "2025-12-19",
            "created_at": datetime(2025, 12, 19, 15, 30, 0),
        }
        monkeypatch.setattr(
            market.market_analysis_cache,
            "get_cached_analysis",
            lambda date_str, time_slot=None: None,
        )
        monkeypatch.setattr(
            market.market_analysis_cache,
            "get_latest_trading_day_analysis",
            lambda: latest_data,
        )
        monkeypatch.setattr(
            market.market_analysis_cache,
            "list_slots_for_date",
            lambda d: [],
        )
        monkeypatch.setattr(
            market.market_analysis_cache,
            "get_nearest_previous_snapshot",
            lambda d, s, max_lookback_days=7: None,
        )

        result = market.fetch_market_analysis_result()

        assert result["success"] is True
        assert result["analysis_date"] == "2025-12-19"
        assert result["cache_info"]["from_cache"] is True
        assert "今日非交易日" in result["cache_info"]["reason"]
        assert "2025-12-19" in result["cache_info"]["reason"]
        assert "非交易日" in result["notice"]

    def test_non_trading_day_no_latest_analysis(self, monkeypatch):
        """No cache for today and no latest analysis → error payload."""
        from routers import market

        fake_today = date(2025, 12, 21)
        _patch_market_date(monkeypatch, market, fake_today)

        monkeypatch.setattr(
            market.market_analysis_cache,
            "is_trading_day",
            lambda d: False,
        )
        monkeypatch.setattr(
            market.market_analysis_cache,
            "get_cached_analysis",
            lambda date_str, time_slot=None: None,
        )
        monkeypatch.setattr(
            market.market_analysis_cache,
            "get_latest_trading_day_analysis",
            lambda: None,
        )

        result = market.fetch_market_analysis_result()

        assert result["success"] is False
        assert result["error"] == "2025-12-21 为非交易日，且暂无可展示的历史大盘分析数据"
        assert result["is_trading_day"] is False
        assert result["is_expected"] is True


@pytest.mark.unit
class TestHomeSummaryBuilders:
    def test_market_home_summary_falls_back_to_result_fields(self):
        from routers import market

        summary = market._build_market_home_summary({
            "summary": "市场整体呈现震荡偏强格局，主线仍在科技与高股息之间轮动。",
            "riskLevel": "medium",
            "mainlineAnalysis": "科技分化，高股息承接",
            "mood": "谨慎乐观",
        })

        assert summary["summary_line"].startswith("市场整体呈现震荡偏强格局")
        assert summary["signals"] == ["风险 medium", "科技分化，高股息承接"]
        assert summary["tone"] == "谨慎乐观"

    def test_global_home_summary_prefers_embedded_home_summary(self):
        from routers import market

        summary = market._build_global_home_summary({
            "analysis": {
                "sentiment_mood": "谨慎",
                "net_impact_score": 43,
                "risk_factors": ["VIX抬升"],
                "home_summary": {
                    "summary_line": "外盘承压，A股开盘情绪偏谨慎",
                    "signals": ["VIX抬升", "黄金走强"],
                    "tone": "谨慎",
                    "risk_tag": "中性偏谨慎",
                },
            }
        })

        assert summary["summary_line"] == "谨慎 | 净影响分 +43.0"
        assert summary["signals"] == ["VIX抬升", "黄金走强"]
        assert summary["risk_tag"] == "中性偏谨慎"

    def test_data_pulse_home_summary_from_overview(self):
        from routers import market

        summary = market._build_data_pulse_home_summary({
            "sync_health": {
                "health_pct": 84,
                "healthy": 21,
                "total_pipelines": 25,
            },
            "freshness_detail": [
                {"latest_value": "20251222"},
                {"latest_value": "20251221"},
            ],
            "news": {
                "headlines": {
                    "macro": "headline-a",
                    "equity": "headline-b",
                }
            },
        })

        assert summary["status"] == "正常"
        assert summary["summary_line"] == "21/25 条管线正常，当前健康度 84%"
        assert summary["signals"] == ["新闻 2 条", "同步正常"]
        assert summary["updated_at"] == "最新数据 12-22"

    def test_collect_home_summary_cards_aggregates_three_modules(self, monkeypatch):
        from routers import market

        monkeypatch.setattr(market, "fetch_market_analysis_result", lambda analysis_date=None: {
            "success": True,
            "analysisType": "已生成",
            "summary": "市场延续修复，主线集中在科技。",
            "riskLevel": "medium",
            "mainlineAnalysis": "科技修复",
            "mood": "谨慎乐观",
            "analysis_date": "2025-12-22",
            "cache_info": {"created_at": "2025-12-22T15:30:00"},
        })

        fake_global_collection = MagicMock()
        fake_global_collection.find_one.return_value = {
            "analysis": {
                "sentiment_mood": "谨慎",
                "net_impact_score": 43,
                "risk_factors": ["VIX抬升"],
            },
            "analyzed_at": "2025-12-22T08:00:00",
        }
        monkeypatch.setattr(market, "get_analyzer_db", lambda: {"global_analysis": fake_global_collection})
        monkeypatch.setattr(market, "_get_data_pulse_overview_for_home", lambda: {
            "sync_health": {"health_pct": 80, "healthy": 8, "total_pipelines": 10},
            "freshness_detail": [{"latest_value": "20251222"}],
            "news": {"headlines": {"macro": "headline-a"}},
        })

        data = market._collect_home_summary_cards("2025-12-22")

        assert set(data.keys()) == {"global-market-brief", "data-pulse", "market-analysis"}
        assert data["market-analysis"]["success"] is True
        assert data["global-market-brief"]["success"] is True
        assert data["data-pulse"]["success"] is True


@pytest.mark.unit
class TestGenerateMarketAnalysisTask:
    """scheduler.generate_market_analysis_task — enqueue to analysis_tasks (no celery_app)."""

    @staticmethod
    def _patch_collections(monkeypatch, fake_tasks_col, fake_users_coll):
        def fake_get_collections():
            return {
                "analysis_tasks": fake_tasks_col,
                "users": fake_users_coll,
            }

        import db

        monkeypatch.setattr(db, "get_db_collections", fake_get_collections)

    def test_schedule_success(self, monkeypatch):
        from scheduler import generate_market_analysis_task
        import scheduler as sched_mod

        fake_now = datetime(2025, 6, 15, 12, 0, 0)
        monkeypatch.setattr(sched_mod, "datetime", MagicMock())
        sched_mod.datetime.now.return_value = fake_now

        fake_tasks_col = MagicMock()
        fake_tasks_col.find_one.return_value = None
        fake_tasks_col.insert_one.return_value.inserted_id = "new-task-id"

        fake_users_coll = MagicMock()
        fake_users_coll.find_one.return_value = {"_id": "admin-id"}

        self._patch_collections(monkeypatch, fake_tasks_col, fake_users_coll)

        generate_market_analysis_task("market_analysis_1130")

        fake_tasks_col.insert_one.assert_called_once()
        inserted = fake_tasks_col.insert_one.call_args[0][0]
        assert inserted["type"] == "market_analysis"
        assert inserted["status"] == "pending"
        assert inserted["user_id"] == "admin-id"
        assert inserted["symbol"] == "MARKET"
        assert inserted["params"]["date"] == "2025-06-15"
        assert inserted["params"]["job_id"] == "market_analysis_1130"
        assert inserted["params"]["analysis_type"] == "intraday"

    def test_post_market_job_type(self, monkeypatch):
        from scheduler import generate_market_analysis_task
        import scheduler as sched_mod

        monkeypatch.setattr(sched_mod, "datetime", MagicMock())
        sched_mod.datetime.now.return_value = datetime(2025, 6, 15, 16, 30, 0)

        fake_tasks_col = MagicMock()
        fake_tasks_col.find_one.return_value = None
        fake_tasks_col.insert_one.return_value.inserted_id = "id2"
        fake_users_coll = MagicMock()
        fake_users_coll.find_one.return_value = {"_id": "a"}

        self._patch_collections(monkeypatch, fake_tasks_col, fake_users_coll)

        generate_market_analysis_task("market_analysis_1630")
        inserted = fake_tasks_col.insert_one.call_args[0][0]
        assert inserted["params"]["analysis_type"] == "post_market"

    def test_duplicate_prevention(self, monkeypatch):
        from scheduler import generate_market_analysis_task
        import scheduler as sched_mod

        monkeypatch.setattr(sched_mod, "datetime", MagicMock())
        sched_mod.datetime.now.return_value = datetime(2025, 6, 15, 10, 0, 0)

        fake_tasks_col = MagicMock()
        fake_tasks_col.find_one.return_value = {"_id": "existing-task"}
        fake_users_coll = MagicMock()
        fake_users_coll.find_one.return_value = {"_id": "admin-id"}

        self._patch_collections(monkeypatch, fake_tasks_col, fake_users_coll)

        generate_market_analysis_task("market_analysis_1130")

        fake_tasks_col.insert_one.assert_not_called()

    def test_admin_fallback(self, monkeypatch):
        from scheduler import generate_market_analysis_task
        import scheduler as sched_mod

        monkeypatch.setattr(sched_mod, "datetime", MagicMock())
        sched_mod.datetime.now.return_value = datetime(2025, 6, 15, 10, 0, 0)

        fake_tasks_col = MagicMock()
        fake_tasks_col.find_one.return_value = None
        fake_tasks_col.insert_one.return_value.inserted_id = "task-id"
        fake_users_coll = MagicMock()
        fake_users_coll.find_one.return_value = None

        self._patch_collections(monkeypatch, fake_tasks_col, fake_users_coll)

        generate_market_analysis_task("market_analysis_1130")

        fake_tasks_col.insert_one.assert_called_once()
        inserted = fake_tasks_col.insert_one.call_args[0][0]
        assert inserted["user_id"] == "system"


@pytest.mark.unit
class TestTriggerMarketAnalysis:
    """Tests for routers.market.trigger_market_analysis effective date handling."""

    @staticmethod
    def _patch_collections(monkeypatch, market_module, fake_tasks_col):
        monkeypatch.setattr(
            market_module,
            "get_db_collections",
            lambda: {"analysis_tasks": fake_tasks_col},
        )

    def test_non_trading_trigger_uses_effective_trading_day(self, monkeypatch):
        from routers import market
        from models.all_models import MarketAnalysisRequest

        fake_tasks_col = MagicMock()
        fake_tasks_col.find_one.return_value = None
        fake_tasks_col.insert_one.return_value.inserted_id = "task-1"

        self._patch_collections(monkeypatch, market, fake_tasks_col)
        monkeypatch.setattr(market, "get_latest_trade_date", lambda: "20251219")
        monkeypatch.setattr(market.market_analysis_cache, "is_trading_day", lambda d: False)
        monkeypatch.setattr(market.market_analysis_cache, "get_cached_analysis", lambda d, time_slot=None: None)

        req = MarketAnalysisRequest(date="2025-12-21", force_refresh=False)
        result = asyncio.run(market.trigger_market_analysis(req, current_user={"id": "u1"}))

        assert result["success"] is True
        assert result["requested_date"] == "2025-12-21"
        assert result["effective_analysis_date"] == "2025-12-19"
        assert result["is_non_trading_trigger"] is True

        inserted = fake_tasks_col.insert_one.call_args[0][0]
        assert inserted["params"]["date"] == "2025-12-19"
        assert inserted["params"]["requested_date"] == "2025-12-21"
        assert inserted["params"]["effective_analysis_date"] == "2025-12-19"
        assert inserted["params"]["is_non_trading_trigger"] is True

    def test_non_trading_trigger_skips_when_cache_exists(self, monkeypatch):
        from routers import market
        from models.all_models import MarketAnalysisRequest

        fake_tasks_col = MagicMock()
        self._patch_collections(monkeypatch, market, fake_tasks_col)
        monkeypatch.setattr(market, "get_latest_trade_date", lambda: "20251219")
        monkeypatch.setattr(market.market_analysis_cache, "is_trading_day", lambda d: False)
        monkeypatch.setattr(
            market.market_analysis_cache,
            "get_cached_analysis",
            lambda d, time_slot=None: {"analysis_date": "2025-12-19", "analysis_result": {"summary": "ok"}},
        )

        req = MarketAnalysisRequest(date="2025-12-21", force_refresh=False)
        result = asyncio.run(market.trigger_market_analysis(req, current_user={"id": "u1"}))

        assert result["success"] is True
        assert result["skipped_existing"] is True
        assert result["effective_analysis_date"] == "2025-12-19"
        fake_tasks_col.insert_one.assert_not_called()
