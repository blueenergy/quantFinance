import pytest

from routers.fund_copywork import (
    get_copywork_product,
    get_copywork_stock,
    list_copywork_consensus,
    list_copywork_periods,
    list_copywork_watchlist,
)


class _Collection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    def distinct(self, field, query=None):
        return list(
            {
                doc.get(field)
                for doc in self.docs
                if doc.get(field) is not None and self._matches(doc, query or {})
            }
        )

    def find_one(self, query=None, projection=None, sort=None):
        rows = self.find(query, projection, sort, limit=1)
        return rows[0] if rows else None

    def find(self, query=None, projection=None, sort=None, limit=0):
        rows = [dict(doc) for doc in self.docs if self._matches(doc, query or {})]
        if sort:
            for field, direction in reversed(sort):
                rows.sort(
                    key=lambda row: row.get(field) if row.get(field) is not None else 0,
                    reverse=direction < 0,
                )
        if limit:
            rows = rows[:limit]
        if projection:
            keep = {k for k, v in projection.items() if v and k != "_id"}
            drop = {k for k, v in projection.items() if not v and k != "_id"}
            cleaned = []
            for row in rows:
                item = dict(row)
                item.pop("_id", None)
                if drop:
                    for key in drop:
                        item.pop(key, None)
                elif keep:
                    item = {k: item.get(k) for k in keep if k in item}
                cleaned.append(item)
            rows = cleaned
        return rows

    def _matches(self, doc, query):
        for key, expected in query.items():
            actual = doc.get(key)
            if isinstance(expected, dict) and "$in" in expected:
                if actual not in expected["$in"]:
                    return False
            elif actual != expected:
                return False
        return True


@pytest.fixture
def copywork_db(monkeypatch):
    meta = _Collection(
        [
            {
                "universe": "active_equity",
                "period_prev": "20250630",
                "period_curr": "20251231",
                "compare_mode": "full_vs_full",
                "lag_note": "不是今日买卖",
            }
        ]
    )
    products = _Collection(
        [
            {
                "universe": "active_equity",
                "fund_key": "易方达|明星混合",
                "name": "明星混合A",
                "period_prev": "20250630",
                "period_curr": "20251231",
                "rank": 1,
                "n_new": 2,
                "n_increased": 3,
                "n_decreased": 1,
                "n_exited": 1,
                "aum_curr": 1.2e10,
                "holdings": [{"symbol": "300502", "action": "increased", "change_pct": 20, "mkv_curr": 1e9}],
            }
        ]
    )
    stocks = _Collection(
        [
            {
                "universe": "active_equity",
                "symbol": "300502",
                "name": "新易盛",
                "period_prev": "20250630",
                "period_curr": "20251231",
                "n_operated": 5,
                "n_new": 1,
                "n_increased": 2,
                "n_decreased": 1,
                "n_exited": 1,
                "cards": [{"fund_key": "易方达|明星混合", "action_label": "加仓", "change_pct": 20}],
            }
        ]
    )
    data = {
        "fund_copywork_meta": meta,
        "fund_copywork_products": products,
        "fund_copywork_stocks": stocks,
    }
    monkeypatch.setattr("routers.fund_copywork.get_data_db", lambda: data)
    return data


@pytest.mark.unit
def test_copywork_watchlist_and_product(copywork_db):
    periods = list_copywork_periods(current_user={"username": "t"})
    assert periods["data"]["periods"] == ["20251231"]
    assert periods["data"]["items"][0]["quality_status"] == "official"
    watch = list_copywork_watchlist(period="20251231", current_user={"username": "t"})
    assert watch["data"]["rows"][0]["n_new"] == 2
    assert watch["data"]["meta"]["quality_status"] == "official"
    assert "holdings" not in watch["data"]["rows"][0]
    detail = get_copywork_product("易方达|明星混合", period="20251231", current_user={"username": "t"})
    assert detail["data"]["product"]["holdings"][0]["symbol"] == "300502"
    assert detail["data"]["product"]["holdings"][0]["name"] == "新易盛"


@pytest.mark.unit
def test_copywork_consensus_and_lookup(copywork_db):
    cons = list_copywork_consensus(period="20251231", limit=50, current_user={"username": "t"})
    assert cons["data"]["rows"][0]["n_operated"] == 5
    found = get_copywork_stock("300502.SZ", period="20251231", current_user={"username": "t"})
    assert found["data"]["stock"]["name"] == "新易盛"
    missing = get_copywork_stock("999999", period="20251231", current_user={"username": "t"})
    assert missing["data"]["stock"]["n_operated"] == 0
