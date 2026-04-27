import pytest
from fastapi.testclient import TestClient
from routers import stock_ranking
from main import app

# Small in-memory fake collection classes to mimic minimal pymongo API used in router
class FakeCollection:
    def __init__(self, docs=None):
        self.docs = docs or []

    def find(self, query=None, sort=None):
        # return list-like cursor with limit
        class Cursor(list):
            def limit(self, n):
                return list(self)[:n]
        return Cursor(self.docs)

    def aggregate(self, pipeline):
        # A very small, naive implementation for tests that only needs to support
        # the router's simple aggregate used to find latest per symbol.
        # For our test data we can simplify by returning docs as-is sorted by composite_score
        return sorted(self.docs, key=lambda d: stock_ranking.extract_strategy_score(d.get('composite_score', 0), 'balanced'), reverse=True)

    def find_one(self, query):
        # simplistic matcher for symbol
        for d in self.docs:
            if all(item in d.items() for item in query.items()):
                return d
        return None


class FakeDB:
    def __init__(self, stock_scores_docs=None, stock_info_docs=None):
        self._collections = {
            'stock_scores': FakeCollection(stock_scores_docs or []),
            'stock_info': FakeCollection(stock_info_docs or [])
        }

    def __getitem__(self, key):
        return self._collections[key]


class FakeVolumePriceCollection:
    """Supports find_one with sort and trade_date $lte for return_since_score tests."""

    def __init__(self, docs):
        self.docs = list(docs)

    def find_one(self, query, sort=None):
        sym = query.get("symbol")
        rows = [d for d in self.docs if d.get("symbol") == sym]
        td_flt = query.get("trade_date")
        if isinstance(td_flt, dict) and "$lte" in td_flt:
            ub = str(td_flt["$lte"])
            rows = [d for d in rows if str(d.get("trade_date", "")) <= ub]
        if not rows:
            return None
        if sort:
            k, direction = sort[0]
            assert k == "trade_date"
            rows.sort(key=lambda d: str(d.get("trade_date", "")), reverse=(direction == -1))
        return rows[0]


def _vp_ranking_pair():
    return [
        {"symbol": "AAA.SH", "trade_date": "20250901", "close": 50},
        {"symbol": "AAA.SH", "trade_date": "20250920", "close": 100},
        {"symbol": "AAA.SH", "trade_date": "20251001", "close": 110},
        {"symbol": "BBB.SH", "trade_date": "20250920", "close": 200},
        {"symbol": "BBB.SH", "trade_date": "20251001", "close": 180},
    ]


@pytest.mark.unit
def test_stock_rankings_sorts_mixed_composite(monkeypatch):
    # composite_score: one doc with dict, one with numeric
    docs = [
        {'symbol': 'AAA.SH', 'composite_score': {'balanced': 75}, 'cycle_score': 10, 'value_score': 20, 'fundamental_score': 30, 'growth_score': 5, 'technical_score': 7, 'money_flow_score': 3, 'score_date': '20250920'},
        {'symbol': 'BBB.SH', 'composite_score': 80, 'cycle_score': 12, 'value_score': 21, 'fundamental_score': 31, 'growth_score': 6, 'technical_score': 6, 'money_flow_score': 4, 'score_date': '20250920'},
    ]
    info_docs = [{'symbol': 'AAA.SH', 'name': 'A Corp', 'industry': 'Tech'}, {'symbol': 'BBB.SH', 'name': 'B Corp', 'industry': 'Finance'}]
    fake_db = FakeDB(stock_scores_docs=docs, stock_info_docs=info_docs)

    def fake_get_collections():
        return fake_db

    monkeypatch.setattr(stock_ranking, 'get_db_collections', fake_get_collections)
    monkeypatch.setattr(
        stock_ranking,
        'get_data_db',
        lambda: {'volume_price': FakeVolumePriceCollection(_vp_ranking_pair())},
    )

    client = TestClient(app)
    resp = client.get('/api/stock-rankings?limit=2&strategy=balanced')
    assert resp.status_code == 200
    data = resp.json()
    # BBB has numeric 80 > AAA balanced 75, so BBB should be first
    assert data['data'][0]['symbol'] == 'BBB.SH'
    assert data['data'][1]['symbol'] == 'AAA.SH'
    assert data['data'][0]['return_since_score_pct'] == pytest.approx(-10.0)
    assert data['data'][1]['return_since_score_pct'] == pytest.approx(10.0)
    assert data['data'][1]['price_base_trade_date'] == '20250920'
    assert data['data'][1]['price_latest_trade_date'] == '20251001'


@pytest.mark.unit
def test_selected_stock_rankings_defaults_and_fields(monkeypatch):
    docs = [
        {'symbol': 'CCC.SH', 'composite_score': {'balanced': 60}, 'cycle_score': 11, 'value_score': 19, 'fundamental_score': 25, 'growth_score': 6, 'technical_score': 9, 'money_flow_score': 5, 'score_date': '20250919'},
        {'symbol': 'DDD.SH', 'composite_score': 55, 'cycle_score': 9, 'value_score': 18, 'fundamental_score': 22, 'growth_score': 7, 'technical_score': 8, 'money_flow_score': 2, 'score_date': '20250919'},
    ]
    info_docs = [{'symbol': 'CCC.SH', 'name': 'C Corp', 'industry': 'Energy'}, {'symbol': 'DDD.SH', 'name': 'D Corp', 'industry': 'Retail'}]
    fake_db = FakeDB(stock_scores_docs=docs, stock_info_docs=info_docs)

    def fake_get_collections():
        return fake_db

    monkeypatch.setattr(stock_ranking, 'get_db_collections', fake_get_collections)
    vp = [
        {"symbol": "CCC.SH", "trade_date": "20250910", "close": 80},
        {"symbol": "CCC.SH", "trade_date": "20250919", "close": 100},
        {"symbol": "CCC.SH", "trade_date": "20251005", "close": 115},
        {"symbol": "DDD.SH", "trade_date": "20250919", "close": 50},
        {"symbol": "DDD.SH", "trade_date": "20251005", "close": 50},
    ]
    monkeypatch.setattr(
        stock_ranking,
        'get_data_db',
        lambda: {'volume_price': FakeVolumePriceCollection(vp)},
    )

    client = TestClient(app)
    body = {'symbols': ['CCC.SH', 'DDD.SH']}
    resp = client.post('/api/stock-rankings/selected', json=body)
    assert resp.status_code == 200
    j = resp.json()
    assert j['total'] == 2
    for s in j['data']:
        assert 'technical_score' in s
        assert isinstance(s['technical_score'], (int, float))
        assert 'composite_score' in s
        assert isinstance(s['composite_score'], dict)
        assert 'return_since_score_pct' in s
    by_sym = {s['symbol']: s for s in j['data']}
    assert by_sym['CCC.SH']['return_since_score_pct'] == pytest.approx(15.0)
    assert by_sym['DDD.SH']['return_since_score_pct'] == pytest.approx(0.0)


@pytest.mark.unit
def test_selected_multi_date_per_date_return_since(monkeypatch):
    docs = [
        {'symbol': 'EEE.SH', 'composite_score': {'balanced': 70}, 'cycle_score': 1, 'value_score': 2, 'fundamental_score': 3, 'growth_score': 4, 'technical_score': 5, 'money_flow_score': 6, 'score_date': '20250910'},
        {'symbol': 'EEE.SH', 'composite_score': {'balanced': 71}, 'cycle_score': 1, 'value_score': 2, 'fundamental_score': 3, 'growth_score': 4, 'technical_score': 5, 'money_flow_score': 6, 'score_date': '20250920'},
    ]
    info_docs = [{'symbol': 'EEE.SH', 'name': 'E Corp', 'industry': 'X'}]
    fake_db = FakeDB(stock_scores_docs=docs, stock_info_docs=info_docs)

    monkeypatch.setattr(stock_ranking, 'get_db_collections', lambda: fake_db)
    vp = [
        {"symbol": "EEE.SH", "trade_date": "20250905", "close": 10},
        {"symbol": "EEE.SH", "trade_date": "20250910", "close": 10},
        {"symbol": "EEE.SH", "trade_date": "20250920", "close": 20},
        {"symbol": "EEE.SH", "trade_date": "20251001", "close": 40},
    ]
    monkeypatch.setattr(
        stock_ranking,
        'get_data_db',
        lambda: {'volume_price': FakeVolumePriceCollection(vp)},
    )

    client = TestClient(app)
    resp = client.post(
        '/api/stock-rankings/selected',
        json={'symbols': ['EEE.SH'], 'dates': ['20250910', '20250920']},
    )
    assert resp.status_code == 200
    j = resp.json()
    assert j['total'] == 1
    row = j['data'][0]
    assert 'per_date_return_since' in row
    pr = row['per_date_return_since']
    # 20250910 base close 10, latest 40 -> +300%
    assert pr['20250910']['return_since_score_pct'] == pytest.approx(300.0)
    # 20250920 base close 20, latest 40 -> +100%
    assert pr['20250920']['return_since_score_pct'] == pytest.approx(100.0)
