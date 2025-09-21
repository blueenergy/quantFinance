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


@pytest.mark.unit
def test_stock_rankings_sorts_mixed_composite(monkeypatch):
    # composite_score: one doc with dict, one with numeric
    docs = [
        {'symbol': 'AAA', 'composite_score': {'balanced': 75}, 'cycle_score': 10, 'value_score': 20, 'fundamental_score': 30, 'growth_score': 5, 'technical_score': 7, 'money_flow_score': 3, 'score_date': '20250920'},
        {'symbol': 'BBB', 'composite_score': 80, 'cycle_score': 12, 'value_score': 21, 'fundamental_score': 31, 'growth_score': 6, 'technical_score': 6, 'money_flow_score': 4, 'score_date': '20250920'},
    ]
    info_docs = [{'symbol': 'AAA', 'name': 'A Corp', 'industry': 'Tech'}, {'symbol': 'BBB', 'name': 'B Corp', 'industry': 'Finance'}]
    fake_db = FakeDB(stock_scores_docs=docs, stock_info_docs=info_docs)

    def fake_get_collections():
        return fake_db

    monkeypatch.setattr(stock_ranking, 'get_db_collections', fake_get_collections)

    client = TestClient(app)
    resp = client.get('/api/stock-rankings?limit=2&strategy=balanced')
    assert resp.status_code == 200
    data = resp.json()
    print(data)
    # BBB has numeric 80 > AAA balanced 75, so BBB should be first
    assert data['data'][0]['symbol'] == 'BBB'
    assert data['data'][1]['symbol'] == 'AAA'


@pytest.mark.unit
def test_selected_stock_rankings_defaults_and_fields(monkeypatch):
    docs = [
        {'symbol': 'CCC', 'composite_score': {'balanced': 60}, 'cycle_score': 11, 'value_score': 19, 'fundamental_score': 25, 'growth_score': 6, 'technical_score': 9, 'money_flow_score': 5, 'score_date': '20250919'},
        {'symbol': 'DDD', 'composite_score': 55, 'cycle_score': 9, 'value_score': 18, 'fundamental_score': 22, 'growth_score': 7, 'technical_score': 8, 'money_flow_score': 2, 'score_date': '20250919'},
    ]
    info_docs = [{'symbol': 'CCC', 'name': 'C Corp', 'industry': 'Energy'}, {'symbol': 'DDD', 'name': 'D Corp', 'industry': 'Retail'}]
    fake_db = FakeDB(stock_scores_docs=docs, stock_info_docs=info_docs)

    def fake_get_collections():
        return fake_db

    monkeypatch.setattr(stock_ranking, 'get_db_collections', fake_get_collections)

    client = TestClient(app)
    body = {'symbols': ['CCC', 'DDD']}
    resp = client.post('/api/stock-rankings/selected', json=body)
    assert resp.status_code == 200
    j = resp.json()
    # should return two stocks
    assert j['total'] == 2
    # ensure numeric fields are present and rounded
    for s in j['data']:
        assert 'technical_score' in s
        assert isinstance(s['technical_score'], (int, float))
        assert 'composite_score' in s
        # composite_score should be a dict after formatting
        assert isinstance(s['composite_score'], dict)

