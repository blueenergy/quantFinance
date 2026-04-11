"""Unit tests for market analysis cache fetch and scheduler task enqueue."""
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
            lambda date_str: cached_data,
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
            lambda date_str: None,
        )
        monkeypatch.setattr(
            market.market_analysis_cache,
            "get_latest_trading_day_analysis",
            lambda: None,
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
            lambda date_str: None,
        )
        monkeypatch.setattr(
            market.market_analysis_cache,
            "get_latest_trading_day_analysis",
            lambda: latest_data,
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
            lambda date_str: None,
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
