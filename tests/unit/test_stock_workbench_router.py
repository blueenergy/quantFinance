import pytest

from routers.stock_workbench import (
    _ai_status,
    _analysis_evaluation_summary,
    _analysis_history,
    _attach_display_pct_change,
    _financials_section,
    _kline_section,
    _money_flow_section,
    _nineturn_section,
    _quote_section,
    _quote_status,
    _score_history,
    _score_status,
    _shareholder_section,
    _trading_section,
    _valuation_section,
    _valuation_status,
    get_stock_workbench,
)


class _Collection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    def find_one(self, query=None, projection=None, sort=None):
        rows = [dict(doc) for doc in self.docs if self._matches(doc, query or {})]
        if sort:
            for field, direction in reversed(sort):
                rows.sort(key=lambda row: row.get(field) or "", reverse=direction < 0)
        return rows[0] if rows else None

    def find(self, query=None, projection=None, sort=None, limit=0):
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
                continue
            if value != expected:
                return False
        return True


def test_display_pct_change_uses_close_against_daily_pre_close():
    data = {
        "volume_price": _Collection(
            [{"symbol": "000001.SZ", "trade_date": "20260610", "pre_close": 10.0}]
        )
    }
    quote = {
        "symbol": "000001.SZ",
        "trade_date": "20260610",
        "close": 10.04113,
        "pct_chg": 9.99,
    }

    normalized = _attach_display_pct_change(data, quote, ["000001.SZ"])

    assert normalized["display_pct_chg"] == pytest.approx(0.4113)
    assert normalized["display_pre_close"] == pytest.approx(10.0)
    assert normalized["display_pct_chg_source"] == "daily.pre_close"


def test_display_pct_change_falls_back_to_previous_daily_close():
    data = {
        "volume_price": _Collection(
            [{"symbol": "000001.SZ", "trade_date": "20260609", "close": 10.0}]
        )
    }
    quote = {
        "symbol": "000001.SZ",
        "trade_date": "20260610",
        "close": 10.5,
        "pct_chg": 0.1,
    }

    normalized = _attach_display_pct_change(data, quote, ["000001.SZ"])

    assert normalized["display_pct_chg"] == pytest.approx(5.0)
    assert normalized["display_pct_chg_source"] == "previous_daily.close"


def test_display_pct_change_uses_pct_field_when_pre_close_unavailable():
    data = {"volume_price": _Collection([])}
    quote = {"symbol": "000001.SZ", "trade_date": "20260610", "close": 10.5, "pct_chg": 0.41}

    normalized = _attach_display_pct_change(data, quote, ["000001.SZ"])

    assert normalized["display_pct_chg"] == pytest.approx(0.41)
    assert normalized["display_pct_chg_source"] == "quote_pct_field"


def test_section_status_helpers_expose_freshness_contract():
    quote_status = _quote_status({"trade_date": "20260610", "source": "daily"})
    score_status = _score_status({"score_date": "20260609"})
    ai_status = _ai_status({"created_at": "2026-06-10T12:00:00"})

    assert quote_status["found"] is True
    assert quote_status["as_of"] == "20260610"
    assert quote_status["source"] == "daily"
    assert score_status["as_of"] == "20260609"
    assert score_status["source"] == "finance.stock_scores"
    assert ai_status["expected_freshness"] == "用户手动触发后更新"


def test_score_history_orders_recent_and_omits_sentiment(monkeypatch):
    scores = _Collection(
        [
            {
                "_id": 1,
                "symbol": "000001.SZ",
                "score_date": "20260609",
                "technical_score": 60,
                "sentiment_score": 99,
            },
            {
                "_id": 2,
                "symbol": "000001.SZ",
                "score_date": "20260610",
                "technical_score": 70,
                "sentiment_score": 100,
            },
        ]
    )
    monkeypatch.setattr("routers.stock_workbench.get_db_collections", lambda: {"stock_scores": scores})

    history = _score_history("000001.SZ", limit=1)

    assert history == [{"symbol": "000001.SZ", "score_date": "20260610", "technical_score": 70}]


def test_quote_section_merges_market_money_flow_and_daily_basic(monkeypatch):
    data = {
        "volume_price_realtime_latest": _Collection([]),
        "volume_price": _Collection(
            [
                {
                    "_id": 1,
                    "symbol": "000001.SZ",
                    "trade_date": "20260610",
                    "close": 10.0,
                    "pct_chg": 1.2,
                }
            ]
        ),
        "stock_weekly": _Collection(
            [
                {
                    "_id": 11,
                    "ts_code": "000001.SZ",
                    "trade_date": "20260612",
                    "close": 10.2,
                    "pct_chg": 2.0,
                }
            ]
        ),
        "stock_monthly": _Collection(
            [
                {
                    "_id": 12,
                    "ts_code": "000001.SZ",
                    "trade_date": "20260630",
                    "close": 10.8,
                    "pct_chg": 8.0,
                }
            ]
        ),
        "financial_daily_basic": _Collection(
            [
                {
                    "_id": 2,
                    "ts_code": "000001.SZ",
                    "trade_date": "20260610",
                    "turnover_rate": 1.5,
                    "pe_ttm": 8.2,
                }
            ]
        ),
        "money_flow": _Collection(
            [
                {
                    "_id": 3,
                    "ts_code": "000001.SZ",
                    "trade_date": "20260610",
                    "buy_elg_amount": 1000.0,
                    "sell_elg_amount": 400.0,
                    "buy_lg_amount": 800.0,
                    "sell_lg_amount": 300.0,
                    "buy_md_amount": 200.0,
                    "sell_md_amount": 250.0,
                    "net_mf_amount": 1234.5,
                    "amount": 9000.0,
                },
                {
                    "_id": 4,
                    "ts_code": "000001.SZ",
                    "trade_date": "20260609",
                    "buy_elg_amount": 200.0,
                    "sell_elg_amount": 500.0,
                    "buy_lg_amount": 100.0,
                    "sell_lg_amount": 300.0,
                    "buy_md_amount": 150.0,
                    "sell_md_amount": 100.0,
                    "net_mf_amount": -500.0,
                }
            ]
        ),
    }
    monkeypatch.setattr("routers.stock_workbench.get_data_db", lambda: data)

    section = _quote_section("000001.SZ")

    assert section["quote"]["source"] == "daily"
    assert section["daily_quotes"][0]["close"] == 10.0
    assert "weekly_quotes" not in section
    assert "monthly_quotes" not in section
    assert section["daily_basic"]["pe_ttm"] == 8.2
    assert section["money_flow"]["net_mf_amount"] == 1234.5
    assert [row["trade_date"] for row in section["money_flow_history"]] == ["20260609", "20260610"]
    assert section["money_flow_history"][-1]["main_net"] == pytest.approx(1100.0)
    assert section["money_flow_history"][-1]["md_net"] == pytest.approx(-50.0)
    assert section["money_flow_summary"]["main_net_today"] == pytest.approx(1100.0)
    assert section["money_flow_summary"]["main_net_5d"] == pytest.approx(600.0)
    assert section["money_flow_summary"]["inflow_streak"] == 1
    assert section["money_flow_summary"]["unit"] == "wan_yuan"
    assert section["data_status"]["found"] is True


def test_quote_section_prefers_daily_when_realtime_trade_date_is_stale(monkeypatch):
    """Stale realtime ETL must not hide newer completed bars in quant_data.volume_price."""
    data = {
        "volume_price_realtime_latest": _Collection(
            [
                {
                    "symbol": "000001.SZ",
                    "trade_date": "20260615",
                    "close": 9.0,
                    "trade_time": "20260615150000",
                }
            ]
        ),
        "volume_price": _Collection(
            [
                {"symbol": "000001.SZ", "trade_date": "20260618", "close": 11.0, "pct_chg": 2.0},
                {"symbol": "000001.SZ", "trade_date": "20260617", "close": 10.5, "pct_chg": 0.5},
            ]
        ),
        "stock_weekly": _Collection([]),
        "stock_monthly": _Collection([]),
        "financial_daily_basic": _Collection([]),
        "money_flow": _Collection([]),
    }
    monkeypatch.setattr("routers.stock_workbench.get_data_db", lambda: data)

    section = _quote_section("000001.SZ")

    assert section["quote"]["source"] == "daily"
    assert section["quote"]["trade_date"] == "20260618"
    assert section["quote"]["close"] == 11.0
    assert section["daily_quotes"][0]["trade_date"] == "20260618"


def test_kline_section_loads_weekly_and_monthly_on_demand(monkeypatch):
    data = {
        "volume_price": _Collection([]),
        "stock_weekly": _Collection(
            [{"ts_code": "000001.SZ", "trade_date": "20260612", "close": 10.2}]
        ),
        "stock_monthly": _Collection(
            [{"ts_code": "000001.SZ", "trade_date": "20260630", "close": 10.8}]
        ),
    }
    monkeypatch.setattr("routers.stock_workbench.get_data_db", lambda: data)

    weekly = _kline_section("000001.SZ", "1w")
    monthly = _kline_section("000001.SZ", "1m")

    assert weekly["tf"] == "1w"
    assert weekly["rows"][0]["close"] == 10.2
    assert weekly["rows"][0]["amount_unit"] == "yuan"
    assert weekly["rows"][0]["volume_unit"] == "shares"
    assert weekly["data_status"]["source"] == "quant_data.stock_weekly"
    assert monthly["tf"] == "1m"
    assert monthly["rows"][0]["close"] == 10.8
    assert monthly["rows"][0]["amount_unit"] == "yuan"
    assert monthly["rows"][0]["volume_unit"] == "shares"
    assert monthly["data_status"]["source"] == "quant_data.stock_monthly"


def test_kline_section_adds_partial_weekly_and_monthly_rows(monkeypatch):
    data = {
        "volume_price": _Collection(
            [
                {
                    "symbol": "000001.SZ",
                    "trade_date": "20260617",
                    "open": 10.6,
                    "high": 11.1,
                    "low": 10.5,
                    "close": 10.9,
                    "pre_close": 10.5,
                    "vol": 300,
                    "amount": 3000,
                },
                {
                    "symbol": "000001.SZ",
                    "trade_date": "20260616",
                    "open": 10.3,
                    "high": 10.8,
                    "low": 10.1,
                    "close": 10.6,
                    "pre_close": 10.2,
                    "vol": 200,
                    "amount": 2000,
                },
                {
                    "symbol": "000001.SZ",
                    "trade_date": "20260615",
                    "open": 10.0,
                    "high": 10.4,
                    "low": 9.9,
                    "close": 10.3,
                    "pre_close": 9.8,
                    "vol": 100,
                    "amount": 1000,
                },
            ]
        ),
        "stock_weekly": _Collection(
            [{"ts_code": "000001.SZ", "trade_date": "20260612", "close": 10.2}]
        ),
        "stock_monthly": _Collection(
            [{"ts_code": "000001.SZ", "trade_date": "20260529", "close": 9.8}]
        ),
    }
    monkeypatch.setattr("routers.stock_workbench.get_data_db", lambda: data)

    weekly = _kline_section("000001.SZ", "1w")
    monthly = _kline_section("000001.SZ", "1m")

    for section in (weekly, monthly):
        partial = section["rows"][0]
        assert partial["is_partial"] is True
        assert partial["source"] == "dynamic_daily_aggregate"
        assert partial["trade_date"] == "20260617"
        assert partial["period_start"] == "20260615"
        assert partial["open"] == pytest.approx(10.0)
        assert partial["close"] == pytest.approx(10.9)
        assert partial["high"] == pytest.approx(11.1)
        assert partial["low"] == pytest.approx(9.9)
        assert partial["vol"] == pytest.approx(600)
        assert partial["volume_unit"] == "hands"
        assert partial["amount"] == pytest.approx(6000000)
        assert partial["amount_unit"] == "yuan"
    assert weekly["rows"][1]["trade_date"] == "20260612"
    assert monthly["rows"][1]["trade_date"] == "20260529"


def test_money_flow_section_loads_history_on_demand(monkeypatch):
    data = {
        "money_flow": _Collection(
            [
                {
                    "ts_code": "000001.SZ",
                    "trade_date": "20260612",
                    "buy_elg_amount": 1000.0,
                    "sell_elg_amount": 200.0,
                    "buy_lg_amount": 500.0,
                    "sell_lg_amount": 100.0,
                    "amount": 9000.0,
                },
                {
                    "ts_code": "000001.SZ",
                    "trade_date": "20260611",
                    "buy_elg_amount": 200.0,
                    "sell_elg_amount": 300.0,
                    "buy_lg_amount": 100.0,
                    "sell_lg_amount": 200.0,
                    "amount": 8000.0,
                },
            ]
        )
    }
    monkeypatch.setattr("routers.stock_workbench.get_data_db", lambda: data)

    section = _money_flow_section("000001.SZ", "1m")

    assert section["tf"] == "1m"
    assert [row["trade_date"] for row in section["history"]] == ["20260611", "20260612"]
    assert section["history"][-1]["main_net"] == pytest.approx(1200.0)
    assert section["summary"]["main_net_today"] == pytest.approx(1200.0)
    assert section["data_status"]["source"] == "quant_data.money_flow"


def test_nineturn_section_enriches_raw_tushare_signal(monkeypatch):
    daily_rows = []
    for idx in range(60):
        day = idx + 1
        close = 100 + idx
        daily_rows.append(
            {
                "symbol": "000001.SZ",
                "trade_date": f"202604{day:02d}",
                "open": close - 1,
                "high": close + 2,
                "low": close - 2,
                "close": close,
                "vol": 1000,
            }
        )
    daily_rows[-4]["low"] = 150
    daily_rows[-3]["low"] = 151
    daily_rows[-2]["low"] = 149
    daily_rows[-1]["low"] = 148
    daily_rows[-1]["vol"] = 400
    nineturn_rows = [
        {
            "ts_code": "000001.SZ",
            "symbol": "000001.SZ",
            "trade_date": daily_rows[-1]["trade_date"],
            "freq": "daily",
            "down_count": 9,
            "nine_down_turn": "-9",
        }
    ]
    data = {
        "volume_price": _Collection(daily_rows),
        "stk_nineturn": _Collection(nineturn_rows),
    }
    monkeypatch.setattr("routers.stock_workbench.get_data_db", lambda: data)

    section = _nineturn_section("000001.SZ")

    assert section["data_status"]["found"] is True
    assert section["latest_signal"]["grade"] == "strong"
    assert section["signals"][0]["perfect"] is True
    assert section["signals"][0]["vol_filter_pass"] is True
    assert section["signals"][0]["trend_filter_pass"] is True


def test_financials_section_merges_statements_and_earnings(monkeypatch):
    data = {
        "financial_income": _Collection(
            [{"ts_code": "000001.SZ", "end_date": "20260331", "revenue": 100.0}]
        ),
        "financial_balance": _Collection(
            [{"ts_code": "000001.SZ", "end_date": "20260331", "total_assets": 1000.0}]
        ),
        "financial_cashflow": _Collection(
            [{"ts_code": "000001.SZ", "end_date": "20260331", "n_cashflow_act": 20.0}]
        ),
        "financial_indicator": _Collection(
            [{"ts_code": "000001.SZ", "end_date": "20260331", "roe": 12.3}]
        ),
        "earnings_forecast": _Collection(
            [{"ts_code": "000001.SZ", "ann_date": "20260401", "type": "预增"}]
        ),
        "earnings_express": _Collection(
            [{"ts_code": "000001.SZ", "ann_date": "20260402", "revenue": 101.0}]
        ),
        "earnings_disclosure": _Collection(
            [{"ts_code": "000001.SZ", "ann_date": "20260403", "pre_date": "20260425"}]
        ),
        "earnings_report_rc": _Collection(
            [{"ts_code": "000001.SZ", "report_date": "20260404", "org_name": "券商", "report_title": "业绩点评"}]
        ),
    }
    monkeypatch.setattr("routers.stock_workbench.get_data_db", lambda: data)

    section = _financials_section("000001.SZ")

    assert section["income"][0]["revenue"] == 100.0
    assert section["balance"][0]["total_assets"] == 1000.0
    assert section["cashflow"][0]["n_cashflow_act"] == 20.0
    assert section["indicators"][0]["roe"] == 12.3
    assert section["earnings"]["forecast"][0]["type"] == "预增"
    assert section["data_status"]["as_of"] == "20260331"


def test_valuation_status_exposes_lazy_load_contract():
    status = _valuation_status()

    assert status["found"] is False
    assert status["expected_freshness"] == "进入估值模型页签后按需加载"
    assert "financial_daily_basic" in status["source"]


def test_valuation_section_returns_models_and_percentiles(monkeypatch):
    daily_basic = []
    for idx in range(40):
        day = 40 - idx
        daily_basic.append(
            {
                "ts_code": "000001.SZ",
                "trade_date": f"202605{day:02d}",
                "close": 10.0,
                "pe_ttm": 8.0 + idx * 0.1,
                "pb": 0.8 + idx * 0.01,
                "ps_ttm": 1.0 + idx * 0.02,
                "dv_ttm": 5.0,
                "total_mv": 100000.0,
                "circ_mv": 90000.0,
            }
        )
    data = {
        "financial_daily_basic": _Collection(daily_basic),
        "financial_indicator": _Collection(
            [
                {"ts_code": "000001.SZ", "end_date": "20260331", "fcff": 500000000.0, "roe": 12.0, "netprofit_yoy": 8.0},
                {"ts_code": "000001.SZ", "end_date": "20251231", "fcff": 480000000.0, "roe": 11.0, "netprofit_yoy": 7.0},
                {"ts_code": "000001.SZ", "end_date": "20250930", "fcff": 460000000.0, "roe": 10.0, "netprofit_yoy": 6.0},
                {"ts_code": "000001.SZ", "end_date": "20250630", "fcff": 440000000.0, "roe": 9.0, "netprofit_yoy": 5.0},
                {"ts_code": "000001.SZ", "end_date": "20250331", "fcff": 200000000.0, "roe": 4.0, "netprofit_yoy": 4.0},
            ]
        ),
        "financial_cashflow": _Collection(
            [{"ts_code": "000001.SZ", "end_date": "20251231", "n_cashflow_act": 250000000.0}]
        ),
        "financial_income": _Collection(
            [{"ts_code": "000001.SZ", "end_date": "20251231", "n_income_attr_p": 120000000.0}]
        ),
        "financial_dividend": _Collection(
            [
                {"ts_code": "000001.SZ", "end_date": "20251231", "ann_date": "20260320", "div_proc": "实施", "cash_div_tax": 0.8, "ex_date": "20260601", "base_share": 10000.0},
                {"ts_code": "000001.SZ", "end_date": "20241231", "ann_date": "20250320", "div_proc": "实施", "cash_div_tax": 0.6, "ex_date": "20250601", "base_share": 10000.0},
                {"ts_code": "000001.SZ", "end_date": "20231231", "ann_date": "20240320", "div_proc": "实施", "cash_div_tax": 0.5, "ex_date": "20240601", "base_share": 10000.0},
            ]
        ),
        "financial_balance": _Collection(
            [
                {
                    "ts_code": "000001.SZ",
                    "end_date": "20251231",
                    "money_cap": 50_000_000.0,
                    "st_borr": 300_000_000.0,
                    "lt_borr": 0.0,
                    "bond_payable": 0.0,
                    "non_cur_liab_due_1y": 0.0,
                    "minority_int": 100_000_000.0,
                }
            ]
        ),
        "stock_info": _Collection([{"ts_code": "000001.SZ", "industry": "食品饮料"}]),
    }
    finance = {
        "stock_scores": _Collection(
            [{"symbol": "000001.SZ", "score_date": "20260601", "value_score": 72.0}]
        )
    }
    monkeypatch.setattr("routers.stock_workbench.get_data_db", lambda: data)
    monkeypatch.setattr("routers.stock_workbench.get_db_collections", lambda: finance)

    section = _valuation_section("000001.SZ")

    assert section["data_status"]["found"] is True
    assert section["data_status"]["as_of"] == "20260540"
    assert section["market_multiples"]["current"]["pe_ttm"] == pytest.approx(8.0)
    assert section["market_multiples"]["percentiles"]["pe_ttm"] is not None
    assert section["dcf"]["status"] == "ok"
    assert section["dcf"]["input"]["method"] == "ttm_from_cumulative_indicator"
    assert section["dcf"]["input"]["value"] == pytest.approx(780000000.0)
    assert section["ddm"]["status"] == "ok"
    assert len(section["ddm"]["scenarios"]) == 3
    assert section["ddm"]["annual_dividends"][0]["total_cash_dividend_wan"] == pytest.approx(8000.0)
    assert len(section["dcf"]["scenarios"]) == 3
    assert section["dcf"]["scenarios"][1]["name"] == "base"
    base_dcf = section["dcf"]["scenarios"][1]
    assert base_dcf["net_debt_wan"] == pytest.approx(25000.0)
    assert base_dcf["minority_interest_wan"] == pytest.approx(10000.0)
    assert base_dcf["enterprise_value_wan"] == pytest.approx(
        base_dcf["equity_value_wan"] + base_dcf["net_debt_wan"] + base_dcf["minority_interest_wan"]
    )
    assert section["dcf"]["net_debt_adjustment"]["applied"] is True
    assert section["dcf"]["per_share"]["status"] == "ok"
    assert section["dcf"]["per_share"]["shares_implied_wan"] == pytest.approx(10000.0)
    assert section["ddm"]["dividend_quality"]["status"] == "ok"
    assert "signals" in section["ddm"]["dividend_quality"]
    base_ddm = section["ddm"]["scenarios"][1]
    assert "ke_breakdown" in base_ddm
    assert base_ddm["ke"] >= section["dcf"]["scenarios"][1]["wacc"]
    assert base_ddm["growth"] < base_ddm["ke"]
    assert section["relative_valuation"]["method"] == "current multiples vs own 3-5y history percentile"
    assert section["model_suitability"]["recommended_model"] == "dcf"
    assert any("DDM" in reason for reason in section["model_suitability"]["reasons"])
    assert section["value_score_link"]["value_score"] == 72.0


def test_valuation_section_degrades_when_inputs_are_missing(monkeypatch):
    data = {
        "financial_daily_basic": _Collection([]),
        "financial_indicator": _Collection([]),
        "financial_cashflow": _Collection([]),
        "financial_dividend": _Collection([]),
        "financial_income": _Collection([]),
        "stock_info": _Collection([]),
    }
    monkeypatch.setattr("routers.stock_workbench.get_data_db", lambda: data)
    monkeypatch.setattr("routers.stock_workbench.get_db_collections", lambda: {"stock_scores": _Collection([])})

    section = _valuation_section("000001.SZ")

    assert section["data_status"]["found"] is False
    assert section["dcf"]["status"] == "insufficient_data"
    assert section["ddm"]["status"] == "insufficient_data"
    assert section["relative_valuation"]["status"] == "insufficient_data"
    assert "dividend_quality" in section["ddm"]


def test_valuation_dcf_skips_net_debt_for_financial_sector(monkeypatch):
    daily_basic = []
    for idx in range(40):
        day = 40 - idx
        daily_basic.append(
            {
                "ts_code": "600000.SH",
                "trade_date": f"202605{day:02d}",
                "close": 10.0,
                "pe_ttm": 8.0,
                "pb": 0.8,
                "ps_ttm": 1.0,
                "dv_ttm": 3.0,
                "total_mv": 100000.0,
                "circ_mv": 90000.0,
            }
        )
    data = {
        "financial_daily_basic": _Collection(daily_basic),
        "financial_indicator": _Collection(
            [
                {"ts_code": "600000.SH", "end_date": "20260331", "fcff": 500000000.0, "roe": 10.0, "netprofit_yoy": 5.0},
                {"ts_code": "600000.SH", "end_date": "20251231", "fcff": 480000000.0, "roe": 9.0, "netprofit_yoy": 4.0},
                {"ts_code": "600000.SH", "end_date": "20250930", "fcff": 460000000.0, "roe": 8.0, "netprofit_yoy": 3.0},
                {"ts_code": "600000.SH", "end_date": "20250630", "fcff": 440000000.0, "roe": 7.0, "netprofit_yoy": 2.0},
                {"ts_code": "600000.SH", "end_date": "20250331", "fcff": 200000000.0, "roe": 4.0, "netprofit_yoy": 1.0},
            ]
        ),
        "financial_cashflow": _Collection(
            [{"ts_code": "600000.SH", "end_date": "20251231", "n_cashflow_act": 250000000.0}]
        ),
        "financial_balance": _Collection(
            [
                {
                    "ts_code": "600000.SH",
                    "end_date": "20251231",
                    "money_cap": 0.0,
                    "st_borr": 1_000_000_000_000.0,
                    "lt_borr": 0.0,
                    "bond_payable": 0.0,
                    "non_cur_liab_due_1y": 0.0,
                    "minority_int": 500_000_000_000.0,
                }
            ]
        ),
        "financial_dividend": _Collection([]),
        "financial_income": _Collection([]),
        "stock_info": _Collection([{"ts_code": "600000.SH", "industry": "银行"}]),
    }
    monkeypatch.setattr("routers.stock_workbench.get_data_db", lambda: data)
    monkeypatch.setattr("routers.stock_workbench.get_db_collections", lambda: {"stock_scores": _Collection([])})

    section = _valuation_section("600000.SH")

    assert section["dcf"]["status"] == "ok"
    assert section["dcf"]["net_debt_adjustment"]["skipped_financial_sector"] is True
    base = section["dcf"]["scenarios"][1]
    assert base["enterprise_value_wan"] == pytest.approx(base["equity_value_wan"])
    assert base["net_debt_wan"] is None
    data = {
        "stock_info": _Collection([]),
        "stock_basic": _Collection([]),
        "volume_price_realtime_latest": _Collection([]),
        "volume_price": _Collection([]),
    }
    monkeypatch.setattr("routers.stock_workbench.get_data_db", lambda: data)
    monkeypatch.setattr("routers.stock_workbench.get_db_collections", lambda: {"stock_scores": _Collection([])})

    response = get_stock_workbench("000001.SZ", current_user={"id": "u1"})

    assert response["data"]["data_status"]["sections"]["valuation"]["found"] is False
    assert "financial_daily_basic" in response["data"]["data_status"]["sections"]["valuation"]["source"]


def test_analysis_history_wraps_recent_rows(monkeypatch):
    rows = [
        {"id": "new", "created_at": "2026-06-10", "analysis": {"investment_advice": "关注"}},
        {"id": "old", "created_at": "2026-06-09", "analysis": {"investment_advice": "观望"}},
    ]

    def fake_payload(user_id, *, symbol=None, page=1, limit=50):
        return {"success": True, "data": rows[:limit], "total": 2, "page": page, "limit": limit}

    monkeypatch.setattr("routers.stock_workbench.build_user_analysis_history_payload", fake_payload)

    history = _analysis_history("u1", "000001.SZ", limit=1)

    assert history["rows"] == [rows[0]]
    assert history["total"] == 2


def test_analysis_evaluation_summary_counts_accuracy(monkeypatch):
    history = {
        "rows": [
            {
                "id": "h1",
                "created_at": "2026-06-10",
                "analysis_mode": "multi_expert_v1",
                "analysis": {"investment_advice": "关注"},
            },
            {
                "id": "h2",
                "created_at": "2026-06-09",
                "analysis": {"investment_advice": "观望"},
            },
            {
                "id": "h3",
                "created_at": "2026-06-08",
                "analysis": {"investment_advice": "回避"},
            },
        ],
        "total": 3,
    }
    analyzer = {
        "analysis_evaluations": _Collection(
            [
                {
                    "history_id": "h1",
                    "evaluation_status": "completed",
                    "evaluated_at": "2026-06-12",
                    "llm_review": {
                        "outcome_accuracy": "accurate",
                        "reasoning_quality": "strong",
                        "summary": "方向兑现",
                    },
                },
                {
                    "history_id": "h2",
                    "evaluation_status": "completed",
                    "evaluated_at": "2026-06-11",
                    "llm_review": {
                        "outcome_accuracy": "mixed",
                        "reasoning_quality": "partial",
                    },
                },
            ]
        )
    }
    monkeypatch.setattr("routers.stock_workbench.get_analyzer_db", lambda: analyzer)

    summary = _analysis_evaluation_summary(history)

    assert summary["evaluated_count"] == 2
    assert summary["pending_count"] == 1
    assert summary["accuracy_counts"] == {"accurate": 1, "mixed": 1}
    assert summary["accuracy_rate"] == pytest.approx(50.0)
    assert summary["hit_rate"] == pytest.approx(75.0)
    assert summary["latest_evaluations"][0]["title"] == "关注"
    assert summary["data_status"]["as_of"] == "2026-06-12"


def test_shareholder_section_merges_chips_sources_and_summary(monkeypatch):
    data = {
        "stk_holdernumber": _Collection(
            [
                {"ts_code": "000001.SZ", "end_date": "20260331", "holder_num": 9000.0},
                {"ts_code": "000001.SZ", "end_date": "20251231", "holder_num": 10000.0},
            ]
        ),
        "hk_hold": _Collection(
            [
                {"ts_code": "000001.SZ", "trade_date": "20260331", "vol": 5000000, "ratio": 5.5},
                {"ts_code": "000001.SZ", "trade_date": "20251231", "vol": 4000000, "ratio": 5.0},
            ]
        ),
        "top10_floatholders": _Collection(
            [
                {"ts_code": "000001.SZ", "end_date": "20260331", "holder_name": "UBSAG", "raw": {"hold_amount": 948100, "hold_ratio": 0.39}},
                {"ts_code": "000001.SZ", "end_date": "20251231", "holder_name": "UBS AG", "raw": {"hold_amount": 1900000, "hold_ratio": 0.79}},
                {"ts_code": "000001.SZ", "end_date": "20260331", "holder_name": "BARCLAYS BANK PLC", "raw": {"hold_amount": 1000000, "hold_ratio": 1.2}},
                {"ts_code": "000001.SZ", "end_date": "20260331", "holder_name": "GOLDMAN SACHS", "raw": {"hold_amount": 1500000, "hold_ratio": 1.5}},
                {"ts_code": "000001.SZ", "end_date": "20251231", "holder_name": "GOLDMAN SACHS", "raw": {"hold_amount": 1200000, "hold_ratio": 1.1}},
                {"ts_code": "000001.SZ", "end_date": "20251231", "holder_name": "MORGAN STANLEY", "raw": {"hold_amount": 900000, "hold_ratio": 0.9}},
                {"ts_code": "000001.SZ", "end_date": "20260331", "holder_name": "香港中央结算有限公司", "raw": {"hold_amount": 8000000, "hold_ratio": 8.0}},
                {"ts_code": "000001.SZ", "end_date": "20251231", "holder_name": "香港中央结算有限公司", "raw": {"hold_amount": 7000000, "hold_ratio": 7.5}},
            ]
        ),
        "stk_holdertrade": _Collection(
            [{"ts_code": "000001.SZ", "ann_date": "20260320", "holder_name": "张三", "in_de": "IN", "change_vol": 100000}]
        ),
        "share_float": _Collection(
            [{"ts_code": "000001.SZ", "ann_date": "20260101", "float_date": "20270101", "share_type": "定增", "float_share": 200000}]
        ),
        "repurchase": _Collection(
            [{"ts_code": "000001.SZ", "ann_date": "20260210", "proc": "完成", "vol": 300000, "amount": 5000000.0}]
        ),
    }
    monkeypatch.setattr("routers.stock_workbench.get_data_db", lambda: data)

    section = _shareholder_section("000001.SZ")

    assert section["summary"]["holder_num"] == 9000.0
    # 户数从 10000 -> 9000，环比 -10%（筹码集中）
    assert section["summary"]["holder_num_chg_pct"] == pytest.approx(-10.0)
    assert section["summary"]["northbound_ratio"] == 5.5
    assert section["summary"]["northbound_ratio_chg"] == pytest.approx(0.5)
    assert section["summary"]["intl_new_count"] == 1
    assert section["summary"]["intl_change_count"] == 4
    assert section["summary"]["hksc_hold_ratio_chg"] == pytest.approx(0.5)
    assert section["top10_float_change"]["intl_new"][0]["norm_key"] == "Barclays"
    changes = {row["norm_key"]: row for row in section["top10_float_change"]["intl_changes"]}
    assert section["top10_float_change"]["intl_change_summary"] == {"new": 1, "increased": 1, "exited": 1, "decreased": 1}
    assert changes["Barclays"]["change_type"] == "new"
    assert changes["UBS"]["change_type"] == "decreased"
    assert changes["UBS"]["hold_ratio_chg"] == pytest.approx(-0.4)
    assert changes["UBS"]["hold_ratio_rel_chg_pct"] == pytest.approx(-50.6329)
    assert changes["GoldmanSachs"]["change_type"] == "increased"
    assert changes["GoldmanSachs"]["hold_ratio_chg"] == pytest.approx(0.4)
    assert changes["MorganStanley"]["change_type"] == "exited"
    assert section["top10_float_change"]["hksc"]["change_type"] == "increased"
    assert section["top10_float_change"]["hksc"]["hold_ratio_rel_chg_pct"] == pytest.approx(6.6666667)
    assert section["top10_float_change"]["hksc"]["hold_amount_chg"] == pytest.approx(1000000)
    assert section["holder_trades"][0]["holder_name"] == "张三"
    assert section["share_floats"][0]["share_type"] == "定增"
    assert section["repurchases"][0]["proc"] == "完成"
    assert section["data_status"]["as_of"] == "20260331"
    assert section["data_status"]["found"] is True


def test_trading_section_merges_user_context(monkeypatch):
    collections = {
        "user_watchlists": _Collection(
            [{"user_id": "u1", "symbols": ["000001.SZ", "600519.SH"]}]
        ),
        "watchlist_strategies": _Collection(
            [{"user_id": "u1", "symbol": "000001.SZ", "strategy_key": "dragon", "updated_at": 2}]
        ),
        "trader_positions": _Collection(
            [{"user_id": "u1", "symbol": "000001.SZ", "quantity": 100, "updated_at": 3}]
        ),
        "trade_signals": _Collection(
            [{"user_id": "u1", "symbol": "000001.SZ", "action": "buy", "timestamp": 4}]
        ),
    }
    monkeypatch.setattr("routers.stock_workbench.get_db_collections", lambda: collections)

    section = _trading_section("u1", "000001.SZ")

    assert section["watchlist"]["in_watchlist"] is True
    assert section["watchlist_strategies"][0]["strategy_key"] == "dragon"
    assert section["positions"][0]["quantity"] == 100
    assert section["recent_signals"][0]["action"] == "buy"
    assert section["data_status"]["found"] is True
