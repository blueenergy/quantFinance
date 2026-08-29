import re

import pytest

from routers import stock
from utils import pinyin_utils


def test_pinyin_initials_pingan_bank():
    assert pinyin_utils.pinyin_initials("平安银行") == "PAYH"


def test_pinyin_initials_handles_changhai_polyphone():
    assert pinyin_utils.pinyin_initials("长海股份") == "CHGF"


def test_pinyin_full_pingan_bank():
    assert pinyin_utils.pinyin_full("平安银行") == "PINGANYINHANG"


class _Cursor(list):
    def limit(self, n):
        return _Cursor(list(self)[:n])


class _Collection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])
        self.queries = []

    def find(self, query=None, projection=None):
        self.queries.append(query or {})
        if not query:
            raise AssertionError("full collection scan is not allowed")
        return _Cursor([dict(doc) for doc in self.docs if self._matches(doc, query or {})])

    def _matches(self, doc, query):
        if not query:
            return True
        for key, expected in query.items():
            if key == "$or":
                return any(self._matches(doc, item) for item in expected)
            value = str(doc.get(key) or "")
            if isinstance(expected, dict) and "$regex" in expected:
                flags = re.I if expected.get("$options") == "i" else 0
                if not re.search(expected["$regex"], value, flags):
                    return False
                continue
            if doc.get(key) != expected:
                return False
        return True


def test_search_stocks_matches_pinyin_prefix_without_full_scan(monkeypatch):
    collection = _Collection(
        [
            {
                "_id": "1",
                "symbol": "000001.SZ",
                "name": "平安银行",
                "industry": "银行",
                "pinyin": "PAYH",
                "pinyin_full": "PINGANYINHANG",
            }
        ]
    )
    monkeypatch.setattr(
        stock,
        "get_data_db",
        lambda: {"stock_info": collection, "etf_basic": _Collection([])},
    )

    response = stock.search_stocks("PAYH")

    assert response["success"] is True
    assert response["data"][0]["symbol"] == "000001.SZ"
    assert response["data"][0]["pinyin"] == "PAYH"
    assert collection.queries
    assert "$or" in collection.queries[0]


def test_symbol_candidates_pad_hk_code():
    assert "00700.HK" in stock._symbol_candidates("700")
    assert "00700.HK" in stock._symbol_candidates("00700.HK")


def test_ohlcv_collection_name_routes_markets():
    assert stock._ohlcv_collection_name("000001.SZ") == "volume_price"
    assert stock._ohlcv_collection_name("00700.HK") == "hk_volume_price"
    assert stock._ohlcv_collection_name("AAPL") == "us_volume_price"


def test_search_stocks_includes_hk(monkeypatch):
    a_share = _Collection(
        [{"_id": "1", "symbol": "000001.SZ", "name": "平安银行", "pinyin": "PAYH"}]
    )
    hk = _Collection(
        [{"_id": "2", "symbol": "00700.HK", "name": "腾讯控股", "pinyin": "TXKG"}]
    )
    monkeypatch.setattr(
        stock,
        "get_data_db",
        lambda: {
            "stock_info": a_share,
            "etf_basic": _Collection([]),
            "hk_stock_info": hk,
            "us_stock_info": _Collection([]),
        },
    )

    response = stock.search_stocks("700")

    assert response["success"] is True
    symbols = [row["symbol"] for row in response["data"]]
    assert "00700.HK" in symbols
    hk_row = next(row for row in response["data"] if row["symbol"] == "00700.HK")
    assert hk_row["market"] == "HK"


def test_search_stocks_digit_prefix(monkeypatch):
    collection = _Collection(
        [{"_id": "1", "symbol": "600000.SH", "name": "浦发银行", "pinyin": "PFYH"}]
    )
    monkeypatch.setattr(
        stock,
        "get_data_db",
        lambda: {"stock_info": collection, "etf_basic": _Collection([])},
    )

    response = stock.search_stocks("600")

    assert response["success"] is True
    assert response["data"][0]["symbol"] == "600000.SH"
    assert collection.queries[0] == {"symbol": {"$regex": "^600"}}


def test_search_stocks_chinese_name(monkeypatch):
    collection = _Collection(
        [{"_id": "1", "symbol": "000001.SZ", "name": "平安银行", "pinyin": "PAYH"}]
    )
    monkeypatch.setattr(
        stock,
        "get_data_db",
        lambda: {"stock_info": collection, "etf_basic": _Collection([])},
    )

    response = stock.search_stocks("平安")

    assert response["success"] is True
    assert response["data"][0]["name"] == "平安银行"


def test_build_stock_search_query_english_uses_anchored_pinyin():
    query = stock._build_stock_search_query("payh")
    assert query["$or"][1] == {"pinyin": {"$regex": "^PAYH"}}


def test_build_stock_info_pinyin_ops_generates_fields():
    from datetime import datetime

    from tools.backfill_stock_info_pinyin import build_stock_info_pinyin_ops

    docs = [
        {"_id": "1", "name": "平安银行"},
        {"_id": "2", "name": "浦发银行"},
    ]
    ops = build_stock_info_pinyin_ops(docs, now=datetime.now())
    assert len(ops) == 2
    payload = ops[0]._doc["$set"]
    assert payload["pinyin"] == "PAYH"
    assert payload["pinyin_full"] == "PINGANYINHANG"
