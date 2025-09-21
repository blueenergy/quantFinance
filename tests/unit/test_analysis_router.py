import pytest
from routers import analysis


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = docs or []

    def find(self, query=None, sort=None):
        class Cursor(list):
            def limit(self, n):
                return list(self)[:n]
        return Cursor(self.docs)

    def find_one(self, query=None, sort=None):
        return self.docs[0] if self.docs else None


class FakeLLMService:
    async def analyze_stock(self, symbol, stock_data):
        return {"analysis": f"analysis for {symbol}", "success": True}


class FakeLLMManager:
    def get_service(self, provider=None, model=None):
        return FakeLLMService()


@pytest.mark.unit
def test_analyze_stock_happy_path(monkeypatch):
    # prepare fake market/volume and money_flow data for get_stock_data_for_analysis
    volume_docs = [
        {"symbol": "000001", "trade_date": __import__('datetime').date(2025, 9, 20), "price": 100},
    ]
    money_flow_docs = [
        {"symbol": "000001", "trade_date": __import__('datetime').date(2025, 9, 20), "flow": 123}
    ]

    fake_collections = {
        'volume_price': FakeCollection(volume_docs),
        'money_flow': FakeCollection(money_flow_docs)
    }

    monkeypatch.setattr(analysis, 'get_db_collections', lambda: fake_collections)
    monkeypatch.setattr(analysis.llm_manager, 'get_service', lambda provider=None, model=None: FakeLLMService())

    # mock authenticated user dependency
    # Use FastAPI dependency override so the route receives a fake current_user
    from fastapi.testclient import TestClient
    from main import app

    client = TestClient(app)
    client.app.dependency_overrides[analysis.get_current_active_user] = lambda: {"id": "u1", "username": "tester"}
    payload = {"symbol": "000001", "start_date": "2025-09-01", "end_date": "2025-09-20", "prompt": "analyze"}
    # TestClient will run the async endpoint in the event loop, no pytest-asyncio needed
    resp = client.post('/api/analyze-stock', json=payload)
    assert resp.status_code == 200
    j = resp.json()
    assert j.get('user_id') == 'u1'
    assert 'analysis' in j
    # cleanup dependency overrides
    client.app.dependency_overrides.pop(analysis.get_current_active_user, None)


@pytest.mark.unit
def test_get_stock_data_for_analysis_none(monkeypatch):
    # empty DB should return None
    fake_collections = {
        'volume_price': FakeCollection([]),
        'money_flow': FakeCollection([])
    }
    monkeypatch.setattr(analysis, 'get_db_collections', lambda: fake_collections)

    res = analysis.get_stock_data_for_analysis('XXXX')
    assert res is not None
    # latest_record should be None, historical_records empty
    assert res['latest_record'] is None
    assert res['historical_records'] == []
