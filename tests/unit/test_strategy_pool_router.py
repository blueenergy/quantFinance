"""
Unit tests for strategy_pool router endpoints.

Tests the /api/strategy-pool endpoints that serve the Strategy Stock Pool UI:
- GET /dates - Returns available dates for a strategy
- GET /stocks - Returns stocks for a specific date and strategy

Critical bug tested: Strategy filtering must work correctly to prevent
showing hidden_dragon stocks when single_yang is selected.
"""

import pytest
from datetime import datetime
from tests.conftest import FakeCollection


@pytest.mark.unit
def test_get_available_dates_with_strategy(client, monkeypatch):
    """Test /api/strategy-pool/dates filters by strategy correctly."""
    from routers import strategy_pool
    from auth import get_current_user
    
    # Mock authentication
    def mock_auth():
        return {'id': 'user1', 'username': 'testuser'}
    
    client.app.dependency_overrides[get_current_user] = mock_auth
    
    # Mock data: different dates for different strategies
    pool_data = [
        {'date': '20260109', 'strategy': 'hidden_dragon', 'symbol': '000001.SZ'},
        {'date': '20260108', 'strategy': 'hidden_dragon', 'symbol': '000002.SZ'},
        {'date': '20260107', 'strategy': 'hidden_dragon', 'symbol': '000003.SZ'},
        {'date': '20260109', 'strategy': 'single_yang', 'symbol': '000004.SZ'},
        {'date': '20260108', 'strategy': 'single_yang', 'symbol': '000005.SZ'},
        {'date': '20260106', 'strategy': 'single_yang', 'symbol': '000006.SZ'},
    ]
    
    fake_col = FakeCollection(pool_data)
    
    def mock_get_collections():
        return {'strategy_stock_pool': fake_col}
    
    monkeypatch.setattr('routers.strategy_pool.get_db_collections', mock_get_collections)
    
    # Test hidden_dragon dates
    response = client.get('/api/strategy-pool/dates?strategy=hidden_dragon')
    assert response.status_code == 200
    data = response.json()
    assert data['success'] is True
    assert len(data['dates']) == 3
    assert '20260109' in data['dates']
    assert '20260108' in data['dates']
    assert '20260107' in data['dates']
    assert '20260106' not in data['dates']  # single_yang only
    
    # Test single_yang dates
    response = client.get('/api/strategy-pool/dates?strategy=single_yang')
    assert response.status_code == 200
    data = response.json()
    assert data['success'] is True
    assert len(data['dates']) == 3
    assert '20260109' in data['dates']
    assert '20260108' in data['dates']
    assert '20260106' in data['dates']
    assert '20260107' not in data['dates']  # hidden_dragon only
    
    # Cleanup
    client.app.dependency_overrides.clear()


@pytest.mark.unit
def test_get_available_dates_no_strategy_filter(client, monkeypatch):
    """Test /api/strategy-pool/dates without strategy filter returns all dates."""
    from auth import get_current_user
    
    def mock_auth():
        return {'id': 'user1', 'username': 'testuser'}
    
    client.app.dependency_overrides[get_current_user] = mock_auth
    
    pool_data = [
        {'date': '20260109', 'strategy': 'hidden_dragon', 'symbol': '000001.SZ'},
        {'date': '20260108', 'strategy': 'single_yang', 'symbol': '000002.SZ'},
        {'date': '20260107', 'strategy': 'hidden_dragon', 'symbol': '000003.SZ'},
    ]
    
    fake_col = FakeCollection(pool_data)
    
    def mock_get_collections():
        return {'strategy_stock_pool': fake_col}
    
    monkeypatch.setattr('routers.strategy_pool.get_db_collections', mock_get_collections)
    
    response = client.get('/api/strategy-pool/dates')
    assert response.status_code == 200
    data = response.json()
    assert data['success'] is True
    assert len(data['dates']) == 3
    
    client.app.dependency_overrides.clear()


@pytest.mark.unit
def test_get_pool_stocks_with_strategy_filter(client, monkeypatch):
    """
    CRITICAL TEST: Verify stocks endpoint filters by BOTH date AND strategy.
    
    This prevents the bug where selecting single_yang would show hidden_dragon stocks.
    """
    from auth import get_current_user
    
    def mock_auth():
        return {'id': 'user1', 'username': 'testuser'}
    
    client.app.dependency_overrides[get_current_user] = mock_auth
    
    # Same date, different strategies, different stocks
    pool_data = [
        {
            'date': '20260109',
            'strategy': 'hidden_dragon',
            'symbol': '000001.SZ',
            'name': '平安银行',
            'action': 'BUY',
            'price': 10.5
        },
        {
            'date': '20260109',
            'strategy': 'hidden_dragon',
            'symbol': '000002.SZ',
            'name': '万科A',
            'action': 'BUY',
            'price': 8.2
        },
        {
            'date': '20260109',
            'strategy': 'single_yang',
            'symbol': '000003.SZ',
            'name': '京东方A',
            'action': 'BUY',
            'price': 3.8
        },
        {
            'date': '20260109',
            'strategy': 'single_yang',
            'symbol': '000004.SZ',
            'name': '国华网安',
            'action': 'BUY',
            'price': 12.6
        },
    ]
    
    fake_col = FakeCollection(pool_data)
    
    def mock_get_collections():
        return {'strategy_stock_pool': fake_col}
    
    monkeypatch.setattr('routers.strategy_pool.get_db_collections', mock_get_collections)
    
    # Test hidden_dragon stocks
    response = client.get('/api/strategy-pool/stocks?date=20260109&strategy=hidden_dragon')
    assert response.status_code == 200
    data = response.json()
    assert data['success'] is True
    assert data['count'] == 2
    assert len(data['stocks']) == 2
    
    symbols = [s['symbol'] for s in data['stocks']]
    assert '000001.SZ' in symbols
    assert '000002.SZ' in symbols
    assert '000003.SZ' not in symbols  # single_yang stock
    assert '000004.SZ' not in symbols  # single_yang stock
    
    # Verify all returned stocks have correct strategy
    for stock in data['stocks']:
        assert stock['strategy'] == 'hidden_dragon'
    
    # Test single_yang stocks
    response = client.get('/api/strategy-pool/stocks?date=20260109&strategy=single_yang')
    assert response.status_code == 200
    data = response.json()
    assert data['success'] is True
    assert data['count'] == 2
    assert len(data['stocks']) == 2
    
    symbols = [s['symbol'] for s in data['stocks']]
    assert '000003.SZ' in symbols
    assert '000004.SZ' in symbols
    assert '000001.SZ' not in symbols  # hidden_dragon stock
    assert '000002.SZ' not in symbols  # hidden_dragon stock
    
    # Verify all returned stocks have correct strategy
    for stock in data['stocks']:
        assert stock['strategy'] == 'single_yang'
    
    client.app.dependency_overrides.clear()


@pytest.mark.unit
def test_get_available_presets_for_strategies(client, monkeypatch):
    """Test /api/strategy-pool/presets returns correct preset names per strategy."""
    from auth import get_current_user

    def mock_auth():
        return {'id': 'user1', 'username': 'testuser'}

    client.app.dependency_overrides[get_current_user] = mock_auth

    pool_data = [
        # hidden_dragon presets (note: use dragon_* naming)
        {'date': '20260109', 'strategy': 'hidden_dragon', 'preset': 'dragon_conservative', 'symbol': '000001.SZ'},
        {'date': '20260109', 'strategy': 'hidden_dragon', 'preset': 'dragon_default', 'symbol': '000002.SZ'},
        {'date': '20260109', 'strategy': 'hidden_dragon', 'preset': 'dragon_aggressive', 'symbol': '000003.SZ'},
        # single_yang presets (use yang_* naming)
        {'date': '20260109', 'strategy': 'single_yang', 'preset': 'yang_conservative', 'symbol': '000004.SZ'},
        {'date': '20260109', 'strategy': 'single_yang', 'preset': 'yang_default', 'symbol': '000005.SZ'},
        {'date': '20260109', 'strategy': 'single_yang', 'preset': 'yang_aggressive', 'symbol': '000006.SZ'},
        # turtle presets
        {'date': '20260109', 'strategy': 'turtle', 'preset': 'turtle_conservative', 'symbol': '000007.SZ'},
        {'date': '20260109', 'strategy': 'turtle', 'preset': 'turtle_standard', 'symbol': '000008.SZ'},
        {'date': '20260109', 'strategy': 'turtle', 'preset': 'turtle_aggressive', 'symbol': '000009.SZ'},
    ]

    fake_col = FakeCollection(pool_data)

    def mock_get_collections():
        return {'strategy_stock_pool': fake_col}

    monkeypatch.setattr('routers.strategy_pool.get_db_collections', mock_get_collections)

    # hidden_dragon should return dragon_* presets
    res = client.get('/api/strategy-pool/presets?strategy=hidden_dragon')
    assert res.status_code == 200
    data = res.json()
    assert data['success'] is True
    assert data['strategy'] == 'hidden_dragon'
    assert set(data['presets']) == {'dragon_conservative', 'dragon_default', 'dragon_aggressive'}

    # single_yang should return yang_* presets
    res = client.get('/api/strategy-pool/presets?strategy=single_yang')
    assert res.status_code == 200
    data = res.json()
    assert data['success'] is True
    assert data['strategy'] == 'single_yang'
    assert set(data['presets']) == {'yang_conservative', 'yang_default', 'yang_aggressive'}

    # turtle should return turtle_* presets
    res = client.get('/api/strategy-pool/presets?strategy=turtle')
    assert res.status_code == 200
    data = res.json()
    assert data['success'] is True
    assert data['strategy'] == 'turtle'
    assert set(data['presets']) == {'turtle_conservative', 'turtle_standard', 'turtle_aggressive'}

    client.app.dependency_overrides.clear()


@pytest.mark.unit
def test_get_pool_stocks_empty_result(client, monkeypatch):
    """Test stocks endpoint returns empty list when no data matches."""
    from auth import get_current_user
    
    def mock_auth():
        return {'id': 'user1', 'username': 'testuser'}
    
    client.app.dependency_overrides[get_current_user] = mock_auth
    
    pool_data = [
        {'date': '20260108', 'strategy': 'hidden_dragon', 'symbol': '000001.SZ'},
    ]
    
    fake_col = FakeCollection(pool_data)
    
    def mock_get_collections():
        return {'strategy_stock_pool': fake_col}
    
    monkeypatch.setattr('routers.strategy_pool.get_db_collections', mock_get_collections)
    
    # Query non-existent date
    response = client.get('/api/strategy-pool/stocks?date=20260109&strategy=hidden_dragon')
    assert response.status_code == 200
    data = response.json()
    assert data['success'] is True
    assert data['count'] == 0
    assert len(data['stocks']) == 0
    
    client.app.dependency_overrides.clear()


@pytest.mark.unit
def test_get_pool_stocks_no_collection(client, monkeypatch):
    """Test stocks endpoint handles missing collection gracefully."""
    from auth import get_current_user
    
    def mock_auth():
        return {'id': 'user1', 'username': 'testuser'}
    
    client.app.dependency_overrides[get_current_user] = mock_auth
    
    def mock_get_collections():
        return {}  # No strategy_stock_pool collection
    
    monkeypatch.setattr('routers.strategy_pool.get_db_collections', mock_get_collections)
    
    response = client.get('/api/strategy-pool/stocks?date=20260109&strategy=hidden_dragon')
    assert response.status_code == 200
    data = response.json()
    assert data['success'] is True
    assert data['stocks'] == []
    
    client.app.dependency_overrides.clear()


@pytest.mark.unit
def test_dates_sorted_descending(client, monkeypatch):
    """Test dates are returned in descending order (newest first)."""
    from auth import get_current_user
    
    def mock_auth():
        return {'id': 'user1', 'username': 'testuser'}
    
    client.app.dependency_overrides[get_current_user] = mock_auth
    
    pool_data = [
        {'date': '20260105', 'strategy': 'hidden_dragon', 'symbol': '000001.SZ'},
        {'date': '20260109', 'strategy': 'hidden_dragon', 'symbol': '000002.SZ'},
        {'date': '20260107', 'strategy': 'hidden_dragon', 'symbol': '000003.SZ'},
        {'date': '20260108', 'strategy': 'hidden_dragon', 'symbol': '000004.SZ'},
    ]
    
    fake_col = FakeCollection(pool_data)
    
    def mock_get_collections():
        return {'strategy_stock_pool': fake_col}
    
    monkeypatch.setattr('routers.strategy_pool.get_db_collections', mock_get_collections)
    
    response = client.get('/api/strategy-pool/dates?strategy=hidden_dragon')
    assert response.status_code == 200
    data = response.json()
    
    dates = data['dates']
    assert dates == ['20260109', '20260108', '20260107', '20260105']
    
    client.app.dependency_overrides.clear()


@pytest.mark.unit
def test_authentication_required(client):
    """Test both endpoints require authentication."""
    # Without auth, should get 401 or 403
    response = client.get('/api/strategy-pool/dates')
    assert response.status_code in [401, 403]
    
    response = client.get('/api/strategy-pool/stocks?date=20260109&strategy=hidden_dragon')
    assert response.status_code in [401, 403]


@pytest.mark.unit
def test_get_preset_params_from_templates(client, monkeypatch):
    """Test /api/strategy-pool/params returns params from strategy_template with preset normalization."""
    from auth import get_current_user

    def mock_auth():
        return {'id': 'user1', 'username': 'testuser'}

    client.app.dependency_overrides[get_current_user] = mock_auth

    # Templates store prefixed presets for hidden_dragon
    templates = [
        {
            'strategy': 'hidden_dragon',
            'preset': 'dragon_default',
            'is_default': True,
            'params_with_desc': {
                'limit_up_rate': {'value': 0.095, 'description': 'x'},
                'max_callback_days': {'value': 20, 'description': 'y'},
            }
        },
        {
            'strategy': 'turtle',
            'preset': 'turtle_standard',
            'is_default': True,
            'params': {
                'entry_window': 55,
                'exit_window': 10,
            }
        },
    ]

    from tests.conftest import FakeCollection
    fake_tpl = FakeCollection(templates)

    def mock_get_collections():
        return {'strategy_template': fake_tpl}

    monkeypatch.setattr('routers.strategy_pool.get_db_collections', mock_get_collections)

    # Exact preset
    res = client.get('/api/strategy-pool/params?strategy=hidden_dragon&preset=dragon_default')
    assert res.status_code == 200
    data = res.json()
    assert data['success'] is True
    assert data['found'] is True
    assert data['preset'] == 'dragon_default'
    assert data['params']['limit_up_rate'] == 0.095
    assert data['params']['max_callback_days'] == 20

    # Unprefixed preset should still resolve (default -> dragon_default)
    res = client.get('/api/strategy-pool/params?strategy=hidden_dragon&preset=default')
    assert res.status_code == 200
    data = res.json()
    assert data['success'] is True
    assert data['found'] is True
    assert data['preset'] == 'dragon_default'
    assert data['params']['limit_up_rate'] == 0.095

    # Missing preset: should return default template
    res = client.get('/api/strategy-pool/params?strategy=turtle')
    assert res.status_code == 200
    data = res.json()
    assert data['success'] is True
    assert data['found'] is True
    assert data['preset'] == 'turtle_standard'
    assert data['params']['entry_window'] == 55

    client.app.dependency_overrides.clear()
