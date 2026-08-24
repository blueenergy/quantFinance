from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from main import app
from routers import stock_ranking


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = docs or []

    def find_one(self, query, projection=None, sort=None):
        for d in self.docs:
            if "con_code" in query and d.get("con_code") == query.get("con_code"):
                if query.get("src") and d.get("src") != query.get("src"):
                    continue
                if query.get("is_new") and d.get("is_new") != query.get("is_new"):
                    continue
                return d
            if all(d.get(k) == v for k, v in query.items() if not isinstance(v, dict)):
                return d
        return None

    def find(self, query, projection=None):
        class Cursor:
            def __init__(self, rows):
                self._rows = rows

            def sort(self, key, direction=1):
                return self

            def __iter__(self):
                return iter(self._rows)

        rows = []
        for d in self.docs:
            sym_q = query.get("symbol")
            if isinstance(sym_q, dict) and "$in" in sym_q:
                if d.get("symbol") not in sym_q["$in"]:
                    continue
            elif sym_q and d.get("symbol") != sym_q:
                continue
            ts_q = query.get("ts_code")
            if isinstance(ts_q, dict) and "$in" in ts_q:
                if d.get("ts_code") not in ts_q["$in"]:
                    continue
            elif ts_q and d.get("ts_code") != ts_q:
                continue
            td = query.get("trade_date")
            if isinstance(td, dict):
                if d.get("trade_date", "") < td.get("$gte", ""):
                    continue
                if d.get("trade_date", "") > td.get("$lte", ""):
                    continue
            rows.append(d)
        return Cursor(rows)


def _price_rows(symbol, base, drift, start="20250101", days=200):
    base_dt = datetime.strptime(start, "%Y%m%d")
    rows = []
    for i in range(days):
        trade_date = (base_dt + timedelta(days=i)).strftime("%Y%m%d")
        close = base * (1 + drift * i)
        rows.append({"symbol": symbol, "trade_date": trade_date, "close": close})
    return rows


def test_stock_score_detail_industry_rs_computes_breakdown():
    symbol = "000001.SZ"
    score_date = "20250701"
    stock_rows = _price_rows(symbol, 10.0, 0.002)
    index_rows = [
        {"ts_code": "801780.SI", "trade_date": r["trade_date"], "close": 1000 * (1 + 0.001 * i)}
        for i, r in enumerate(stock_rows)
    ]
    doc = {
        "symbol": symbol,
        "score_date": score_date,
        "industry_rs_score": 72.5,
        "industry_rs_level": "l1",
    }
    data_db = {
        "volume_price": FakeCollection(stock_rows),
        "sw_daily": FakeCollection(index_rows),
        "sw_index_member": FakeCollection([
            {
                "con_code": symbol,
                "src": "SW2021",
                "is_new": "Y",
                "l1_code": "801780.SI",
                "l1_name": "银行",
            }
        ]),
    }
    finance_db = {
        "stock_scores": FakeCollection([doc]),
        # Mirrors get_db_collections(); empty here, so the endpoint falls back
        # to the details still inlined on the score document above.
        "stock_score_details": FakeCollection([]),
    }

    with patch.object(stock_ranking, "get_db_collections", return_value=finance_db), patch.object(
        stock_ranking, "get_data_db", return_value=data_db
    ):
        client = TestClient(app)
        res = client.get(
            "/api/stock-score-detail",
            params={"symbol": symbol, "category": "industry_rs", "score_date": score_date},
        )

    assert res.status_code == 200
    payload = res.json()
    assert payload["success"] is True
    details = payload["data"]["details"]
    assert details["score"] == 72.5
    assert details["stored_score"] == 72.5
    assert details["score_source"] == "stored"
    assert details["computed_score"] != details["score"]
    assert details["score_formula"]["base"] == 50
    assert details["industry"]["name"] == "银行"
    assert len(details["windows"]) == 3
    assert all(w.get("status") == "ok" for w in details["windows"])
