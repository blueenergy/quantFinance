from datetime import date

import pytest

from routers.fund_inventory import list_fund_inventory_periods, list_fund_inventory_snapshots
from routers.stock_workbench import _fund_inventory_section
from services.fund_inventory import classify_inventory_candidate, public_snapshot
from services.fund_inventory_context import build_candidate_contexts

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


class _SortableRows(list):
    def sort(self, spec):
        rows = list(self)
        for field, direction in reversed(spec):
            rows.sort(
                key=lambda row: row.get(field) or "",
                reverse=direction < 0,
            )
        return _SortableRows(rows)


class _ContextCollection:
    def __init__(self, *, aggregate_rows=None, find_rows=None):
        self.aggregate_rows = list(aggregate_rows or [])
        self.find_rows = list(find_rows or [])

    def aggregate(self, pipeline):
        return list(self.aggregate_rows)

    def find(self, query=None, projection=None):
        return _SortableRows(dict(row) for row in self.find_rows)


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
def test_public_snapshot_labels_provisional_top10_scope():
    snap = public_snapshot(
        _doc(
            quality_status="provisional",
            comparison_scope="top10",
            market="HK",
            ts_code="00700.HK",
            price_source="fund_report_implied",
            corporate_action_adjusted=False,
            price_note="由基金报告推导，未校正公司行动。",
            quality={
                "status": "provisional",
                "strategy_coverage_pct": 100.0,
                "holding_depth_pct": 20.0,
                "full_readiness_pct": 20.0,
            },
            state="inventory_exit",
        )
    )
    assert snap["state_label"] == "退出前十大"
    assert snap["quality_status"] == "provisional"
    assert snap["quality"]["holding_depth_pct"] == 20.0
    assert "不代表完整全持仓" in snap["lag_note"]
    assert "暂未校正拆股" in snap["lag_note"]
    assert snap["market"] == "HK"
    assert snap["corporate_action_adjusted"] is False
    assert "前十大" in snap["four_questions"][0]["question"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("state", "context", "expected"),
    [
        (
            "incumbent_strengthen",
            {
                "score": {
                    "growth_score": 70,
                    "fundamental_score": 60,
                    "value_score": 55,
                },
                "financials": [
                    {"q_netprofit_yoy": 18},
                    {"q_netprofit_yoy": 12},
                ],
                "ytd_return_pct": 20,
            },
            "underpriced",
        ),
        (
            "incumbent_strengthen",
            {
                "score": {
                    "growth_score": 70,
                    "fundamental_score": 60,
                    "value_score": 25,
                },
                "financials": [
                    {"q_netprofit_yoy": 18},
                    {"q_netprofit_yoy": 12},
                ],
                "ytd_return_pct": 80,
            },
            "crowded_trend",
        ),
        (
            "new_inventory",
            {
                "score": {
                    "growth_score": 65,
                    "fundamental_score": 55,
                    "value_score": 50,
                },
                "financials": [
                    {"q_netprofit_yoy": 15},
                    {"q_netprofit_yoy": -8},
                ],
                "ytd_return_pct": 12,
            },
            "earnings_inflection",
        ),
        (
            "inventory_turnover",
            {"ytd_return_pct": 65},
            "high_level_turnover",
        ),
        (
            "inventory_exit",
            {
                "score": {
                    "growth_score": 35,
                    "fundamental_score": 40,
                    "value_score": 45,
                },
                "financials": [
                    {"q_netprofit_yoy": -20},
                    {"q_netprofit_yoy": -5},
                ],
                "ytd_return_pct": -15,
            },
            "negative_combo",
        ),
    ],
)
def test_classify_inventory_candidate_combinations(state, context, expected):
    result = classify_inventory_candidate(_doc(state=state), context)
    assert result["category"] == expected
    assert result["evidence"]
    assert result["rule_version"] == "fund_inventory_candidate_v1"


@pytest.mark.unit
def test_provisional_exit_is_not_classified_as_negative():
    result = classify_inventory_candidate(
        _doc(state="inventory_exit", quality_status="provisional"),
        {
            "score": {"growth_score": 30},
            "financials": [
                {"q_netprofit_yoy": -20},
                {"q_netprofit_yoy": -5},
            ],
            "ytd_return_pct": -15,
        },
    )
    assert result["category"] == "other"
    assert "不按完整持仓退出判负面" in result["caveat"]


@pytest.mark.unit
def test_unadjusted_ytd_return_does_not_drive_price_categories():
    result = classify_inventory_candidate(
        _doc(state="incumbent_strengthen"),
        {
            "score": {
                "growth_score": 70,
                "fundamental_score": 60,
                "value_score": 20,
            },
            "financials": [
                {"q_netprofit_yoy": 18},
                {"q_netprofit_yoy": 12},
            ],
            "ytd_return_pct": 80,
            "ytd_adjustment_degraded": True,
        },
    )
    assert result["category"] == "other"
    assert "复权数据不足" in result["evidence"][-2]


@pytest.mark.unit
def test_hk_without_comparable_scores_reports_insufficient_data():
    result = classify_inventory_candidate(
        _doc(ts_code="00700.HK", market="HK"),
    )
    assert result["category"] == "insufficient_data"
    assert "港股暂缺" in result["caveat"]


@pytest.mark.unit
def test_build_candidate_contexts_uses_adjusted_ytd_return():
    symbol = "300502.SZ"
    contexts = build_candidate_contexts(
        [_doc(ts_code=symbol, market="A")],
        finance_collections={
            "stock_scores": _ContextCollection(
                aggregate_rows=[
                    {
                        "symbol": symbol,
                        "score_date": "20260828",
                        "growth_score": 70,
                        "fundamental_score": 60,
                        "value_score": 50,
                    }
                ]
            )
        },
        data_collections={
            "financial_indicator": _ContextCollection(
                find_rows=[
                    {
                        "ts_code": symbol,
                        "end_date": "20260331",
                        "q_netprofit_yoy": 18,
                    },
                    {
                        "ts_code": symbol,
                        "end_date": "20251231",
                        "q_netprofit_yoy": 12,
                    },
                ]
            ),
            "volume_price": _ContextCollection(
                aggregate_rows=[
                    {
                        "_id": symbol,
                        "first_date": "20260105",
                        "first_close": 100,
                        "last_date": "20260828",
                        "last_close": 60,
                    }
                ]
            ),
            "stock_adj_factor": _ContextCollection(
                find_rows=[
                    {
                        "symbol": symbol,
                        "trade_date": "20260105",
                        "adj_factor": 1,
                    },
                    {
                        "symbol": symbol,
                        "trade_date": "20260828",
                        "adj_factor": 2,
                    },
                ]
            ),
        },
        as_of=date(2026, 8, 29),
    )
    assert contexts[symbol]["ytd_return_pct"] == 20.0
    assert contexts[symbol]["ytd_adjustment_degraded"] is False
    assert len(contexts[symbol]["financials"]) == 2


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
    monkeypatch.setattr(
        "routers.fund_inventory.build_candidate_contexts",
        lambda *args, **kwargs: {},
    )
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
def test_list_snapshots_filters_market(monkeypatch):
    coll = _Collection(
        [
            _doc(symbol="300502", market="A"),
            _doc(symbol="00700.HK", ts_code="00700.HK", name="腾讯控股", market="HK"),
        ]
    )
    monkeypatch.setattr(
        "routers.fund_inventory.get_data_db",
        lambda: {"fund_inventory_snapshots": coll},
    )
    monkeypatch.setattr(
        "routers.fund_inventory.build_candidate_contexts",
        lambda *args, **kwargs: {},
    )
    body = list_fund_inventory_snapshots(
        period="20260630",
        state=None,
        market="HK",
        limit=50,
        sort="net_ex_price",
        current_user={"id": "u1"},
    )
    assert [row["ts_code"] for row in body["data"]["rows"]] == ["00700.HK"]
    assert body["data"]["market"] == "HK"


@pytest.mark.unit
def test_list_periods(monkeypatch):
    coll = _Collection([_doc(), _doc(period_curr="20251231")])
    monkeypatch.setattr("routers.fund_inventory.get_data_db", lambda: {"fund_inventory_snapshots": coll})
    body = list_fund_inventory_periods(current_user={"id": "u1"})
    assert body["data"]["periods"][0] == "20260630"
