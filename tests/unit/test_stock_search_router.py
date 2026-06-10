from routers import stock


def test_pinyin_initials_handles_changhai_polyphone():
    assert stock._pinyin_initials("长海股份") == "CHGF"


class _Cursor(list):
    def limit(self, n):
        return _Cursor(list(self)[:n])


class _Collection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    def find(self, query=None, projection=None):
        return _Cursor([dict(doc) for doc in self.docs if self._matches(doc, query or {})])

    def _matches(self, doc, query):
        if not query:
            return True
        for key, expected in query.items():
            if key == "$or":
                return any(self._matches(doc, item) for item in expected)
            value = str(doc.get(key) or "")
            if isinstance(expected, dict) and "$regex" in expected:
                import re

                flags = re.I if expected.get("$options") == "i" else 0
                if not re.search(expected["$regex"], value, flags):
                    return False
                continue
            if doc.get(key) != expected:
                return False
        return True


def test_search_stocks_runtime_pinyin_fallback(monkeypatch):
    monkeypatch.setattr(
        stock,
        "get_data_db",
        lambda: {
            "stock_info": _Collection(
                [{"_id": "1", "symbol": "000001.SZ", "name": "平安银行", "industry": "银行"}]
            ),
            "etf_basic": _Collection([]),
        },
    )
    monkeypatch.setattr(stock, "_pinyin_initials", lambda text: "PAYH" if text == "平安银行" else "")

    response = stock.search_stocks("PAYH")

    assert response["success"] is True
    assert response["data"][0]["symbol"] == "000001.SZ"
    assert response["data"][0]["pinyin"] == "PAYH"
