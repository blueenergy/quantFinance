import re
import pytest
from fastapi.testclient import TestClient
from routers import records
from main import app


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = docs or []

    def aggregate(self, pipeline):
        # Return docs as-is for tests
        return self.docs

    def find_one(self, query):
        for d in self.docs:
            # direct string compare
            if str(d.get('_id')) == str(query.get('_id')):
                return d
            # if qid is an ObjectId-like, extract 24-hex substring from either
            m_q = re.search(r'([0-9a-fA-F]{24})', str(query.get('_id')))
            m_d = re.search(r'([0-9a-fA-F]{24})', str(d.get('_id')))
            if m_q and m_d and m_q.group(1) == m_d.group(1):
                return d
        return None

    def find(self, query=None, sort=None):
        class Cursor(list):
            def sort(self, *args, **kwargs):
                return self

            def skip(self, n):
                return Cursor(list(self)[n:])

            def limit(self, n):
                return Cursor(list(self)[:n])

        return Cursor(self.docs)


@pytest.mark.unit
def test_get_records_and_project(monkeypatch):
    docs = [
        {'_id': '000000000000000000000001', 'symbol': '000001', 'trade_date': '20250920', 'open': 10, 'close': 12},
    ]
    fake_collections = {
        'volume_price': FakeCollection(docs),
        'stock_scores': FakeCollection([])
    }
    # override module-level collection in records.py
    monkeypatch.setattr(records, 'collection', fake_collections['volume_price'])

    client = TestClient(app)
    resp = client.get('/api/records/?symbol=000001&limit=1')
    assert resp.status_code == 200
    j = resp.json()
    assert isinstance(j, list)
    assert j[0]['symbol'] == '000001'


@pytest.mark.unit
def test_get_record_by_id(monkeypatch):
    docs = [
        {'_id': '000000000000000000000001', 'symbol': '000001', 'trade_date': '20250920', 'open': 10, 'close': 12},
    ]
    fake_collections = {'volume_price': FakeCollection(docs)}
    monkeypatch.setattr(records, 'collection', fake_collections['volume_price'])

    client = TestClient(app)
    # request the exact 24-hex id that is present in the fake collection
    resp = client.get('/api/record/000000000000000000000001')
    assert resp.status_code == 200
    j = resp.json()
    assert j['symbol'] == '000001'
