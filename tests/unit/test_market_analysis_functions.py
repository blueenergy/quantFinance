"""Unit tests for market analysis functions added today.

Tests cover:
1. fetch_market_analysis_result() - frontend cache-only logic
2. generate_market_analysis_task() - backend Celery task
"""
import pytest
from datetime import date, datetime
from unittest.mock import MagicMock, AsyncMock


@pytest.mark.unit
class TestFetchMarketAnalysisResult:
    """Test the frontend cache-only function."""

    def test_trading_day_with_cache(self, monkeypatch):
        """On a trading day, if cache exists, return it."""
        from routers import market
        from datetime import date as date_class
        
        # Mock today as a trading day (e.g., Monday)
        fake_today = date_class(2025, 12, 22)  # Monday
        
        class FakeDate:
            @staticmethod
            def today():
                return fake_today
        
        monkeypatch.setattr(market, 'date', FakeDate)
        
        # Mock is_trading_day -> True
        monkeypatch.setattr(
            market.market_analysis_cache,
            'is_trading_day',
            lambda d: True
        )
        
        # Mock cache hit
        cached_data = {
            'analysis_result': {
                'success': True,
                'mood': '乐观',
                'summary': '市场表现强劲'
            },
            'analysis_date': '2025-12-22',
            'created_at': datetime(2025, 12, 22, 10, 0, 0)
        }
        monkeypatch.setattr(
            market.market_analysis_cache,
            'get_cached_analysis',
            lambda date_str: cached_data
        )
        
        result = market.fetch_market_analysis_result()
        
        assert result['success'] is True
        assert result['analysis_date'] == '2025-12-22'
        assert result['cache_info']['from_cache'] is True
        assert result['cache_info']['reason'] == "返回当日缓存的大盘分析结果"
        assert result['mood'] == '乐观'

    def test_trading_day_no_cache(self, monkeypatch):
        """On a trading day, if no cache, return error message."""
        from routers import market
        from datetime import date as date_class
        
        fake_today = date_class(2025, 12, 22)
        
        class FakeDate:
            @staticmethod
            def today():
                return fake_today
        
        monkeypatch.setattr(market, 'date', FakeDate)
        
        monkeypatch.setattr(
            market.market_analysis_cache,
            'is_trading_day',
            lambda d: True
        )
        
        # Mock cache miss
        monkeypatch.setattr(
            market.market_analysis_cache,
            'get_cached_analysis',
            lambda date_str: None
        )
        
        result = market.fetch_market_analysis_result()
        
        assert result['success'] is False
        assert result['error'] == "今日尚无大盘分析，请等待系统定时任务生成"
        assert result['is_trading_day'] is True

    def test_non_trading_day_with_latest_analysis(self, monkeypatch):
        """On a non-trading day, return latest trading day's analysis."""
        from routers import market
        from datetime import date as date_class
        
        # Mock today as a weekend (Sunday)
        fake_today = date_class(2025, 12, 21)  # Sunday
        
        class FakeDate:
            @staticmethod
            def today():
                return fake_today
        
        monkeypatch.setattr(market, 'date', FakeDate)
        
        monkeypatch.setattr(
            market.market_analysis_cache,
            'is_trading_day',
            lambda d: False
        )
        
        # Mock latest analysis from Friday
        latest_data = {
            'analysis_result': {
                'success': True,
                'mood': '谨慎',
                'summary': '周五收盘总结'
            },
            'analysis_date': '2025-12-19',
            'created_at': datetime(2025, 12, 19, 15, 30, 0)
        }
        monkeypatch.setattr(
            market.market_analysis_cache,
            'get_latest_trading_day_analysis',
            lambda: latest_data
        )
        
        result = market.fetch_market_analysis_result()
        
        assert result['success'] is True
        assert result['analysis_date'] == '2025-12-19'
        assert result['cache_info']['from_cache'] is True
        assert '非交易日' in result['cache_info']['reason']
        assert '2025-12-19' in result['cache_info']['reason']

    def test_non_trading_day_no_latest_analysis(self, monkeypatch):
        """On a non-trading day, if no latest analysis, return error."""
        from routers import market
        from datetime import date as date_class
        
        fake_today = date_class(2025, 12, 21)
        
        class FakeDate:
            @staticmethod
            def today():
                return fake_today
        
        monkeypatch.setattr(market, 'date', FakeDate)
        
        monkeypatch.setattr(
            market.market_analysis_cache,
            'is_trading_day',
            lambda d: False
        )
        
        monkeypatch.setattr(
            market.market_analysis_cache,
            'get_latest_trading_day_analysis',
            lambda: None
        )
        
        result = market.fetch_market_analysis_result()
        
        assert result['success'] is False
        assert result['error'] == "非交易日，且最近交易日尚无大盘分析结果"
        assert result['is_trading_day'] is False


@pytest.mark.unit
class TestGenerateMarketAnalysisTask:
    """Test the backend Celery task for SCHEDULING market analysis (Async)."""

    def test_schedule_success(self, monkeypatch):
        """Task should be inserted into MongoDB analysis_tasks collection."""
        from celery_app import generate_market_analysis_task
        from datetime import datetime
        
        # Mock DB collections
        fake_tasks_col = MagicMock()
        fake_tasks_col.find_one.return_value = None  # No existing task
        fake_tasks_col.insert_one.return_value.inserted_id = 'new-task-id'
        
        fake_users_coll = MagicMock()
        fake_users_coll.find_one.return_value = {"_id": "admin-id"}
        
        def fake_get_collections():
            return {
                'analysis_tasks': fake_tasks_col,
                'users': fake_users_coll
            }
        
        import db
        monkeypatch.setattr(db, 'get_db_collections', fake_get_collections)
        
        result = generate_market_analysis_task()
        
        assert result['success'] is True
        assert result['task_id'] == 'new-task-id'
        
        # Verify insertion
        fake_tasks_col.insert_one.assert_called_once()
        inserted_doc = fake_tasks_col.insert_one.call_args[0][0]
        assert inserted_doc['type'] == 'market_analysis'
        assert inserted_doc['status'] == 'pending'
        assert inserted_doc['user_id'] == 'admin-id'
        assert inserted_doc['symbol'] == 'MARKET'

    def test_duplicate_prevention(self, monkeypatch):
        """If a task is already pending/processing for today, do not insert new one."""
        from celery_app import generate_market_analysis_task
        
        # Mock DB with existing task
        fake_tasks_col = MagicMock()
        fake_tasks_col.find_one.return_value = {'_id': 'existing-task'}
        
        fake_users_coll = MagicMock()
        fake_users_coll.find_one.return_value = {"_id": "admin-id"}
        
        def fake_get_collections():
            return {
                'analysis_tasks': fake_tasks_col,
                'users': fake_users_coll
            }
        
        import db
        monkeypatch.setattr(db, 'get_db_collections', fake_get_collections)
        
        result = generate_market_analysis_task()
        
        assert result['success'] is True
        assert result['message'] == 'already_queued'
        fake_tasks_col.insert_one.assert_not_called()

    def test_admin_fallback(self, monkeypatch):
        """If admin user not found, task should be assigned to 'system'."""
        from celery_app import generate_market_analysis_task
        
        fake_tasks_col = MagicMock()
        fake_tasks_col.find_one.return_value = None
        fake_tasks_col.insert_one.return_value.inserted_id = 'task-id'
        
        fake_users_coll = MagicMock()
        fake_users_coll.find_one.return_value = None  # No admin found
        
        def fake_get_collections():
            return {
                'analysis_tasks': fake_tasks_col,
                'users': fake_users_coll
            }
        
        import db
        monkeypatch.setattr(db, 'get_db_collections', fake_get_collections)
        
        result = generate_market_analysis_task()
        
        assert result['success'] is True
        fake_tasks_col.insert_one.assert_called_once()
        inserted_doc = fake_tasks_col.insert_one.call_args[0][0]
        assert inserted_doc['user_id'] == 'system'
