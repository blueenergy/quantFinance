"""ScreeningService integration tests with fake collections."""

from __future__ import annotations

import pandas as pd
import pytest

from services.screening.schemas import FilterSpec, ScreenSpec, SortSpec, UniverseSpec
from services.screening.service import ScreeningService
from services.screening.providers.price import PriceMetricProvider


class _Cursor:
    def __init__(self, rows):
        self._rows = list(rows)

    def sort(self, *args, **kwargs):
        return self

    def __iter__(self):
        return iter(self._rows)


class _Collection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    def find(self, query=None, projection=None, sort=None):
        rows = [dict(d) for d in self.docs if self._matches(d, query or {})]
        if sort:
            for field, direction in reversed(sort):
                rows.sort(key=lambda r: r.get(field) or "", reverse=direction < 0)
        if projection:
            include = {k for k, v in projection.items() if v and k != "_id"}
            if projection.get("_id") == 0:
                rows = [{k: r.get(k) for k in include if k in r} for r in rows]
        return _Cursor(rows)

    def distinct(self, key, query=None):
        return sorted(
            {doc.get(key) for doc in self.docs if self._matches(doc, query or {}) and doc.get(key)}
        )

    def find_one(self, query=None, sort=None, projection=None):
        rows = list(self.find(query, projection, sort))
        return rows[0] if rows else None

    def _matches(self, doc, query):
        for key, expected in query.items():
            if key == "$or":
                if not any(self._matches(doc, clause) for clause in expected):
                    return False
                continue
            val = doc.get(key)
            if isinstance(expected, dict):
                if "$in" in expected and val not in expected["$in"]:
                    return False
                if "$gte" in expected and not (val >= expected["$gte"]):
                    return False
                if "$lte" in expected and not (val <= expected["$lte"]):
                    return False
                continue
            if val != expected:
                return False
        return True


class _FakePriceDao:
    def load_adjusted_ohlc_batch(self, symbols, start, end, adjust="hfq"):
        out = {}
        for sym in symbols:
            if sym == "688001.SH":
                df = pd.DataFrame(
                    {
                        "trade_date": ["20260401", "20260402", "20260403"],
                        "open": [10.0, 12.0, 11.0],
                        "high": [11.0, 15.0, 12.0],
                        "low": [9.0, 11.0, 10.0],
                        "close": [10.0, 14.0, 11.0],
                    }
                )
                df = df.set_index(pd.to_datetime(df["trade_date"], format="%Y%m%d"))
                df.attrs["adj_degraded"] = False
                out[sym] = df
            elif sym == "688002.SH":
                df = pd.DataFrame(
                    {
                        "trade_date": ["20260401", "20260402"],
                        "open": [20.0, 18.0],
                        "high": [21.0, 19.0],
                        "low": [19.0, 15.0],
                        "close": [20.0, 16.0],
                    }
                )
                df = df.set_index(pd.to_datetime(df["trade_date"], format="%Y%m%d"))
                df.attrs["adj_degraded"] = False
                out[sym] = df
        return out


@pytest.fixture()
def screening_db():
    return {
        "sw_index_member": _Collection(
            [
                {
                    "con_code": "688001.SH",
                    "src": "SW2021",
                    "is_new": "Y",
                    "l2_name": "半导体",
                    "l1_name": "电子",
                    "l3_name": "芯片",
                },
                {
                    "con_code": "688002.SH",
                    "src": "SW2021",
                    "is_new": "Y",
                    "l2_name": "半导体",
                    "l1_name": "电子",
                    "l3_name": "芯片",
                },
            ]
        ),
        "stock_info": _Collection(
            [
                {"ts_code": "688001.SH", "name": "测试芯片"},
                {"ts_code": "688002.SH", "name": "ST测试"},
            ]
        ),
        "volume_price": _Collection(
            [{"trade_date": "20260403"}]
        ),
        "financial_income": _Collection([]),
    }


class _FakeDb(dict):
    """Mimics pymongo: an unknown collection name is empty, not an error."""

    def __getitem__(self, key):
        if key not in self:
            self[key] = _Collection([])
        return super().__getitem__(key)


def test_screening_service_price_sort_and_st_filter(screening_db):
    svc = ScreeningService(
        db=_FakeDb(screening_db),
        price_provider=PriceMetricProvider(price_dao=_FakePriceDao()),
    )

    spec = ScreenSpec(
        universe=UniverseSpec(type="manual", value=["688001.SH", "688002.SH"]),
        start="20260401",
        end="20260403",
        exclude_st=True,
        sort=[SortSpec(field="range_amplitude", order="desc")],
        top_n=10,
    )
    result = svc.run(spec)
    assert result.success
    assert result.returned == 1
    assert result.results[0]["ts_code"] == "688001.SH"
    assert result.results[0]["range_amplitude"] is not None


def test_screening_service_drawdown_filter(screening_db):
    svc = ScreeningService(
        db=_FakeDb(screening_db),
        price_provider=PriceMetricProvider(price_dao=_FakePriceDao()),
    )

    spec = ScreenSpec(
        universe=UniverseSpec(type="manual", value=["688001.SH"]),
        start="20260401",
        end="20260403",
        filters=[
            FilterSpec(field="drawdown_from_peak", op="lte", value=-0.2),
        ],
        sort=[SortSpec(field="peak_gain", order="desc")],
    )
    result = svc.run(spec)
    assert result.success
    assert result.returned == 1


def test_unknown_field_returns_error(screening_db):
    svc = ScreeningService(db=_FakeDb(screening_db))
    spec = ScreenSpec(
        universe=UniverseSpec(type="manual", value=["688001.SH"]),
        start="20260401",
        filters=[FilterSpec(field="bogus_metric", op="gt", value=0)],
    )
    result = svc.run(spec)
    assert not result.success
    assert "unsupported" in (result.error or "")


def test_financial_screen_via_all_financial_universe():
    db = _FakeDb(
        {
            "financial_income": _Collection(
                [
                    {
                        "ts_code": "688001.SH",
                        "end_date": "20260331",
                        "report_type": 1,
                        "revenue": 200_000_000,
                        "operate_profit": 50_000_000,
                    },
                    {
                        "ts_code": "688001.SH",
                        "end_date": "20250331",
                        "report_type": 1,
                        "revenue": 100_000_000,
                        "operate_profit": 20_000_000,
                    },
                    {
                        "ts_code": "688002.SH",
                        "end_date": "20260331",
                        "report_type": 1,
                        "revenue": 80_000_000,
                        "operate_profit": 5_000_000,
                    },
                    {
                        "ts_code": "688002.SH",
                        "end_date": "20250331",
                        "report_type": 1,
                        "revenue": 70_000_000,
                        "operate_profit": 4_000_000,
                    },
                ]
            ),
            "stock_info": _Collection(
                [
                    {"ts_code": "688001.SH", "name": "高增长"},
                    {"ts_code": "688002.SH", "name": "低增长"},
                ]
            ),
            "sw_index_member": _Collection([]),
            "volume_price": _Collection([]),
        }
    )
    svc = ScreeningService(db=db)
    spec = ScreenSpec(
        universe=UniverseSpec(type="all_financial", value="all"),
        period="20260331",
        filters=[
            FilterSpec(
                field="revenue",
                metric_type="yoy",
                op="gt",
                value=0.5,
            )
        ],
        sort=[SortSpec(field="revenue_yoy", order="desc")],
    )
    result = svc.run(spec)
    assert result.success
    assert result.returned == 1
    assert result.results[0]["ts_code"] == "688001.SH"
    assert result.results[0]["revenue_yoy"] == pytest.approx(1.0)


def test_unmatched_sw_l2_name_is_reported(screening_db):
    svc = ScreeningService(
        db=_FakeDb(screening_db),
        price_provider=PriceMetricProvider(price_dao=_FakePriceDao()),
    )
    spec = ScreenSpec(
        universe=UniverseSpec(type="sw_l2", value=["半导体", "不存在的行业"]),
        start="20260401",
        end="20260403",
    )
    result = svc.run(spec)
    assert result.success
    assert result.universe_resolved["sw_l2_unmatched"] == ["不存在的行业"]
    assert result.total_universe == 2


def test_matched_counts_after_st_exclusion(screening_db):
    svc = ScreeningService(
        db=_FakeDb(screening_db),
        price_provider=PriceMetricProvider(price_dao=_FakePriceDao()),
    )
    spec = ScreenSpec(
        universe=UniverseSpec(type="manual", value=["688001.SH", "688002.SH"]),
        start="20260401",
        end="20260403",
        exclude_st=True,
    )
    result = svc.run(spec)
    assert result.total_screened == 2
    assert result.matched == 1
    assert result.returned == 1


def test_missing_price_data_yields_no_results(screening_db):
    """A symbol with no OHLC in the window must be dropped, not returned bare."""
    svc = ScreeningService(
        db=_FakeDb(screening_db),
        price_provider=PriceMetricProvider(price_dao=_FakePriceDao()),
    )
    spec = ScreenSpec(
        universe=UniverseSpec(type="manual", value=["000001.SZ"]),
        start="20260401",
        end="20260403",
    )
    result = svc.run(spec)
    assert result.success
    assert result.total_universe == 1
    assert result.matched == 0
    assert result.results == []


def test_fields_projection_keeps_industry_summary(screening_db):
    svc = ScreeningService(
        db=_FakeDb(screening_db),
        price_provider=PriceMetricProvider(price_dao=_FakePriceDao()),
    )
    spec = ScreenSpec(
        universe=UniverseSpec(type="manual", value=["688001.SH"]),
        start="20260401",
        end="20260403",
        fields=["ts_code", "range_amplitude"],
    )
    result = svc.run(spec)
    assert result.success
    assert result.summary["industries_distribution"] == [{"sw_l1": "电子", "count": 1}]
    assert set(result.results[0]) == {"ts_code", "name", "range_amplitude"}


def _financial_db():
    return _FakeDb(
        {
            "financial_income": _Collection(
                [
                    # Huge absolute revenue but modest growth.
                    {
                        "ts_code": "600000.SH",
                        "end_date": "20260331",
                        "report_type": 1,
                        "revenue": 90_000_000_000,
                    },
                    {
                        "ts_code": "600000.SH",
                        "end_date": "20250331",
                        "report_type": 1,
                        "revenue": 60_000_000_000,
                    },
                    # Tiny absolute revenue but explosive growth.
                    {
                        "ts_code": "688003.SH",
                        "end_date": "20260331",
                        "report_type": 1,
                        "revenue": 100_000_000,
                    },
                    {
                        "ts_code": "688003.SH",
                        "end_date": "20250331",
                        "report_type": 1,
                        "revenue": 10_000_000,
                    },
                ]
            ),
            "stock_info": _Collection(
                [
                    {"ts_code": "600000.SH", "name": "大盘股"},
                    {"ts_code": "688003.SH", "name": "小盘股"},
                ]
            ),
            "sw_index_member": _Collection([]),
            "volume_price": _Collection([]),
        }
    )


def test_default_sort_uses_yoy_metric_not_absolute():
    svc = ScreeningService(db=_financial_db())
    spec = ScreenSpec(
        universe=UniverseSpec(type="all_financial", value="all"),
        period="20260331",
        filters=[FilterSpec(field="revenue", metric_type="yoy", op="gt", value=0.3)],
    )
    result = svc.run(spec)
    assert result.success
    assert [r["ts_code"] for r in result.results] == ["688003.SH", "600000.SH"]


def test_all_financial_universe_skips_ts_code_in_filter():
    db = _financial_db()
    queries = []
    original_find = db["financial_income"].find

    def recording_find(query=None, projection=None, sort=None):
        queries.append(query or {})
        return original_find(query, projection, sort)

    db["financial_income"].find = recording_find

    svc = ScreeningService(db=db)
    spec = ScreenSpec(
        universe=UniverseSpec(type="all_financial", value="all"),
        period="20260331",
        filters=[FilterSpec(field="revenue", metric_type="yoy", op="gt", value=0.3)],
    )
    result = svc.run(spec)
    assert result.success
    assert queries, "financial provider never queried"
    assert all("ts_code" not in q for q in queries)
    assert result.total_universe == 2


def _fallback_db():
    """One stock per source tier, all with a formal prior-year base."""
    return _FakeDb(
        {
            "financial_income": _Collection(
                [
                    {
                        "ts_code": "600001.SH",
                        "end_date": "20260630",
                        "report_type": 1,
                        "revenue": 200_000_000,
                        "n_income_attr_p": 20_000_000,
                    },
                    {
                        "ts_code": "600001.SH",
                        "end_date": "20250630",
                        "report_type": 1,
                        "revenue": 100_000_000,
                        "n_income_attr_p": 10_000_000,
                    },
                    {
                        "ts_code": "600002.SH",
                        "end_date": "20250630",
                        "report_type": 1,
                        "revenue": 100_000_000,
                        "n_income_attr_p": 10_000_000,
                    },
                    {
                        "ts_code": "600003.SH",
                        "end_date": "20250630",
                        "report_type": 1,
                        "revenue": 100_000_000,
                        "n_income_attr_p": 10_000_000,
                    },
                ]
            ),
            "earnings_express": _Collection(
                [
                    {
                        "ts_code": "600002.SH",
                        "end_date": "20260630",
                        "ann_date": "20260715",
                        "revenue": 300_000_000,
                        # 快报的 n_income 实为归母口径
                        "n_income": 30_000_000,
                    }
                ]
            ),
            "earnings_forecast": _Collection(
                [
                    {
                        "ts_code": "600003.SH",
                        "end_date": "20260630",
                        "ann_date": "20260714",
                        "type": "预增",
                        # 万元单位：区间 3000~5000 万元 -> 中值 4000 万元 = 4e7 元
                        "net_profit_min": 3000,
                        "net_profit_max": 5000,
                    }
                ]
            ),
            "stock_info": _Collection(
                [
                    {"ts_code": "600001.SH", "name": "正式"},
                    {"ts_code": "600002.SH", "name": "快报"},
                    {"ts_code": "600003.SH", "name": "预告"},
                ]
            ),
            "sw_index_member": _Collection([]),
            "volume_price": _Collection([]),
        }
    )


def test_express_and_forecast_fill_missing_formal_reports():
    svc = ScreeningService(db=_fallback_db())
    spec = ScreenSpec(
        universe=UniverseSpec(type="all_financial", value="all"),
        period="20260630",
        filters=[
            FilterSpec(field="n_income_attr_p", metric_type="yoy", op="gt", value=0.0)
        ],
    )
    result = svc.run(spec)
    assert result.success
    by_code = {r["ts_code"]: r for r in result.results}
    assert set(by_code) == {"600001.SH", "600002.SH", "600003.SH"}
    assert by_code["600001.SH"]["data_source"] == "formal"
    assert by_code["600002.SH"]["data_source"] == "express"
    assert by_code["600003.SH"]["data_source"] == "forecast"
    # 快报 n_income 映射到归母口径，YoY 基数取去年正式财报
    assert by_code["600002.SH"]["n_income_attr_p_yoy"] == pytest.approx(2.0)
    # 预告区间中值 4000 万元，换算成元 = 4e7
    assert by_code["600003.SH"]["n_income_attr_p"] == pytest.approx(40_000_000)
    assert by_code["600003.SH"]["n_income_attr_p_yoy"] == pytest.approx(3.0)
    assert by_code["600003.SH"]["forecast_type"] == "预增"
    assert result.summary["sources_distribution"] == [
        {"data_source": "forecast", "count": 1},
        {"data_source": "express", "count": 1},
        {"data_source": "formal", "count": 1},
    ]


def test_fallbacks_can_be_disabled():
    svc = ScreeningService(db=_fallback_db())
    spec = ScreenSpec(
        universe=UniverseSpec(type="all_financial", value="all"),
        period="20260630",
        include_express=False,
        include_forecast=False,
        filters=[
            FilterSpec(field="n_income_attr_p", metric_type="yoy", op="gt", value=0.0)
        ],
    )
    result = svc.run(spec)
    assert [r["ts_code"] for r in result.results] == ["600001.SH"]


def test_single_quarter_report_type_ignores_fallbacks():
    """快报/预告只有累计口径，report_type=2 时必须不参与。"""
    svc = ScreeningService(db=_fallback_db())
    spec = ScreenSpec(
        universe=UniverseSpec(type="all_financial", value="all"),
        period="20260630",
        report_type=2,
        filters=[FilterSpec(field="revenue", metric_type="abs", op="gt", value=0)],
    )
    result = svc.run(spec)
    assert result.results == []


def test_forecast_cannot_serve_revenue_metrics():
    """预告没有营收，按营收筛选时该股应落选而不是拿净利润顶替。"""
    svc = ScreeningService(db=_fallback_db())
    spec = ScreenSpec(
        universe=UniverseSpec(type="all_financial", value="all"),
        period="20260630",
        filters=[FilterSpec(field="revenue", metric_type="yoy", op="gt", value=0.0)],
    )
    result = svc.run(spec)
    assert [r["ts_code"] for r in result.results] == ["600002.SH", "600001.SH"]


def test_latest_period_considers_express_but_not_forecast():
    db = _fallback_db()
    db["earnings_forecast"].docs.append(
        {
            "ts_code": "600009.SH",
            "end_date": "20261231",
            "ann_date": "20260720",
            "net_profit_min": 100,
            "net_profit_max": 200,
        }
    )
    db["earnings_express"].docs.append(
        {
            "ts_code": "600002.SH",
            "end_date": "20260930",
            "ann_date": "20261015",
            "revenue": 1,
            "n_income": 1,
        }
    )
    from services.screening.providers.financial import FinancialMetricProvider

    provider = FinancialMetricProvider(db)
    spec = ScreenSpec(
        universe=UniverseSpec(type="all_financial", value="all"), period="latest"
    )
    assert provider.resolve_period(spec) == "20260930"

    spec_no_express = spec.model_copy(update={"include_express": False})
    assert provider.resolve_period(spec_no_express) == "20260630"


def _yoy_quality_db():
    """去年同期分别为：正常、转正前亏损、持续亏损、微利。"""
    prior = {
        "600010.SH": 100_000_000,
        "600011.SH": -50_000_000,
        "600012.SH": -50_000_000,
        "600013.SH": 10_000,
    }
    current = {
        "600010.SH": 150_000_000,
        "600011.SH": 30_000_000,
        "600012.SH": -10_000_000,
        "600013.SH": 50_000_000,
    }
    docs = []
    for ts, v in current.items():
        docs.append(
            {"ts_code": ts, "end_date": "20260331", "report_type": 1, "n_income_attr_p": v}
        )
    for ts, v in prior.items():
        docs.append(
            {"ts_code": ts, "end_date": "20250331", "report_type": 1, "n_income_attr_p": v}
        )
    return _FakeDb(
        {
            "financial_income": _Collection(docs),
            "stock_info": _Collection(
                [{"ts_code": ts, "name": ts[:4]} for ts in prior]
            ),
            "sw_index_member": _Collection([]),
            "volume_price": _Collection([]),
        }
    )


def _run_yoy_quality(**kw):
    svc = ScreeningService(db=_yoy_quality_db())
    return svc.run(
        ScreenSpec(
            universe=UniverseSpec(type="all_financial", value="all"),
            period="20260331",
            filters=[
                FilterSpec(
                    field="n_income_attr_p", metric_type="yoy", op="gt", value=-10.0
                )
            ],
            **kw,
        )
    )


def test_yoy_quality_labels_instead_of_dropping_turnarounds():
    result = _run_yoy_quality()
    by_code = {r["ts_code"]: r for r in result.results}
    assert set(by_code) == {"600010.SH", "600011.SH", "600012.SH", "600013.SH"}
    q = "n_income_attr_p_yoy_quality"
    assert by_code["600010.SH"][q] == "normal"
    assert by_code["600011.SH"][q] == "turnaround"
    assert by_code["600012.SH"][q] == "loss"
    assert by_code["600013.SH"][q] == "low_base"
    # 基数一并给出，读数才可解释
    assert by_code["600011.SH"]["n_income_attr_p_yoy_base"] == -50_000_000
    assert result.summary["yoy_quality_distribution"] == [
        {"yoy_quality": "turnaround", "count": 1},
        {"yoy_quality": "loss", "count": 1},
        {"yoy_quality": "low_base", "count": 1},
        {"yoy_quality": "normal", "count": 1},
    ]


def test_exclude_negative_base_still_available_as_opt_in():
    result = _run_yoy_quality(exclude_negative_base=True)
    assert {r["ts_code"] for r in result.results} == {"600010.SH", "600013.SH"}


def test_low_base_threshold_is_configurable():
    result = _run_yoy_quality(low_base_yoy_threshold=6000.0)
    by_code = {r["ts_code"]: r for r in result.results}
    # 600013 同比约 4999 倍，抬高阈值后不再算低基数
    assert by_code["600013.SH"]["n_income_attr_p_yoy_quality"] == "normal"


def test_projection_keeps_yoy_annotations():
    svc = ScreeningService(db=_yoy_quality_db())
    result = svc.run(
        ScreenSpec(
            universe=UniverseSpec(type="all_financial", value="all"),
            period="20260331",
            filters=[
                FilterSpec(
                    field="n_income_attr_p", metric_type="yoy", op="gt", value=-10.0
                )
            ],
            fields=["ts_code", "n_income_attr_p_yoy"],
        )
    )
    assert set(result.results[0]) == {
        "ts_code",
        "name",
        "n_income_attr_p_yoy",
        "n_income_attr_p_yoy_base",
        "n_income_attr_p_yoy_quality",
        "data_source",
        "data_source_ann_date",
    }


def test_base_period_equal_to_target_period():
    svc = ScreeningService(db=_financial_db())
    spec = ScreenSpec(
        universe=UniverseSpec(type="all_financial", value="all"),
        period="20260331",
        filters=[
            FilterSpec(
                field="revenue",
                metric_type="abs",
                op="gt",
                value=1_000_000_000,
                base_period="20260331",
            )
        ],
    )
    result = svc.run(spec)
    assert result.success
    assert [r["ts_code"] for r in result.results] == ["600000.SH"]
