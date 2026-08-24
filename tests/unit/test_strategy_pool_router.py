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
from tests.conftest import FakeCollection, Cursor


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
def test_get_preset_params_supports_prefixed_presets_and_params_with_desc(client, monkeypatch):
    """Test /api/strategy-pool/params resolves preset name variants and extracts params_with_desc."""
    from auth import get_current_user

    def mock_auth():
        return {'id': 'user1', 'username': 'testuser'}

    client.app.dependency_overrides[get_current_user] = mock_auth

    # Strategy template uses prefixed preset name dragon_default
    templates = FakeCollection([
        {
            'strategy': 'hidden_dragon',
            'preset': 'dragon_default',
            'params_with_desc': {
                'lookback': {'value': 20, 'description': 'x'},
                'threshold': {'value': 0.5, 'description': 'y'},
            },
            'is_default': True,
        }
    ])

    def mock_get_collections():
        return {'strategy_template': templates}

    monkeypatch.setattr('routers.strategy_pool.get_db_collections', mock_get_collections)

    # Request unprefixed preset should still find prefixed template
    res = client.get('/api/strategy-pool/params?strategy=hidden_dragon&preset=default')
    assert res.status_code == 200
    body = res.json()
    assert body['success'] is True
    assert body['found'] is True
    assert body['requested_preset'] == 'default'
    assert body['preset'] == 'dragon_default'
    assert body['params'] == {'lookback': 20, 'threshold': 0.5}

    client.app.dependency_overrides.clear()


@pytest.mark.unit
def test_get_preset_params_prefers_params_field_when_present(client, monkeypatch):
    """If both params and params_with_desc exist, endpoint should return params."""
    from auth import get_current_user

    def mock_auth():
        return {'id': 'user1', 'username': 'testuser'}

    client.app.dependency_overrides[get_current_user] = mock_auth

    templates = FakeCollection([
        {
            'strategy': 'turtle',
            'preset': 'turtle_standard',
            'params': {'period': 55},
            'params_with_desc': {'period': {'value': 20, 'description': 'ignored'}},
        }
    ])

    def mock_get_collections():
        return {'strategy_template': templates}

    monkeypatch.setattr('routers.strategy_pool.get_db_collections', mock_get_collections)

    res = client.get('/api/strategy-pool/params?strategy=turtle&preset=turtle_standard')
    assert res.status_code == 200
    body = res.json()
    assert body['success'] is True
    assert body['found'] is True
    assert body['params'] == {'period': 55}

    client.app.dependency_overrides.clear()


class _TradeCursor(Cursor):
    def sort(self, key, direction):
        reverse = direction == -1
        return _TradeCursor(sorted(list(self), key=lambda d: d.get(key) or '', reverse=reverse))


class _TradeHistoryCollection:
    """Fake trade history collection supporting find(query).sort("datetime", 1) with date range ops."""

    def __init__(self, docs=None):
        self.docs = docs or []

    def find(self, query=None, projection=None):
        query = query or {}

        def match(doc):
            for k, v in query.items():
                if isinstance(v, dict) and k == 'date':
                    gte = v.get('$gte')
                    lte = v.get('$lte')
                    dv = doc.get('date')
                    if gte and (dv is None or dv < gte):
                        return False
                    if lte and (dv is None or dv > lte):
                        return False
                    continue
                if doc.get(k) != v:
                    return False
            return True

        return _TradeCursor([d for d in self.docs if match(d)])


@pytest.mark.unit
def test_get_strategy_pool_backtest_result_reconstructs_metrics_trades_equity_and_params_used(client, monkeypatch):
    from auth import get_current_user

    def mock_auth():
        return {'id': 'user1', 'username': 'testuser'}

    client.app.dependency_overrides[get_current_user] = mock_auth

    docs = [
        {
            'date': '20260108',
            'strategy': 'hidden_dragon',
            'preset': 'dragon_default',
            'symbol': '000001.SZ',
            'datetime': '2026-01-08 10:00:00',
            'action': 'BUY',
            'price': 10.0,
            'quantity': 100,
            'commission': 1.0,
            'pnl': 0.0,
            'cumulative_pnl': 0.0,
            'hist_return': 0.0,
            'hist_sharpe_ratio': 0.0,
            'hist_max_drawdown': 0.0,
            'hist_win_rate': 0.0,
            'hist_total_trades': 0,
        },
        {
            'date': '20260109',
            'strategy': 'hidden_dragon',
            'preset': 'dragon_default',
            'symbol': '000001.SZ',
            'datetime': '2026-01-09 10:00:00',
            'action': 'SELL',
            'price': 11.0,
            'quantity': 100,
            'commission': 1.0,
            'pnl': 100.0,
            'cumulative_pnl': 100.0,
            'params_used': {'lookback': 20, 'threshold': 0.5},
            'hist_return': 0.1,
            'hist_sharpe_ratio': 1.2,
            'hist_max_drawdown': -0.05,
            'hist_win_rate': 0.5,
            'hist_total_trades': 2,
        },
    ]

    trade_history = _TradeHistoryCollection(docs)

    def mock_get_collections():
        return {'strategy_trade_history': trade_history}

    monkeypatch.setattr('routers.strategy_pool.get_db_collections', mock_get_collections)

    res = client.get(
        '/api/strategy-pool/backtest-result'
        '?symbol=000001'
        '&strategy=hidden_dragon'
        '&preset=dragon_default'
        '&end_date=20260109'
        '&days_back=30'
        '&initial_cash=1000000'
    )
    assert res.status_code == 200
    body = res.json()

    # symbol normalized
    assert body['symbol'] == '000001.SZ'
    assert body['strategy_key'] == 'hidden_dragon'
    assert body['strategy_params'] == {'lookback': 20, 'threshold': 0.5}

    # metrics from last_doc
    assert body['metrics']['total_return'] == 0.1
    assert body['metrics']['sharpe_ratio'] == 1.2
    assert body['metrics']['max_drawdown'] == -0.05
    assert body['metrics']['win_rate'] == 0.5
    assert body['metrics']['total_trades'] == 2

    # trades normalized
    assert len(body['trades']) == 2
    assert body['trades'][0]['action'] == 'buy'
    assert body['trades'][1]['action'] == 'sell'
    assert body['trades'][1]['cumulative_pnl'] == 100.0

    # equity curve uses last trade per day
    assert body['equity_curve'] == [
        {'date': '20260108', 'value': 1000000.0},
        {'date': '20260109', 'value': 1000100.0},
    ]

    client.app.dependency_overrides.clear()


@pytest.mark.unit
def test_get_strategy_pool_backtest_result_invalid_end_date_returns_400(client, monkeypatch):
    from auth import get_current_user

    def mock_auth():
        return {'id': 'user1', 'username': 'testuser'}

    client.app.dependency_overrides[get_current_user] = mock_auth

    # Even if trade collection exists, invalid end_date should be rejected
    trade_history = _TradeHistoryCollection([])

    def mock_get_collections():
        return {'strategy_trade_history': trade_history}

    monkeypatch.setattr('routers.strategy_pool.get_db_collections', mock_get_collections)

    res = client.get('/api/strategy-pool/backtest-result?symbol=000001&strategy=hidden_dragon&end_date=2026-01-09')
    assert res.status_code == 400
    assert 'end_date' in res.json().get('detail', '')

    client.app.dependency_overrides.clear()


@pytest.mark.unit
def test_get_strategy_pool_backtest_result_no_trade_history_returns_404(client, monkeypatch):
    from auth import get_current_user

    def mock_auth():
        return {'id': 'user1', 'username': 'testuser'}

    client.app.dependency_overrides[get_current_user] = mock_auth

    trade_history = _TradeHistoryCollection([])

    def mock_get_collections():
        return {'strategy_trade_history': trade_history}

    monkeypatch.setattr('routers.strategy_pool.get_db_collections', mock_get_collections)

    res = client.get('/api/strategy-pool/backtest-result?symbol=000001&strategy=hidden_dragon&preset=dragon_default')
    assert res.status_code == 404
    assert 'No trade history' in (res.json().get('detail') or '')

    client.app.dependency_overrides.clear()


@pytest.mark.unit
def test_get_strategy_pool_backtest_result_strategy_params_null_when_params_used_missing(client, monkeypatch):
    from auth import get_current_user

    def mock_auth():
        return {'id': 'user1', 'username': 'testuser'}

    client.app.dependency_overrides[get_current_user] = mock_auth

    docs = [
        {
            'date': '20260109',
            'strategy': 'hidden_dragon',
            'preset': 'dragon_default',
            'symbol': '000001.SZ',
            'datetime': '2026-01-09 10:00:00',
            'action': 'BUY',
            'price': 10.0,
            'quantity': 100,
            'commission': 1.0,
            'pnl': 0.0,
            'cumulative_pnl': 0.0,
            'hist_return': 0.0,
            'hist_sharpe_ratio': 0.0,
            'hist_max_drawdown': 0.0,
            'hist_win_rate': 0.0,
            'hist_total_trades': 0,
        }
    ]

    trade_history = _TradeHistoryCollection(docs)

    def mock_get_collections():
        return {'strategy_trade_history': trade_history}

    monkeypatch.setattr('routers.strategy_pool.get_db_collections', mock_get_collections)

    res = client.get('/api/strategy-pool/backtest-result?symbol=000001&strategy=hidden_dragon&preset=dragon_default')
    assert res.status_code == 200
    body = res.json()
    assert body['strategy_params'] is None

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


@pytest.mark.unit
def test_get_strategy_pool_backtest_result_includes_params_used(client, monkeypatch):
    """Test /api/strategy-pool/backtest-result returns strategy_params from stored params_used."""
    from auth import get_current_user

    def mock_auth():
        return {'id': 'user1', 'username': 'testuser'}

    client.app.dependency_overrides[get_current_user] = mock_auth

    # Provide ordered docs (Cursor.sort is a no-op in FakeCollection)
    trade_docs = [
        {
            'date': '20260108',
            'strategy': 'hidden_dragon',
            'preset': 'dragon_aggressive',
            'symbol': '000001.SZ',
            'datetime': '2026-01-08 10:00:00',
            'action': 'BUY',
            'price': 10.0,
            'quantity': 100,
            'commission': 0.0,
            'pnl': 0.0,
            'cumulative_pnl': 0.0,
            'hist_win_rate': 0.6,
            'hist_total_trades': 10,
            'hist_return': 0.12,
            'hist_sharpe_ratio': 1.1,
            'hist_max_drawdown': -0.08,
        },
        {
            'date': '20260109',
            'strategy': 'hidden_dragon',
            'preset': 'dragon_aggressive',
            'symbol': '000001.SZ',
            'datetime': '2026-01-09 10:00:00',
            'action': 'SELL',
            'price': 11.0,
            'quantity': 100,
            'commission': 0.0,
            'pnl': 100.0,
            'cumulative_pnl': 100.0,
            'hist_win_rate': 0.6,
            'hist_total_trades': 10,
            'hist_return': 0.12,
            'hist_sharpe_ratio': 1.1,
            'hist_max_drawdown': -0.08,
            'params_used': {'limit_up_rate': 0.097, 'max_callback_days': 18},
        },
    ]

    fake_trade = FakeCollection(trade_docs)

    def mock_get_collections():
        return {'strategy_trade_history': fake_trade}

    monkeypatch.setattr('routers.strategy_pool.get_db_collections', mock_get_collections)

    res = client.get('/api/strategy-pool/backtest-result?symbol=000001.SZ&strategy=hidden_dragon&preset=dragon_aggressive')
    assert res.status_code == 200
    data = res.json()
    assert data['symbol'] == '000001.SZ'
    assert data['strategy_key'] == 'hidden_dragon'
    assert data['strategy_params'] == {'limit_up_rate': 0.097, 'max_callback_days': 18}
    assert data['metrics']['total_return'] == 0.12
    assert len(data['trades']) == 2

    client.app.dependency_overrides.clear()


@pytest.mark.unit
def test_chart_context_returns_kline_markers_and_forward_returns(client, monkeypatch):
    from auth import get_current_user

    def mock_auth():
        return {'id': 'user1', 'username': 'testuser'}

    client.app.dependency_overrides[get_current_user] = mock_auth

    docs = [
        {
            'date': '20260108',
            'strategy': 'hidden_dragon',
            'preset': 'dragon_default',
            'symbol': '000001.SZ',
            'datetime': '2026-01-08 10:00:00',
            'action': 'BUY',
            'price': 10.0,
            'quantity': 100,
            'pnl': 0.0,
        },
        {
            'date': '20260109',
            'strategy': 'hidden_dragon',
            'preset': 'dragon_default',
            'symbol': '000001.SZ',
            'datetime': '2026-01-09 10:00:00',
            'action': 'SELL',
            'price': 11.0,
            'quantity': 100,
            'pnl': 100.0,
        },
    ]

    def fake_kline(symbol, start_date, end_date, adjust='qfq'):
        assert symbol == '000001.SZ'
        assert adjust == 'qfq'
        assert start_date < '20260109' <= end_date
        return [
            {'trade_date': '20260107', 'open': 9.5, 'high': 9.8, 'low': 9.4, 'close': 9.6, 'volume': 1},
            {'trade_date': '20260108', 'open': 9.8, 'high': 10.2, 'low': 9.7, 'close': 10.0, 'volume': 2},
            {'trade_date': '20260109', 'open': 10.1, 'high': 11.2, 'low': 10.0, 'close': 11.0, 'volume': 3},
            {'trade_date': '20260112', 'open': 11.0, 'high': 11.4, 'low': 10.8, 'close': 11.2, 'volume': 3},
        ]

    monkeypatch.setattr('services.strategy_pool_chart.load_adjusted_kline', fake_kline)
    monkeypatch.setattr(
        'routers.strategy_pool.get_db_collections',
        lambda: {'strategy_trade_history': _TradeHistoryCollection(docs)},
    )

    res = client.get(
        '/api/strategy-pool/chart-context'
        '?symbol=000001&strategy=hidden_dragon&preset=dragon_default&signal_date=20260108'
    )
    assert res.status_code == 200
    body = res.json()
    assert body['success'] is True
    assert body['symbol'] == '000001.SZ'
    assert body['signal_date'] == '20260108'
    assert body['kline']['count'] == 4
    kinds = [m['kind'] for m in body['markers']]
    assert kinds == ['buy', 'sell']
    assert any(m['highlighted'] for m in body['markers'])
    assert body['forward_returns']['horizons'][0]['available'] is True
    assert '事后验证' in body['disclaimer']

    client.app.dependency_overrides.clear()


@pytest.mark.unit
def test_chart_context_rejects_bad_signal_date(client, monkeypatch):
    from auth import get_current_user

    client.app.dependency_overrides[get_current_user] = lambda: {'id': 'user1'}
    monkeypatch.setattr(
        'routers.strategy_pool.get_db_collections',
        lambda: {'strategy_trade_history': _TradeHistoryCollection([])},
    )
    res = client.get(
        '/api/strategy-pool/chart-context?symbol=000001.SZ&strategy=hidden_dragon&signal_date=today'
    )
    assert res.status_code == 400
    client.app.dependency_overrides.clear()
