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

    def find_one(self, query=None, projection=None, sort=None):
        rows = list(self.docs)
        query = query or {}
        oid = query.get('_id')
        if oid is not None:
            for d in rows:
                if str(d.get('_id')) == str(oid):
                    return d
                m_q = re.search(r'([0-9a-fA-F]{24})', str(oid))
                m_d = re.search(r'([0-9a-fA-F]{24})', str(d.get('_id')))
                if m_q and m_d and m_q.group(1) == m_d.group(1):
                    return d
            return None
        if sort:
            field, direction = sort[0]
            rows.sort(key=lambda row: str(row.get(field) or ''), reverse=direction < 0)
        return rows[0] if rows else None

    def find(self, query=None, projection=None, sort=None):
        class Cursor(list):
            def sort(self, *args, **kwargs):
                return self

            def skip(self, n):
                return Cursor(list(self)[n:])

            def limit(self, n):
                return Cursor(list(self)[:n])

        return Cursor(self.docs)


@pytest.fixture
def empty_adj(monkeypatch):
    monkeypatch.setattr(records, 'adj_collection', FakeCollection([]))


@pytest.mark.unit
def test_get_records_and_project(monkeypatch, empty_adj):
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
    assert j[0]['adjust'] == 'qfq'
    assert j[0]['adj_degraded'] is True


@pytest.mark.unit
def test_get_records_attaches_decision_gs(monkeypatch, empty_adj):
    docs = [
        {
            '_id': '000000000000000000000001',
            'symbol': '000001',
            'trade_date': '20250920',
            'open': 10.0,
            'high': 11.0,
            'low': 9.0,
            'close': 12.0,
        },
        {
            '_id': '000000000000000000000002',
            'symbol': '000001',
            'trade_date': '20250921',
            'open': 12.0,
            'high': 13.0,
            'low': 11.5,
            'close': 12.5,
        },
    ]
    monkeypatch.setattr(records, 'collection', FakeCollection(docs))

    client = TestClient(app)
    resp = client.get('/api/records/?symbol=000001&limit=2')
    assert resp.status_code == 200
    payload = resp.json()
    assert payload[0]['gs_formula_id'] == 'decision_gs_v1'
    assert payload[0]['mj20'] is not None
    assert payload[1]['mj20'] is not None
    assert payload[0]['gs_signal'] is None


@pytest.mark.unit
def test_apply_qfq_keeps_latest_close_and_scales_history():
    docs = [
        {
            'symbol': '000001.SZ',
            'trade_date': '20260612',
            'open': 12.0,
            'high': 13.0,
            'low': 11.0,
            'close': 12.0,
        },
        {
            'symbol': '000001.SZ',
            'trade_date': '20260611',
            'open': 10.0,
            'high': 11.0,
            'low': 9.0,
            'close': 10.0,
        },
    ]
    adj = FakeCollection(
        [
            {'symbol': '000001.SZ', 'trade_date': '20260611', 'adj_factor': 2.0},
            {'symbol': '000001.SZ', 'trade_date': '20260612', 'adj_factor': 2.4},
        ]
    )
    out = records._apply_records_adjustment(docs, 'qfq', adj)
    assert out[0]['adjust'] == 'qfq'
    assert out[0]['adj_degraded'] is False
    assert out[0]['close'] == pytest.approx(12.0)
    assert out[1]['close'] == pytest.approx(10.0 * 2.0 / 2.4)
    assert out[1]['open'] == pytest.approx(10.0 * 2.0 / 2.4)


@pytest.mark.unit
def test_qfq_uses_global_latest_factor_outside_window():
    docs = [
        {'symbol': '000001.SZ', 'trade_date': '20260611', 'close': 10.0},
    ]
    adj = FakeCollection(
        [
            {'symbol': '000001.SZ', 'trade_date': '20260611', 'adj_factor': 2.0},
            {'symbol': '000001.SZ', 'trade_date': '20260901', 'adj_factor': 3.0},
        ]
    )
    out = records._apply_records_adjustment(docs, 'qfq', adj)
    assert out[0]['close'] == pytest.approx(10.0 * 2.0 / 3.0)


@pytest.mark.unit
def test_apply_hfq_multiplies_raw_by_factor():
    docs = [
        {'symbol': '000001.SZ', 'trade_date': '20260612', 'close': 12.0},
        {'symbol': '000001.SZ', 'trade_date': '20260611', 'close': 10.0},
    ]
    adj = FakeCollection(
        [
            {'symbol': '000001.SZ', 'trade_date': '20260611', 'adj_factor': 2.0},
            {'symbol': '000001.SZ', 'trade_date': '20260612', 'adj_factor': 2.4},
        ]
    )
    out = records._apply_records_adjustment(docs, 'hfq', adj)
    assert out[0]['close'] == pytest.approx(12.0 * 2.4)
    assert out[1]['close'] == pytest.approx(10.0 * 2.0)
    assert out[0]['adjust'] == 'hfq'


@pytest.mark.unit
def test_apply_none_leaves_raw_prices():
    docs = [
        {'symbol': '000001.SZ', 'trade_date': '20260612', 'close': 12.0},
    ]
    adj = FakeCollection(
        [{'symbol': '000001.SZ', 'trade_date': '20260612', 'adj_factor': 2.4}]
    )
    out = records._apply_records_adjustment(docs, 'none', adj)
    assert out[0]['close'] == 12.0
    assert out[0]['adjust'] == 'none'
    assert out[0]['adj_degraded'] is False


def _lookup_sources(pipeline):
    return [stage['$lookup']['from'] for stage in pipeline if '$lookup' in stage]


@pytest.mark.unit
def test_include_scores_false_skips_stock_scores_lookup(monkeypatch, empty_adj):
    class TrackingCollection(FakeCollection):
        def __init__(self, docs=None):
            super().__init__(docs)
            self.pipelines = []

        def aggregate(self, pipeline):
            self.pipelines.append(pipeline)
            return super().aggregate(pipeline)

    coll = TrackingCollection([
        {'_id': '000000000000000000000001', 'symbol': '000001', 'trade_date': '20250920', 'open': 10, 'close': 12},
    ])
    monkeypatch.setattr(records, 'collection', coll)

    client = TestClient(app)
    resp = client.get('/api/records/?symbol=000001&limit=1&include_scores=false')
    assert resp.status_code == 200
    assert _lookup_sources(coll.pipelines[0]) == ['stock_info']


@pytest.mark.unit
def test_include_scores_default_still_looks_up_stock_scores(monkeypatch, empty_adj):
    class TrackingCollection(FakeCollection):
        def __init__(self, docs=None):
            super().__init__(docs)
            self.pipelines = []

        def aggregate(self, pipeline):
            self.pipelines.append(pipeline)
            return super().aggregate(pipeline)

    coll = TrackingCollection([
        {'_id': '000000000000000000000001', 'symbol': '000001', 'trade_date': '20250920', 'open': 10, 'close': 12},
    ])
    monkeypatch.setattr(records, 'collection', coll)

    client = TestClient(app)
    resp = client.get('/api/records/?symbol=000001&limit=1')
    assert resp.status_code == 200
    assert 'stock_scores' in _lookup_sources(coll.pipelines[0])
    assert 'stock_info' in _lookup_sources(coll.pipelines[0])


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
