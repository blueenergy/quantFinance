import pytest

from routers.fund_inventory import list_fund_inventory_periods, list_fund_inventory_snapshots
from routers.stock_workbench import _fund_inventory_section
from services.fund_inventory import public_snapshot

YI = 1e8


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

    def count_documents(self, query=None):
        return len([d for d in self.docs if self._matches(d, query or {})])

    def find_one(self, query=None, projection=None, sort=None):
        rows = self.find(query, projection, sort, limit=1)
        return rows[0] if rows else None

    def find(self, query=None, projection=None, sort=None, limit=0):
        rows = [dict(doc) for doc in self.docs if self._matches(doc, query or {})]
        if sort:
            for field, direction in reversed(sort):
                rows.sort(key=lambda row: row.get(field) if row.get(field) is not None else 0, reverse=direction < 0)
        if limit:
            rows = rows[:limit]
        return rows

    def _matches(self, doc, query):
        for key, expected in query.items():
            value = doc.get(key)
            if isinstance(expected, dict) and "$in" in expected:
                if value not in expected["$in"]:
                    return False
                continue
            if value != expected:
                return False
        return True


def _doc(**overrides):
    base = {
        "symbol": "300502",
        "ts_code": "300502.SZ",
        "name": "新易盛",
        "industry": "电子",
        "universe": "active_equity",
        "period_prev": "20251231",
        "period_curr": "20260630",
        "compare_mode": "full_vs_full",
        "disclosure_prev": "full",
        "disclosure_curr": "full",
        "price_adjusted": True,
        "price_ret": 0.5,
        "state": "incumbent_strengthen",
        "state_label": "存量强化",
        "mv_prev": 100 * YI,
        "mv_curr": 230 * YI,
        "surface_mv_chg": 130 * YI,
        "mv_price_effect": 50 * YI,
        "net_ex_price": 80 * YI,
        "new_inflow": 30 * YI,
        "incumbent_delta": 50 * YI,
        "n_funds_curr": 12,
        "n_new": 3,
        "n_incumbent": 9,
        "n_exited": 1,
    }
    base.update(overrides)
    return base


@pytest.mark.unit
def test_public_snapshot_converts_yi_and_four_questions():
    snap = public_snapshot(_doc())
    assert snap["mv_curr_yi"] == 230.0
    assert snap["net_ex_price_yi"] == 80.0
    assert snap["price_ret_pct"] == 50.0
    assert snap["state_label"] == "存量强化"
    assert len(snap["four_questions"]) == 4
    assert "不是今日买卖" in snap["lag_note"]


@pytest.mark.unit
def test_public_snapshot_does_not_claim_adjustment_when_prices_missing():
    snap = public_snapshot(
        _doc(
            price_adjusted=False,
            state="inventory_exit",
            state_label="库存退出",
            net_ex_price=-10 * YI,
        )
    )
    assert snap["analysis_available"] is False
    assert snap["state"] is None
    assert snap["state_label"] == "数据不足"
    assert snap["net_ex_price_yi"] is None
    assert snap["four_questions"] == []


@pytest.mark.unit
def test_workbench_fund_inventory_section(monkeypatch):
    monkeypatch.setattr(
        "routers.stock_workbench.get_data_db",
        lambda: {"fund_inventory_snapshots": _Collection([_doc()])},
    )
    section = _fund_inventory_section("300502.SZ")
    assert section["data_status"]["found"] is True
    assert section["snapshot"]["state"] == "incumbent_strengthen"
    assert section["snapshot"]["new_inflow_yi"] == 30.0


@pytest.mark.unit
def test_list_snapshots_filters_state(monkeypatch):
    coll = _Collection(
        [
            _doc(symbol="300502", net_ex_price=80 * YI),
            _doc(
                symbol="688256",
                ts_code="688256.SH",
                name="寒武纪",
                state="new_inventory",
                state_label="新建库存",
                net_ex_price=20 * YI,
            ),
        ]
    )
    monkeypatch.setattr("routers.fund_inventory.get_data_db", lambda: {"fund_inventory_snapshots": coll})
    body = list_fund_inventory_snapshots(
        period="20260630",
        state="new_inventory",
        limit=50,
        sort="net_ex_price",
        current_user={"id": "u1"},
    )
    rows = body["data"]["rows"]
    assert len(rows) == 1
    assert rows[0]["name"] == "寒武纪"
    assert body["data"]["counts"]["incumbent_strengthen"] == 1
    assert body["data"]["counts"]["new_inventory"] == 1


@pytest.mark.unit
def test_list_periods(monkeypatch):
    coll = _Collection([_doc(), _doc(period_curr="20251231")])
    monkeypatch.setattr("routers.fund_inventory.get_data_db", lambda: {"fund_inventory_snapshots": coll})
    body = list_fund_inventory_periods(current_user={"id": "u1"})
    assert body["data"]["periods"][0] == "20260630"
