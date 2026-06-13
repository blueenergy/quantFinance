import pytest

from routers.stock_workbench import (
    _ai_status,
    _analysis_evaluation_summary,
    _analysis_history,
    _attach_display_pct_change,
    _financials_section,
    _nineturn_section,
    _quote_section,
    _quote_status,
    _score_history,
    _score_status,
    _shareholder_section,
    _trading_section,
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
                {"ts_code": "000001.SZ", "end_date": "20260331", "holder_name": "UBSAG", "raw": {"hold_amount": 1000000, "hold_ratio": 1.2}},
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
    assert section["summary"]["hksc_hold_ratio_chg"] == pytest.approx(0.5)
    assert section["top10_float_change"]["intl_new"][0]["norm_key"] == "UBS"
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
