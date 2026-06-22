import pytest

from routers.hermes_quant_tools import _foreign_top10_floatholders_payload, _stock_valuation_metrics_payload


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
    }

    payload = _stock_valuation_metrics_payload("688001.SH", data_collections=data)

    assert payload["success"] is True
    assert payload["resolved_symbol"] == "688001.SH"
    assert payload["name"] == "科技样本"
    assert payload["market"]["ps_ttm"] == pytest.approx(8.5)
    assert payload["financial"]["revenue_growth_pct"] == pytest.approx(28.0)
    assert payload["derived"]["rd_intensity_pct"] == pytest.approx(12.0)
    assert payload["derived"]["fcff_margin_pct"] == pytest.approx(8.0)
    assert payload["derived"]["rule_of_40_net_margin"] == pytest.approx(46.0)
    assert payload["derived"]["rule_of_40_fcff_margin"] == pytest.approx(36.0)
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
