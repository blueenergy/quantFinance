import datetime
import pytest
from routers import market


class FakeCursor(list):
    def limit(self, n):
        return list(self)[:n]


class FakeCollection:
    def __init__(self, latest_record=None, historical=None):
        self._latest = latest_record
        self._historical = historical or []

    def find_one(self, query, sort=None):
        return self._latest

    def find(self, query=None, sort=None):
        # return a cursor-like object that supports .limit(n)
        return FakeCursor(self._historical)


@pytest.mark.unit
def test_fetch_market_data_external_success(monkeypatch):
    # external source returns a dict with 'market_data' -> should be returned as-is
    external = {'analysis_date': '2025-09-19', 'market_data': {'IDX': {'symbol': 'IDX'}}}

    monkeypatch.setattr(market.external_market_data, 'get_real_market_data', lambda: external)

    result = market.fetch_market_data()

    assert result is external
    assert result is not None
    assert 'market_data' in result


@pytest.mark.unit
def test_fetch_market_data_local_db_fallback(monkeypatch):
    # external raises -> local DB has data
    def raise_err():
        raise RuntimeError("external down")

    monkeypatch.setattr(market.external_market_data, 'get_real_market_data', raise_err)

    # prepare fake records
    trade_date = datetime.date(2025, 9, 19)
    latest = {'symbol': '1A0001', 'trade_date': trade_date, 'price': 100}
    historical = [
        {'symbol': '1A0001', 'trade_date': trade_date - datetime.timedelta(days=i), 'price': 100 - i}
        for i in range(5)
    ]

    fake_col = FakeCollection(latest_record=latest, historical=historical)

    def fake_get_collections():
        return {'volume_price': fake_col}

    monkeypatch.setattr(market, 'get_db_collections', fake_get_collections)

    # run
    result = market.fetch_market_data()

    assert result is not None
    assert result.get('data_source') == 'local_database'
    assert 'market_data' in result
    # our fake had one symbol
    assert len(result['market_data']) >= 1


@pytest.mark.unit
def test_fetch_market_data_no_data(monkeypatch):
    # external raises and DB returns no records -> None
    monkeypatch.setattr(market.external_market_data, 'get_real_market_data', lambda: (_ for _ in ()).throw(RuntimeError('fail')))

    fake_col = FakeCollection(latest_record=None, historical=[])

    def fake_get_collections():
        return {'volume_price': fake_col}

    monkeypatch.setattr(market, 'get_db_collections', fake_get_collections)

    result = market.fetch_market_data()
    assert result is None
