"""Unit tests for the global-market K-line endpoint (forming session bar)."""
import pytest
from fastapi.testclient import TestClient

from auth import get_current_active_user
from main import app
from routers import market


class _Cursor(list):
    def sort(self, *args, **kwargs):
        return self

    def limit(self, n):
        return _Cursor(list(self)[:n])


class _Col:
    def __init__(self, docs=None, fail=False):
        self.docs = docs or []
        self.fail = fail

    def find(self, query=None, projection=None):
        if self.fail:
            raise RuntimeError("collection unavailable")
        docs = self.docs
        if query:
            docs = [d for d in docs if all(d.get(k) == v for k, v in query.items())]
        return _Cursor(docs)

    def find_one(self, query=None, sort=None):
        if self.fail:
            raise RuntimeError("collection unavailable")
        for d in self.docs:
            if not query or all(d.get(k) == v for k, v in query.items()):
                return d
        return None


DAILY = [
    {"symbol": "^IXIC", "date": "20261120", "open": 23592.67, "high": 23837.76,
     "low": 22513.01, "close": 22617.73, "volume": 12469600000},
    {"symbol": "^IXIC", "date": "20261119", "open": 24809.05, "high": 24939.62,
     "low": 23870.70, "close": 23879.20, "volume": 10558810000},
]

QUOTE_WITH_FORMING = {
    "symbol": "^IXIC",
    "price": 24091.85,
    "change_pct": 6.52,
    "prev_close": 22617.73,
    "forming": {
        "date": "20261123",
        "open": 24269.75,
        "high": 24353.65,
        "low": 23794.96,
        "close": 24091.85,
        "volume": 5052853000,
        "prev_close": 22617.73,
    },
}


@pytest.fixture
def authed_client():
    app.dependency_overrides[get_current_active_user] = lambda: {"id": "u1", "is_active": True}
    yield TestClient(app)
    app.dependency_overrides.pop(get_current_active_user, None)


def _patch_db(monkeypatch, daily_docs, quote_docs, quote_fail=False):
    fake = {
        "global_market_daily": _Col(daily_docs),
        "global_market_quotes": _Col(quote_docs, fail=quote_fail),
    }
    monkeypatch.setattr(market, "get_data_db", lambda: fake)


@pytest.mark.unit
def test_kline_appends_open_session_bar(monkeypatch, authed_client):
    _patch_db(monkeypatch, DAILY, [QUOTE_WITH_FORMING])

    resp = authed_client.get("/api/global-market/%5EIXIC/kline?tf=1d")
    assert resp.status_code == 200
    body = resp.json()

    assert body["success"] is True
    assert body["forming_trade_date"] == "20261123"
    # The forming bar is the newest one, and the closed bars are untouched.
    assert body["data"][0]["trade_date"] == "20261123"
    assert body["data"][0]["close"] == 24091.85
    assert body["data"][0]["open"] == 24269.75
    assert body["data"][1]["trade_date"] == "20261120"
    assert body["count"] == 3


@pytest.mark.unit
def test_kline_without_forming_bar(monkeypatch, authed_client):
    _patch_db(monkeypatch, DAILY, [{"symbol": "^IXIC", "price": 24091.85}])

    body = authed_client.get("/api/global-market/%5EIXIC/kline?tf=1d").json()

    assert body["forming_trade_date"] is None
    assert body["count"] == 2
    assert body["data"][0]["trade_date"] == "20261120"


@pytest.mark.unit
def test_kline_skips_forming_bar_already_stored(monkeypatch, authed_client):
    # The daily sync already wrote this session — the forming bar would be a
    # duplicate candle.
    quote = dict(QUOTE_WITH_FORMING)
    quote["forming"] = dict(QUOTE_WITH_FORMING["forming"], date="20261120")
    _patch_db(monkeypatch, DAILY, [quote])

    body = authed_client.get("/api/global-market/%5EIXIC/kline?tf=1d").json()

    assert body["forming_trade_date"] is None
    assert body["count"] == 2


@pytest.mark.unit
def test_kline_survives_broken_quote_collection(monkeypatch, authed_client):
    _patch_db(monkeypatch, DAILY, [], quote_fail=True)

    body = authed_client.get("/api/global-market/%5EIXIC/kline?tf=1d").json()

    assert body["success"] is True
    assert body["forming_trade_date"] is None
    assert body["count"] == 2


@pytest.mark.unit
def test_kline_weekly_includes_forming_session(monkeypatch, authed_client):
    _patch_db(monkeypatch, DAILY, [QUOTE_WITH_FORMING])

    body = authed_client.get("/api/global-market/%5EIXIC/kline?tf=1w").json()

    assert body["success"] is True
    assert body["forming_trade_date"] == "20261123"
    # The week bar folds in the running session.
    assert body["data"][0]["high"] == 24353.65
    assert body["data"][0]["close"] == 24091.85
