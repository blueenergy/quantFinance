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
    """Test the backend Celery task for generating market analysis."""

    def test_no_market_data(self, monkeypatch):
        """If fetch_market_data returns None, task should skip."""
        from celery_app import generate_market_analysis_task
        
        # Mock fetch_market_data to return None
        import routers.market as market_router
        monkeypatch.setattr(market_router, 'fetch_market_data', lambda: None)
        
        result = generate_market_analysis_task()
        
        assert result['success'] is False
        assert result['reason'] == 'no_market_data'

    def test_no_admin_user(self, monkeypatch):
        """If admin user doesn't exist, task should fail."""
        from celery_app import generate_market_analysis_task
        
        # Mock fetch_market_data
        import routers.market as market_router
        monkeypatch.setattr(
            market_router,
            'fetch_market_data',
            lambda: {'analysis_date': '2025-12-22', 'market_data': {}}
        )
        
        # Mock get_db_collections at db module level
        fake_users_coll = MagicMock()
        fake_users_coll.find_one.return_value = None  # No admin user
        
        # Mock ladder data with return values
        fake_ladder_coll = MagicMock()
        fake_ladder_coll.find_one.return_value = {"sentiment_score": 85, "sentiment_mood": "乐观"}
        fake_daily_coll = MagicMock()
        fake_daily_coll.find_one.return_value = {"limit_up_count": 50}

        def fake_get_collections():
            return {
                'users': fake_users_coll,
                'limit_up_ladder': fake_ladder_coll,
                'daily_limit_up': fake_daily_coll
            }
        
        import db
        monkeypatch.setattr(db, 'get_db_collections', fake_get_collections)
        
        result = generate_market_analysis_task()
        
        assert result['success'] is False
        assert result['reason'] == 'no_admin_user'

    def test_admin_no_llm_config(self, monkeypatch):
        """If admin has no LLM config, task should fail."""
        from celery_app import generate_market_analysis_task
        
        import routers.market as market_router
        monkeypatch.setattr(
            market_router,
            'fetch_market_data',
            lambda: {'analysis_date': '2025-12-22', 'market_data': {}}
        )
        
        # Mock admin user without LLM configs
        fake_users_coll = MagicMock()
        fake_users_coll.find_one.return_value = {
            '_id': 'admin-id',
            'username': 'admin',
            'is_admin': True,
            'llm_configs': [],
            'active_llm_config_id': None
        }
        
        # Mock ladder data with return values
        fake_ladder_coll = MagicMock()
        fake_ladder_coll.find_one.return_value = {"sentiment_score": 85, "sentiment_mood": "乐观"}
        fake_daily_coll = MagicMock()
        fake_daily_coll.find_one.return_value = {"limit_up_count": 50}

        def fake_get_collections():
            return {
                'users': fake_users_coll,
                'limit_up_ladder': fake_ladder_coll,
                'daily_limit_up': fake_daily_coll
            }
        
        import db
        monkeypatch.setattr(db, 'get_db_collections', fake_get_collections)
        
        result = generate_market_analysis_task()
        
        assert result['success'] is False
        assert result['reason'] == 'no_active_llm_config'

    def test_successful_analysis_generation(self, monkeypatch):
        """Happy path: admin exists, LLM runs, cache saves."""
        from celery_app import generate_market_analysis_task
        
        # Mock fetch_market_data
        import routers.market as market_router
        monkeypatch.setattr(
            market_router,
            'fetch_market_data',
            lambda: {
                'analysis_date': '2025-12-22',
                'market_data': {'1A0001': {'symbol': '1A0001'}}
            }
        )
        
        # Mock admin user with valid LLM config
        fake_users_coll = MagicMock()
        fake_users_coll.find_one.return_value = {
            '_id': 'admin-id',
            'username': 'admin',
            'is_admin': True,
            'llm_configs': [
                {
                    '_id': 'config-1',
                    'provider': 'openai',
                    'model': 'gpt-4',
                    'api_key': 'test-key',
                    'base_url': None
                }
            ],
            'active_llm_config_id': 'config-1'
        }
        
        # Mock ladder data
        fake_ladder_coll = MagicMock()
        fake_ladder_coll.find_one.return_value = {
            "date": "2025-12-22",
            "sentiment_score": 85,
            "sentiment_mood": "乐观"
        }
        
        fake_daily_coll = MagicMock()
        fake_daily_coll.find_one.return_value = {
             "date": "2025-12-22",
             "limit_up_count": 50
        }

        def fake_get_collections():
            return {
                'users': fake_users_coll,
                'limit_up_ladder': fake_ladder_coll,
                'daily_limit_up': fake_daily_coll
            }
        
        import db
        monkeypatch.setattr(db, 'get_db_collections', fake_get_collections)
        
        # Mock llm_manager.get_service
        fake_llm_service = MagicMock()
        
        # Mock the async analyze_market method
        async def fake_analyze(bundle):
            return {
                'success': True,
                'analysis': {
                    'mood': '乐观',
                    'summary': '市场强劲',
                    'keyPoints': ['上涨'],
                    'outlook': '继续上涨',
                    'riskLevel': 'low',
                    'riskNote': '风险可控'
                },
                'provider': 'openai',
                'model': 'gpt-4'
            }
        
        fake_llm_service.analyze_market = AsyncMock(side_effect=fake_analyze)
        
        from llm_service import llm_manager
        monkeypatch.setattr(
            llm_manager,
            'get_service',
            lambda **kwargs: fake_llm_service
        )
        
        # Mock market_analysis_cache.save_analysis
        from market_analysis_cache import market_analysis_cache
        monkeypatch.setattr(
            market_analysis_cache,
            'save_analysis',
            lambda result, date_str: True
        )
        
        result = generate_market_analysis_task()
        
        assert result['success'] is True
        assert result['analysis_date'] == '2025-12-22'

    def test_llm_analysis_failure(self, monkeypatch):
        """If LLM analysis fails, task should return failure."""
        from celery_app import generate_market_analysis_task
        
        import routers.market as market_router
        monkeypatch.setattr(
            market_router,
            'fetch_market_data',
            lambda: {'analysis_date': '2025-12-22', 'market_data': {}}
        )
        
        fake_users_coll = MagicMock()
        fake_users_coll.find_one.return_value = {
            '_id': 'admin-id',
            'username': 'admin',
            'is_admin': True,
            'llm_configs': [
                {
                    '_id': 'config-1',
                    'provider': 'openai',
                    'model': 'gpt-4',
                    'api_key': 'test-key',
                    'base_url': None
                }
            ],
            'active_llm_config_id': 'config-1'
        }
        
        # Mock ladder data with return values
        fake_ladder_coll = MagicMock()
        fake_ladder_coll.find_one.return_value = {"sentiment_score": 85, "sentiment_mood": "乐观"}
        fake_daily_coll = MagicMock()
        fake_daily_coll.find_one.return_value = {"limit_up_count": 50}

        def fake_get_collections():
            return {
                'users': fake_users_coll,
                'limit_up_ladder': fake_ladder_coll,
                'daily_limit_up': fake_daily_coll
            }
        
        import db
        monkeypatch.setattr(db, 'get_db_collections', fake_get_collections)
        
        # Mock LLM failure
        fake_llm_service = MagicMock()
        
        async def fake_analyze_fail(bundle):
            return {
                'success': False,
                'error': 'LLM API timeout'
            }
        
        fake_llm_service.analyze_market = AsyncMock(side_effect=fake_analyze_fail)
        
        from llm_service import llm_manager
        monkeypatch.setattr(
            llm_manager,
            'get_service',
            lambda **kwargs: fake_llm_service
        )
        
        result = generate_market_analysis_task()
        
        assert result['success'] is False
        assert result['reason'] == 'llm_analysis_failed'
        assert 'LLM API timeout' in result['error']
