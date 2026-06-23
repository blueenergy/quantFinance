import pytest

from routers.hermes_quant_tools import (
    _foreign_top10_floatholders_payload,
    _stock_earnings_consensus_payload,
    _stock_industry_comparison_payload,
    _stock_valuation_metrics_payload,
)


class _Collection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])
        self.find_calls = 0

    def find(self, query=None, projection=None, sort=None, limit=0):
        self.find_calls += 1
        rows = [dict(doc) for doc in self.docs if self._matches(doc, query or {})]
        if sort:
            for field, direction in reversed(sort):
                rows.sort(key=lambda row: row.get(field) or "", reverse=direction < 0)
        if limit:
            rows = rows[:limit]
        if projection:
            include_keys = {key for key, value in projection.items() if value}
            exclude_id = projection.get("_id") == 0
            if include_keys:
                rows = [{key: row.get(key) for key in include_keys if key in row} for row in rows]
            if exclude_id:
                rows = [{key: value for key, value in row.items() if key != "_id"} for row in rows]
        return rows

    def distinct(self, key, query=None):
        return sorted({doc.get(key) for doc in self.docs if self._matches(doc, query or {}) and doc.get(key)})

    def _matches(self, doc, query):
        for key, expected in query.items():
            if key == "$or":
                if not any(self._matches(doc, clause) for clause in expected):
                    return False
                continue
            value = doc.get(key)
            if isinstance(expected, dict):
                if "$in" in expected and value not in expected["$in"]:
                    return False
                if "$lt" in expected and not (value < expected["$lt"]):
                    return False
                if "$gte" in expected and not (value >= expected["$gte"]):
                    return False
                if "$lte" in expected and not (value <= expected["$lte"]):
                    return False
                continue
            if value != expected:
                return False
        return True


def test_stock_valuation_metrics_builds_tech_fields_from_existing_collections():
    data = {
        "financial_daily_basic": _Collection(
            [
                {
                    "ts_code": "688001.SH",
                    "trade_date": "20260619",
                    "close": 44.33,
                    "pe_ttm": 35.0,
                    "pb": 4.2,
                    "ps_ttm": 8.5,
                    "total_mv": 1_000_000.0,
                    "circ_mv": 800_000.0,
                }
            ]
        ),
        "financial_indicator": _Collection(
            [
                {
                    "ts_code": "688001.SH",
                    "end_date": "20260331",
                    "rd_exp": 120_000_000.0,
                    "grossprofit_margin": 62.5,
                    "netprofit_margin": 18.0,
                    "fcff": 80_000_000.0,
                    "fcfe": 70_000_000.0,
                    "ocfps": 0.8,
                    "roe": 12.0,
                    "roic": 10.0,
                    "tr_yoy": 28.0,
                    "netprofit_yoy": 30.0,
                    "debt_to_assets": 32.0,
                    "q_netprofit_margin": 17.5,
                    "dtprofit_to_profit": 98.5,
                    "opincome_of_ebt": 95.0,
                    "investincome_of_ebt": 2.0,
                    "salescash_to_or": 1.05,
                    "ocf_to_or": 0.35,
                    "ocf_yoy": 12.0,
                    "fixed_assets": 500_000_000.0,
                    "total_fa_trun": 2.5,
                    "capitalized_to_da": 0.8,
                    "roic_yearly": 11.0,
                }
            ]
        ),
        "financial_income": _Collection(
            [
                {
                    "ts_code": "688001.SH",
                    "end_date": "20260331",
                    "revenue": 1_000_000_000.0,
                    "n_income_attr_p": 180_000_000.0,
                }
            ]
        ),
        "stock_info": _Collection(
            [
                {
                    "ts_code": "688001.SH",
                    "symbol": "688001.SH",
                    "name": "科技样本",
                    "industry": "软件开发",
                    "sw_l1": "计算机",
                    "sw_l2": "软件开发",
                }
            ]
        ),
        "financial_mainbz": _Collection(
            [
                {"ts_code": "688001.SH", "end_date": "20260331", "bz_type": "P", "bz_item": "软件", "bz_sales": 1_000.0},
            ]
        ),
    }

    payload = _stock_valuation_metrics_payload("688001.SH", data_collections=data)

    assert payload["success"] is True
    assert payload["resolved_symbol"] == "688001.SH"
    assert payload["name"] == "科技样本"
    assert payload["segments"]["by_type"]["P"]["items"][0]["share_pct"] == pytest.approx(100.0)
    assert payload["market"]["ps_ttm"] == pytest.approx(8.5)
    assert payload["financial"]["revenue_growth_pct"] == pytest.approx(28.0)
    assert payload["derived"]["rd_intensity_pct"] == pytest.approx(12.0)
    assert payload["derived"]["fcff_margin_pct"] == pytest.approx(8.0)
    assert payload["derived"]["rule_of_40_net_margin"] == pytest.approx(46.0)
    assert payload["derived"]["rule_of_40_fcff_margin"] == pytest.approx(36.0)
    assert payload["quality"]["profit_quality"]["q_netprofit_margin"] == pytest.approx(17.5)
    assert payload["quality"]["cash_flow_quality"]["ocf_to_or"] == pytest.approx(0.35)
    assert payload["quality"]["asset_efficiency"]["roic_yearly"] == pytest.approx(11.0)
    assert payload["quality_notes"]
    assert payload["missing"] == []
    assert payload["missing_notes"] == {}
    assert payload["series"][0]["period"] == "20260331"
    assert payload["series"][0]["revenue_growth_pct"] == pytest.approx(28.0)
    assert payload["series"][0]["fcff_margin_pct"] == pytest.approx(8.0)
    assert payload["series"][0]["rd_intensity_pct"] == pytest.approx(12.0)
    assert payload["derived"]["fcff_positive_periods"] == 1
    assert payload["derived"]["fcff_sample_periods"] == 1
    # 只有 1 个交易日样本，分位不足 30 应为 None
    assert payload["valuation_percentiles"]["ps_ttm"] is None
    assert payload["samples"]["daily_count"] == 1


def test_stock_valuation_metrics_builds_segments_from_mainbz():
    data = {
        "financial_daily_basic": _Collection(
            [{"ts_code": "688001.SH", "trade_date": "20260619", "ps_ttm": 8.0}]
        ),
        "financial_indicator": _Collection(
            [{"ts_code": "688001.SH", "end_date": "20260331", "tr_yoy": 20.0}]
        ),
        "financial_income": _Collection(
            [{"ts_code": "688001.SH", "end_date": "20260331", "revenue": 1_000.0}]
        ),
        "stock_info": _Collection([{"ts_code": "688001.SH", "name": "分部样本"}]),
        "financial_mainbz": _Collection(
            [
                # 旧一期，应被忽略
                {"ts_code": "688001.SH", "end_date": "20251231", "bz_type": "P", "bz_item": "旧品", "bz_sales": 50.0},
                # 最新一期 按产品
                {"ts_code": "688001.SH", "end_date": "20260331", "bz_type": "P", "bz_item": "芯片", "bz_sales": 600.0, "bz_profit": 120.0},
                {"ts_code": "688001.SH", "end_date": "20260331", "bz_type": "P", "bz_item": "模组", "bz_sales": 400.0, "bz_profit": 40.0},
                # 最新一期 按地区
                {"ts_code": "688001.SH", "end_date": "20260331", "bz_type": "D", "bz_item": "境内", "bz_sales": 700.0},
                {"ts_code": "688001.SH", "end_date": "20260331", "bz_type": "D", "bz_item": "境外", "bz_sales": 300.0},
            ]
        ),
    }

    payload = _stock_valuation_metrics_payload("688001.SH", data_collections=data)

    segments = payload["segments"]
    assert segments["period"] == "20260331"
    assert set(segments["by_type"].keys()) == {"P", "D"}
    product = segments["by_type"]["P"]
    assert product["item_count"] == 2
    assert product["total_sales_yuan"] == pytest.approx(1000.0)
    # 排序后第一项为占比最大的芯片
    assert product["items"][0]["bz_item"] == "芯片"
    assert product["items"][0]["share_pct"] == pytest.approx(60.0)
    assert product["items"][1]["share_pct"] == pytest.approx(40.0)
    assert "financial_mainbz" not in payload["missing"]


def test_stock_valuation_metrics_marks_missing_mainbz():
    data = {
        "financial_daily_basic": _Collection(
            [{"ts_code": "688009.SH", "trade_date": "20260619", "ps_ttm": 8.0}]
        ),
        "financial_indicator": _Collection(
            [{"ts_code": "688009.SH", "end_date": "20260331", "tr_yoy": 20.0}]
        ),
        "financial_income": _Collection(
            [{"ts_code": "688009.SH", "end_date": "20260331", "revenue": 1_000.0}]
        ),
        "stock_info": _Collection([{"ts_code": "688009.SH", "name": "无分部样本"}]),
        "financial_mainbz": _Collection([]),
    }

    payload = _stock_valuation_metrics_payload("688009.SH", data_collections=data)

    assert payload["segments"]["by_type"] == {}
    assert "financial_mainbz" in payload["missing"]
    assert "financial_mainbz" in payload["missing_notes"]


def test_stock_valuation_metrics_computes_history_percentiles():
    daily = []
    for idx in range(40):
        day = 40 - idx
        daily.append(
            {
                "ts_code": "688004.SH",
                "trade_date": f"202605{day:02d}",
                "close": 30.0,
                "pe_ttm": 20.0 + idx,
                "pb": 3.0 + idx * 0.05,
                "ps_ttm": 5.0 + idx * 0.1,
                "total_mv": 500_000.0,
            }
        )
    data = {
        "financial_daily_basic": _Collection(daily),
        "financial_indicator": _Collection(
            [
                {"ts_code": "688004.SH", "end_date": "20260331", "fcff": 50_000_000.0, "tr_yoy": 25.0, "netprofit_margin": 12.0},
                {"ts_code": "688004.SH", "end_date": "20251231", "fcff": -10_000_000.0, "tr_yoy": 20.0, "netprofit_margin": 8.0},
            ]
        ),
        "financial_income": _Collection(
            [
                {"ts_code": "688004.SH", "end_date": "20260331", "revenue": 1_000_000_000.0},
                {"ts_code": "688004.SH", "end_date": "20251231", "revenue": 900_000_000.0},
            ]
        ),
        "stock_info": _Collection([{"ts_code": "688004.SH", "name": "历史分位样本"}]),
    }

    payload = _stock_valuation_metrics_payload("688004.SH", data_collections=data)

    # 最新交易日(20260540)对应 idx=0，是样本里最低的 pe_ttm/ps_ttm，分位应接近最低
    assert payload["valuation_percentiles"]["pe_ttm"] == pytest.approx(2.5)
    assert payload["valuation_percentiles"]["ps_ttm"] == pytest.approx(2.5)
    assert len(payload["series"]) == 2
    assert payload["derived"]["fcff_positive_periods"] == 1
    assert payload["derived"]["fcff_sample_periods"] == 2


def test_stock_valuation_metrics_marks_missing_quality_fields():
    data = {
        "financial_daily_basic": _Collection(
            [{"ts_code": "688010.SH", "trade_date": "20260619", "ps_ttm": 7.0}]
        ),
        "financial_indicator": _Collection(
            [{"ts_code": "688010.SH", "end_date": "20260331", "netprofit_margin": 15.0, "tr_yoy": 10.0}]
        ),
        "financial_income": _Collection(
            [{"ts_code": "688010.SH", "end_date": "20260331", "revenue": 1_000.0}]
        ),
        "stock_info": _Collection([{"ts_code": "688010.SH", "name": "质量缺字段"}]),
        "financial_mainbz": _Collection(
            [{"ts_code": "688010.SH", "end_date": "20260331", "bz_type": "P", "bz_item": "软件", "bz_sales": 1_000.0}]
        ),
    }

    payload = _stock_valuation_metrics_payload("688010.SH", data_collections=data)

    assert payload["success"] is True
    assert payload["quality"]["profit_quality"]["q_netprofit_margin"] is None
    assert "financial_indicator.q_netprofit_margin" in payload["missing"]
    assert "financial_indicator.ocf_to_or" in payload["missing"]
    assert "financial_indicator.fixed_assets" in payload["missing"]
    assert payload["missing_notes"]["financial_indicator.q_netprofit_margin"]


def test_stock_valuation_metrics_degrades_with_missing_fields():
    data = {
        "financial_daily_basic": _Collection(
            [{"symbol": "688002.SH", "trade_date": "20260619", "pb": 3.0}]
        ),
        "financial_indicator": _Collection(
            [{"symbol": "688002.SH", "end_date": "20260331", "tr_yoy": 12.0}]
        ),
        "financial_income": _Collection([]),
        "stock_info": _Collection([{"symbol": "688002.SH", "name": "缺字段样本"}]),
    }

    payload = _stock_valuation_metrics_payload("688002.SH", data_collections=data)

    assert payload["success"] is True
    assert payload["market"]["pb"] == pytest.approx(3.0)
    assert payload["market"]["ps_ttm"] is None
    assert payload["derived"]["rd_intensity_pct"] is None
    assert "financial_daily_basic.ps_ttm" in payload["missing"]
    assert "financial_indicator.rd_exp" in payload["missing"]
    assert "financial_income" in payload["missing"]


def test_stock_valuation_metrics_matches_symbol_variants():
    data = {
        "financial_daily_basic": _Collection(
            [{"ts_code": "688003.SH", "trade_date": "20260619", "ps_ttm": 6.0}]
        ),
        "financial_indicator": _Collection([]),
        "financial_income": _Collection([]),
        "stock_info": _Collection([{"ts_code": "688003.SH", "name": "变体样本"}]),
    }

    payload = _stock_valuation_metrics_payload("688003", data_collections=data)

    assert payload["success"] is True
    assert payload["resolved_symbol"] == "688003.SH"
    assert payload["market"]["ps_ttm"] == pytest.approx(6.0)


def test_stock_valuation_metrics_computes_normalized_pe_and_profit_alert():
    data = {
        "financial_daily_basic": _Collection(
            [
                {
                    "ts_code": "688111.SH",
                    "trade_date": "20260619",
                    "pe_ttm": 29.0,
                    "total_mv": 3_500.0,
                }
            ]
        ),
        "financial_indicator": _Collection(
            [
                {
                    "ts_code": "688111.SH",
                    "end_date": "20260331",
                    "netprofit_margin": 30.0,
                    "q_netprofit_margin": 136.0,
                    "investincome_of_ebt": 80.5,
                    "dtprofit_to_profit": 98.9,
                    "tr_yoy": 24.0,
                    "fcff": -18_400_000.0,
                },
                {
                    "ts_code": "688111.SH",
                    "end_date": "20251231",
                    "fcff": 500_000_000.0,
                },
            ]
        ),
        "financial_income": _Collection(
            [
                {"ts_code": "688111.SH", "end_date": "20260331", "revenue": 1_000.0, "n_income_attr_p": 100.0},
                {"ts_code": "688111.SH", "end_date": "20251231", "n_income_attr_p": 300.0},
                {"ts_code": "688111.SH", "end_date": "20250331", "n_income_attr_p": 50.0},
            ]
        ),
        "stock_info": _Collection([{"ts_code": "688111.SH", "name": "金山样本"}]),
        "financial_mainbz": _Collection([]),
    }

    payload = _stock_valuation_metrics_payload("688111.SH", data_collections=data)

    assert payload["derived"]["ttm_net_income_yuan"] == pytest.approx(350.0)
    assert payload["derived"]["normalized_pe_ttm"] == pytest.approx(100_000.0)
    assert payload["derived"]["normalized_pe_method"] == "ttm_from_cumulative_income"
    assert payload["derived"]["profit_quality_alert"] is not None
    assert "投资收益" in payload["derived"]["profit_quality_alert"]
    # Q1 累计口径文案应避免说“全年”，并提示归一化 PE
    assert "年内累计" in payload["derived"]["profit_quality_alert"]
    assert "normalized_pe_ttm" in payload["derived"]["profit_quality_alert"]
    assert payload["derived"]["fcff_seasonality_note"] is not None
    assert "Q1" in payload["derived"]["fcff_seasonality_note"]


def test_profit_quality_alert_annual_caliber_wording():
    from routers.hermes_quant_tools import _profit_quality_alert

    annual = _profit_quality_alert(
        {"end_date": "20251231", "investincome_of_ebt": 70.0, "netprofit_margin": 30.0}
    )
    assert annual is not None
    assert "全年" in annual
    # 投资亏损主导也应触发
    loss = _profit_quality_alert(
        {"end_date": "20251231", "investincome_of_ebt": -80.0, "netprofit_margin": 10.0}
    )
    assert loss is not None
    assert "拖累" in loss


def test_stock_valuation_metrics_exposes_invest_income_and_segments_series():
    data = {
        "financial_daily_basic": _Collection(
            [{"ts_code": "688111.SH", "trade_date": "20260619", "ps_ttm": 7.0}]
        ),
        "financial_indicator": _Collection(
            [{"ts_code": "688111.SH", "end_date": "20260331", "tr_yoy": 24.0}]
        ),
        "financial_income": _Collection(
            [
                {
                    "ts_code": "688111.SH",
                    "end_date": "20260331",
                    "revenue": 1_000.0,
                    "invest_income": 200.0,
                }
            ]
        ),
        "stock_info": _Collection([{"ts_code": "688111.SH", "name": "分部增速"}]),
        "financial_mainbz": _Collection(
            [
                {"ts_code": "688111.SH", "end_date": "20250331", "bz_type": "P", "bz_item": "WPS", "bz_sales": 400.0},
                {"ts_code": "688111.SH", "end_date": "20260331", "bz_type": "P", "bz_item": "WPS", "bz_sales": 500.0},
            ]
        ),
    }

    payload = _stock_valuation_metrics_payload("688111.SH", data_collections=data)

    assert payload["financial"]["invest_income_yuan"] == pytest.approx(200.0)
    assert payload["financial"]["invest_income_to_revenue_pct"] == pytest.approx(20.0)
    wps = payload["segments_series"]["by_type"]["P"]["items"][0]
    assert wps["bz_item"] == "WPS"
    assert wps["yoy_pct"] == pytest.approx(25.0)


def test_stock_earnings_consensus_aggregates_forecasts_by_quarter():
    data = {
        "earnings_report_rc": _Collection(
            [
                {
                    "ts_code": "688111.SH",
                    "report_date": "20260601",
                    "org_name": "中信建投",
                    "quarter": "2025Q4",
                    "np": 300_000.0,
                    "op_rt": 1_000_000.0,
                    "eps": 6.5,
                    "rating": "买入",
                },
                {
                    "ts_code": "688111.SH",
                    "report_date": "20260520",
                    "org_name": "华泰证券",
                    "quarter": "2025Q4",
                    "np": 310_000.0,
                    "op_rt": 1_050_000.0,
                    "eps": 6.8,
                    "rating": "增持",
                },
                {
                    "ts_code": "688111.SH",
                    "report_date": "20260610",
                    "org_name": "中信建投",
                    "quarter": "2026Q4",
                    "np": 350_000.0,
                    "op_rt": 1_200_000.0,
                    "eps": 7.2,
                    "rating": "买入",
                },
            ]
        )
    }

    payload = _stock_earnings_consensus_payload("688111.SH", data_collections=data, lookback_days=365)

    assert payload["success"] is True
    assert payload["org_count"] == 2
    q4 = payload["forecasts_by_quarter"]["2025Q4"]
    assert q4["np_median_wan"] == pytest.approx(305_000.0)
    assert q4["sample_count"] == 2
    assert q4["np_sample_count"] == 2
    assert q4["revenue_sample_count"] == 2
    assert payload["rating_distribution"]["买入"] == 2
    assert payload["rating_distribution"]["增持"] == 1
    assert payload["missing"] == []


def test_stock_earnings_consensus_partial_metric_sample_count():
    data = {
        "earnings_report_rc": _Collection(
            [
                {
                    "ts_code": "688111.SH",
                    "report_date": "20260601",
                    "org_name": "机构A",
                    "quarter": "2025Q4",
                    "np": 300_000.0,
                    "eps": 6.5,
                    "rating": "买入",
                },
                {
                    "ts_code": "688111.SH",
                    "report_date": "20260520",
                    "org_name": "机构B",
                    "quarter": "2025Q4",
                    "eps": 6.8,
                    "rating": "买入",
                },
            ]
        )
    }

    payload = _stock_earnings_consensus_payload("688111.SH", data_collections=data, lookback_days=365)
    q4 = payload["forecasts_by_quarter"]["2025Q4"]
    assert q4["sample_count"] == 2
    assert q4["np_sample_count"] == 1
    assert q4["eps_sample_count"] == 2
    assert q4["np_median_wan"] == pytest.approx(300_000.0)


def test_stock_earnings_consensus_degrades_when_empty():
    data = {"earnings_report_rc": _Collection([])}

    payload = _stock_earnings_consensus_payload("688111.SH", data_collections=data)

    assert payload["success"] is True
    assert payload["report_count"] == 0
    # 空响应与正常响应 schema 对齐
    assert payload["deduped_report_count"] == 0
    assert payload["org_count"] == 0
    assert "note" in payload
    assert "earnings_report_rc" in payload["missing"]
    assert payload["missing_notes"]["earnings_report_rc"]


def test_stock_industry_comparison_computes_peer_percentiles():
    symbols = [f"68800{i}.SH" for i in range(1, 6)]
    data = {
        "stock_info": _Collection(
            [
                {
                    "ts_code": ts_code,
                    "name": f"软件{i}",
                    "industry": "软件开发",
                    "sw_l1": "计算机",
                    "sw_l2": "软件开发",
                }
                for i, ts_code in enumerate(symbols, start=1)
            ]
        ),
        "financial_daily_basic": _Collection(
            [
                {
                    "ts_code": ts_code,
                    "trade_date": "20260619",
                    "pe_ttm": 20 + i,
                    "pb": 2 + i,
                    "ps_ttm": 3 + i,
                    "total_mv": 100_000 + i * 10_000,
                }
                for i, ts_code in enumerate(symbols)
            ]
        ),
        "financial_indicator": _Collection(
            [
                {
                    "ts_code": ts_code,
                    "end_date": "20260331",
                    "roe": 8 + i,
                    "netprofit_margin": 10 + i,
                    "grossprofit_margin": 45 + i,
                    "tr_yoy": 15 + i,
                }
                for i, ts_code in enumerate(symbols)
            ]
        ),
    }

    payload = _stock_industry_comparison_payload("688003.SH", data_collections=data)

    assert payload["success"] is True
    assert payload["resolved_symbol"] == "688003.SH"
    assert payload["peer_level"] == "sw_l2"
    assert payload["peer_count"] == 5
    assert payload["as_of"]["market_trade_date"] == "20260619"
    assert payload["metrics"]["market"]["ps_ttm"]["value"] == pytest.approx(5.0)
    assert payload["metrics"]["market"]["ps_ttm"]["peer_median"] == pytest.approx(5.0)
    assert payload["metrics"]["market"]["ps_ttm"]["peer_percentile"] == pytest.approx(60.0)
    assert payload["metrics"]["financial"]["revenue_growth_pct"]["peer_percentile"] == pytest.approx(60.0)


def test_stock_industry_comparison_includes_negative_financials():
    symbols = [f"68820{i}.SH" for i in range(1, 6)]
    margins = [-8, -3, 5, 12, 20]
    data = {
        "stock_info": _Collection(
            [
                {
                    "ts_code": ts_code,
                    "name": f"亏损样本{i}",
                    "industry": "软件开发",
                    "sw_l1": "计算机",
                    "sw_l2": "软件开发",
                }
                for i, ts_code in enumerate(symbols, start=1)
            ]
        ),
        "financial_daily_basic": _Collection(
            [{"ts_code": ts_code, "trade_date": "20260619", "ps_ttm": 5.0} for ts_code in symbols]
        ),
        "financial_indicator": _Collection(
            [
                {"ts_code": ts_code, "end_date": "20260331", "netprofit_margin": margins[i]}
                for i, ts_code in enumerate(symbols)
            ]
        ),
    }

    payload = _stock_industry_comparison_payload("688201.SH", data_collections=data)

    net_margin = payload["metrics"]["financial"]["net_margin_pct"]
    # 5 个样本含 2 个负值，不应被丢弃
    assert net_margin["sample_count"] == 5
    assert net_margin["value"] == pytest.approx(-8.0)
    assert net_margin["peer_median"] == pytest.approx(5.0)
    # 目标自身为最小值，分位应为 1/5 = 20%
    assert net_margin["peer_percentile"] == pytest.approx(20.0)


def test_stock_industry_comparison_falls_back_to_sw_l1_when_l2_missing():
    symbols = [f"30000{i}.SZ" for i in range(1, 6)]
    data = {
        "stock_info": _Collection(
            [
                {
                    "ts_code": ts_code,
                    "name": f"硬件{i}",
                    "industry": "电子设备",
                    "sw_l1": "电子",
                    "sw_l2": None,
                }
                for i, ts_code in enumerate(symbols, start=1)
            ]
        ),
        "financial_daily_basic": _Collection(
            [{"ts_code": ts_code, "trade_date": "20260619", "pb": 2 + i} for i, ts_code in enumerate(symbols)]
        ),
        "financial_indicator": _Collection(
            [{"ts_code": ts_code, "end_date": "20260331", "roe": 5 + i} for i, ts_code in enumerate(symbols)]
        ),
    }

    payload = _stock_industry_comparison_payload("300003.SZ", data_collections=data, level="sw_l2")

    assert payload["success"] is True
    assert payload["requested_level"] == "sw_l2"
    assert payload["peer_level"] == "sw_l1"
    assert payload["peer_value"] == "电子"
    assert payload["metrics"]["market"]["pb"]["sample_count"] == 5


def test_stock_industry_comparison_returns_null_when_peer_sample_is_small():
    symbols = [f"68810{i}.SH" for i in range(1, 4)]
    data = {
        "stock_info": _Collection(
            [
                {
                    "ts_code": ts_code,
                    "name": f"小样本{i}",
                    "industry": "软件开发",
                    "sw_l1": "计算机",
                    "sw_l2": "软件开发",
                }
                for i, ts_code in enumerate(symbols, start=1)
            ]
        ),
        "financial_daily_basic": _Collection(
            [{"ts_code": ts_code, "trade_date": "20260619", "ps_ttm": 4 + i} for i, ts_code in enumerate(symbols)]
        ),
        "financial_indicator": _Collection([]),
    }

    payload = _stock_industry_comparison_payload("688101.SH", data_collections=data)

    assert payload["success"] is True
    assert payload["peer_count"] == 3
    assert payload["metrics"]["market"]["ps_ttm"]["value"] == pytest.approx(4.0)
    assert payload["metrics"]["market"]["ps_ttm"]["peer_median"] is None
    assert payload["metrics"]["market"]["ps_ttm"]["peer_percentile"] is None
    assert payload["metrics"]["market"]["ps_ttm"]["sample_count"] == 3
    assert "同业股票数少于 5" in payload["notes"][0]

    # 放宽 min_peer_count 后，3 个样本即可算出中位数与分位
    relaxed = _stock_industry_comparison_payload(
        "688101.SH", data_collections=data, min_peer_count=3
    )
    assert relaxed["metrics"]["market"]["ps_ttm"]["peer_median"] is not None
    assert relaxed["metrics"]["market"]["ps_ttm"]["peer_percentile"] is not None
    assert relaxed["notes"][0:1] != ["同业股票数少于 5，分位与中位数返回 null"]


def test_foreign_top10_floatholders_matches_default_banks(monkeypatch):
    collection = _Collection(
        [
            {
                "ts_code": "000001.SZ",
                "end_date": "20260331",
                "ann_date": "20260420",
                "holder_name": "GOLDMAN SACHS INTERNATIONAL",
                "holder_rank": 7,
                "raw": {"hold_amount": 1200000, "hold_ratio": 0.8},
            },
            {
                "ts_code": "000002.SZ",
                "end_date": "20260331",
                "ann_date": "20260421",
                "holder_name": "UBS AG",
                "holder_rank": 9,
                "hold_amount": 800000,
                "hold_ratio": 0.5,
            },
            {
                "ts_code": "000003.SZ",
                "end_date": "20260331",
                "holder_name": "香港中央结算有限公司",
                "holder_rank": 1,
            },
        ]
    )
    monkeypatch.setattr(
        "routers.hermes_quant_tools._stock_info_map",
        lambda symbols: {
            "000001.SZ": {"name": "平安银行", "industry": "银行"},
            "000002.SZ": {"name": "万科A", "industry": "房地产"},
        },
    )

    payload = _foreign_top10_floatholders_payload(period="20260331", collection=collection)

    assert payload["success"] is True
    assert payload["matched_count"] == 2
    assert payload["by_holder"] == {"高盛": 1, "瑞银/UBS": 1}
    assert payload["results"][0]["ts_code"] == "000001.SZ"
    assert payload["results"][0]["name"] == "平安银行"
    assert payload["results"][0]["hold_amount"] == pytest.approx(1200000)
    assert payload["results"][0]["hold_ratio_pct"] == pytest.approx(0.8)
    assert payload["results"][0]["hold_ratio_display"] == "0.80%"
    assert payload["results"][1]["norm_key"] == "UBS"


def test_foreign_top10_floatholders_filters_aliases_and_new_entries(monkeypatch):
    collection = _Collection(
        [
            {
                "ts_code": "300750.SZ",
                "end_date": "20251231",
                "holder_name": "MORGAN STANLEY & CO. INTERNATIONAL PLC",
            },
            {
                "ts_code": "300750.SZ",
                "end_date": "20260331",
                "holder_name": "摩根斯坦利国际股份有限公司",
                "holder_rank": 8,
            },
            {
                "ts_code": "600519.SH",
                "end_date": "20260331",
                "holder_name": "BARCARLY BANK PLC",
                "holder_rank": 10,
            },
        ]
    )
    monkeypatch.setattr("routers.hermes_quant_tools._stock_info_map", lambda symbols: {})

    payload = _foreign_top10_floatholders_payload(
        period="20260331",
        holders="摩根斯坦利,Barcarly",
        new_only=True,
        collection=collection,
    )

    assert payload["previous_period"] == "20251231"
    assert payload["matched_count"] == 1
    assert payload["results"][0]["ts_code"] == "600519.SH"
    assert payload["results"][0]["norm_key"] == "Barclays"


def test_foreign_top10_floatholders_filters_by_range_return(monkeypatch):
    holders = _Collection(
        [
            {
                "ts_code": "000001.SZ",
                "end_date": "20260331",
                "holder_name": "高盛国际",
                "holder_rank": 8,
            },
            {
                "ts_code": "000002.SZ",
                "end_date": "20260331",
                "holder_name": "瑞银集团",
                "holder_rank": 9,
            },
        ]
    )
    prices = _Collection(
        [
            {"symbol": "000001", "trade_date": "20260102", "close": 10.0},
            {"symbol": "000001", "trade_date": "20260615", "close": 12.5},
            {"symbol": "000002", "trade_date": "20260102", "close": 10.0},
            {"symbol": "000002", "trade_date": "20260615", "close": 14.0},
        ]
    )
    monkeypatch.setattr("routers.hermes_quant_tools._stock_info_map", lambda symbols: {})

    payload = _foreign_top10_floatholders_payload(
        period="20260331",
        return_start="20260101",
        return_end="20260615",
        max_return_pct=30,
        collection=holders,
        price_collection=prices,
    )

    assert payload["matched_count"] == 1
    assert payload["return_filter"]["max_return_pct"] == 30
    assert payload["results"][0]["ts_code"] == "000001.SZ"
    assert payload["results"][0]["return_pct"] == pytest.approx(25.0)
    assert payload["results"][0]["return_pct_display"] == "25.00%"
    assert payload["results"][0]["max_return_pct"] == 30
    assert payload["results"][0]["return_start_trade_date"] == "20260102"
    assert payload["results"][0]["return_end_trade_date"] == "20260615"
    assert payload["results"][0]["range_return"]["return_pct"] == pytest.approx(25.0)
    assert prices.find_calls == 1


def test_foreign_top10_floatholders_reports_period_changes(monkeypatch):
    collection = _Collection(
        [
            {
                "ts_code": "000001.SZ",
                "end_date": "20251231",
                "holder_name": "GOLDMAN SACHS",
                "hold_amount": 100,
                "hold_ratio": 0.5,
            },
            {
                "ts_code": "000001.SZ",
                "end_date": "20260331",
                "holder_name": "GOLDMAN SACHS",
                "hold_amount": 140,
                "hold_ratio": 0.7,
            },
            {
                "ts_code": "000002.SZ",
                "end_date": "20251231",
                "holder_name": "UBS AG",
                "hold_amount": 200,
                "hold_ratio": 1.2,
            },
            {
                "ts_code": "000002.SZ",
                "end_date": "20260331",
                "holder_name": "UBS AG",
                "hold_amount": 150,
                "hold_ratio": 0.8,
            },
            {
                "ts_code": "000003.SZ",
                "end_date": "20260331",
                "holder_name": "BARCLAYS BANK PLC",
                "hold_amount": 90,
                "hold_ratio": 0.3,
            },
            {
                "ts_code": "000004.SZ",
                "end_date": "20251231",
                "holder_name": "MORGAN STANLEY",
                "hold_amount": 80,
                "hold_ratio": 0.25,
            },
        ]
    )
    monkeypatch.setattr("routers.hermes_quant_tools._stock_info_map", lambda symbols: {})

    payload = _foreign_top10_floatholders_payload(period="20260331", collection=collection)
    changes = {(row["ts_code"], row["norm_key"]): row for row in payload["changes"]}

    assert payload["previous_period"] == "20251231"
    assert payload["change_summary"] == {"increased": 1, "decreased": 1, "new": 1, "exited": 1}
    assert changes[("000001.SZ", "GoldmanSachs")]["change_type"] == "increased"
    assert changes[("000001.SZ", "GoldmanSachs")]["hold_amount_chg"] == pytest.approx(40)
    assert changes[("000001.SZ", "GoldmanSachs")]["hold_ratio_chg_display"] == "0.20%"
    assert changes[("000002.SZ", "UBS")]["change_type"] == "decreased"
    assert changes[("000003.SZ", "Barclays")]["change_type"] == "new"
    assert changes[("000004.SZ", "MorganStanley")]["change_type"] == "exited"
