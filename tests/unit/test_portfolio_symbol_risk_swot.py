import pytest

from services.portfolio_symbol_risk import build_workbench_swot_section


class _Ledger:
    def __init__(self, grouped):
        self.grouped = grouped
        self.calls = []

    def get_active_for_symbols(self, symbols):
        self.calls.append(list(symbols))
        return {symbol: list(self.grouped.get(symbol, [])) for symbol in symbols}

    def get_active_for_industries(self, industries):
        self.calls.append(list(industries))
        return {industry: list(self.grouped.get(industry, [])) for industry in industries}


class _Reviews:
    def __init__(self, reviews, internal_reviews=None):
        self.reviews = reviews
        self.internal_reviews = internal_reviews or {}

    def get_latest_reviews(self, symbols, review_scope="external_event"):
        source = (
            self.internal_reviews
            if review_scope == "internal_fundamental"
            else self.reviews
        )
        return {symbol: source.get(symbol) for symbol in symbols if symbol in source}


@pytest.fixture(autouse=True)
def _empty_internal_ledgers(monkeypatch):
    monkeypatch.setattr(
        "services.portfolio_symbol_risk.get_symbol_strength_ledger",
        lambda db=None: _Ledger({}),
    )
    monkeypatch.setattr(
        "services.portfolio_symbol_risk.get_symbol_weakness_ledger",
        lambda db=None: _Ledger({}),
    )
    monkeypatch.setattr(
        "services.portfolio_symbol_risk.internal_signal_enabled",
        lambda: True,
    )


def test_build_workbench_swot_section_maps_ot_and_planned_sw(monkeypatch):
    risk_ledger = _Ledger(
        {
            "000001.SZ": [
                {
                    "finding_key": "risk-1",
                    "severity": "high",
                    "summary": "监管调查",
                    "status": "active",
                    "last_confirmed_at": "2026-07-10T10:00:00",
                }
            ]
        }
    )
    opportunity_ledger = _Ledger(
        {
            "000001.SZ": [
                {
                    "finding_key": "opp-1",
                    "strength": "medium",
                    "summary": "业绩超预期",
                    "status": "active",
                    "first_detected_at": "2026-07-09T08:00:00",
                }
            ]
        }
    )
    monkeypatch.setattr(
        "services.portfolio_symbol_risk.get_symbol_risk_ledger",
        lambda db=None: risk_ledger,
    )
    monkeypatch.setattr(
        "services.portfolio_symbol_risk.get_symbol_opportunity_ledger",
        lambda db=None: opportunity_ledger,
    )
    monkeypatch.setattr(
        "services.portfolio_symbol_risk.get_symbol_signal_reviews",
        lambda db=None: _Reviews(
            {
                "000001.SZ": {
                    "analyzed_at": "2026-07-10T11:00:00",
                    "last_run_status": "analyzed",
                    "evidence_count": 3,
                }
            }
        ),
    )
    monkeypatch.setattr(
        "services.portfolio_symbol_risk.opportunity_ledger_read_enabled",
        lambda: True,
    )

    payload = build_workbench_swot_section("000001")

    assert payload["symbol"] == "000001.SZ"
    assert payload["swot"]["strength"]["status"] == "not_analyzed"
    assert payload["swot"]["weakness"]["status"] == "not_analyzed"
    assert payload["swot"]["strength"]["findings"] == []
    assert payload["swot"]["threat"]["severity"] == "high"
    assert payload["swot"]["threat"]["findings"][0]["summary"] == "监管调查"
    assert payload["swot"]["opportunity"]["strength"] == "medium"
    assert payload["swot"]["opportunity"]["findings"][0]["summary"] == "业绩超预期"
    assert payload["signal_review"]["evidence_count"] == 3
    assert payload["data_status"]["found"] is True
    assert payload["data_status"]["threat_count"] == 1
    assert payload["data_status"]["opportunity_count"] == 1
    assert len(risk_ledger.calls) == 1
    assert len(opportunity_ledger.calls) == 1
    assert risk_ledger.calls[0][0] == "000001.SZ"


def test_build_workbench_swot_section_empty_when_no_ledger_rows(monkeypatch):
    monkeypatch.setattr(
        "services.portfolio_symbol_risk.get_symbol_risk_ledger",
        lambda db=None: _Ledger({}),
    )
    monkeypatch.setattr(
        "services.portfolio_symbol_risk.get_symbol_opportunity_ledger",
        lambda db=None: _Ledger({}),
    )
    monkeypatch.setattr(
        "services.portfolio_symbol_risk.get_symbol_signal_reviews",
        lambda db=None: _Reviews({}),
    )
    monkeypatch.setattr(
        "services.portfolio_symbol_risk.opportunity_ledger_read_enabled",
        lambda: True,
    )

    payload = build_workbench_swot_section("600519.SH")

    assert payload["data_status"]["found"] is False
    assert payload["swot"]["threat"]["severity"] == "none"
    assert payload["swot"]["opportunity"]["strength"] == "none"
    assert payload["swot"]["strength"]["status"] == "not_analyzed"


def test_build_workbench_swot_section_includes_industry_references(monkeypatch):
    risk_ledger = _Ledger(
        {
            "玻璃": [
                {
                    "finding_key": "industry-risk-1",
                    "severity": "low",
                    "summary": "原材料价格波动",
                }
            ]
        }
    )
    opportunity_ledger = _Ledger(
        {
            "玻璃": [
                {
                    "finding_key": "industry-opp-1",
                    "strength": "low",
                    "summary": "玻纤行业景气修复",
                }
            ]
        }
    )
    monkeypatch.setattr(
        "services.portfolio_symbol_risk.get_symbol_risk_ledger",
        lambda db=None: risk_ledger,
    )
    monkeypatch.setattr(
        "services.portfolio_symbol_risk.get_symbol_opportunity_ledger",
        lambda db=None: opportunity_ledger,
    )
    monkeypatch.setattr(
        "services.portfolio_symbol_risk.get_symbol_signal_reviews",
        lambda db=None: _Reviews({}),
    )
    monkeypatch.setattr(
        "services.portfolio_symbol_risk.opportunity_ledger_read_enabled",
        lambda: True,
    )

    payload = build_workbench_swot_section("300196.SZ", industry="玻璃")

    reference = payload["industry_signal_reference"]
    assert reference["industry"] == "玻璃"
    assert reference["data_status"]["found"] is True
    assert reference["opportunity"]["findings"][0]["summary"] == "玻纤行业景气修复"
    assert reference["threat"]["findings"][0]["summary"] == "原材料价格波动"


def test_build_workbench_swot_section_includes_strength_and_weakness(monkeypatch):
    monkeypatch.setattr(
        "services.portfolio_symbol_risk.get_symbol_risk_ledger",
        lambda db=None: _Ledger({}),
    )
    monkeypatch.setattr(
        "services.portfolio_symbol_risk.get_symbol_opportunity_ledger",
        lambda db=None: _Ledger({}),
    )
    monkeypatch.setattr(
        "services.portfolio_symbol_risk.get_symbol_strength_ledger",
        lambda db=None: _Ledger(
            {
                "300196.SZ": [
                    {
                        "finding_key": "profitability.roe_high",
                        "rule_id": "profitability.roe_high",
                        "strength": "high",
                        "category": "profitability",
                        "summary": "ROE 水平较高且处于行业前列",
                        "engine_version": "sw-mvp-v1",
                        "rule_config_hash": "rules-hash",
                        "last_confirmed_at": "2026-07-10T12:00:00",
                    }
                ]
            }
        ),
    )
    monkeypatch.setattr(
        "services.portfolio_symbol_risk.get_symbol_weakness_ledger",
        lambda db=None: _Ledger(
            {
                "300196.SZ": [
                    {
                        "finding_key": "leverage.debt_elevated",
                        "rule_id": "leverage.debt_elevated",
                        "severity": "medium",
                        "category": "leverage",
                        "summary": "资产负债率较高且处于行业高位",
                    }
                ]
            }
        ),
    )
    monkeypatch.setattr(
        "services.portfolio_symbol_risk.get_symbol_signal_reviews",
        lambda db=None: _Reviews(
            {},
            {
                "300196.SZ": {
                    "analyzed_at": "2026-07-10T12:00:00",
                    "engine_type": "rules",
                    "engine_version": "sw-mvp-v1",
                    "rule_config_hash": "rules-hash",
                    "input_completeness": "complete",
                    "result_counts": {"strength": 1, "weakness": 1},
                }
            },
        ),
    )

    payload = build_workbench_swot_section("300196.SZ")

    assert payload["swot"]["strength"]["status"] == "completed"
    assert payload["swot"]["strength"]["strength"] == "high"
    assert payload["swot"]["strength"]["findings"][0]["rule_id"] == (
        "profitability.roe_high"
    )
    assert payload["swot"]["weakness"]["severity"] == "medium"
    assert payload["internal_signal_review"]["engine_type"] == "rules"
    assert payload["data_status"]["strength_count"] == 1
    assert payload["data_status"]["weakness_count"] == 1


def test_enrich_items_with_internal_swot_attaches_summary(monkeypatch):
    monkeypatch.setattr(
        "services.portfolio_symbol_risk.internal_signal_enabled",
        lambda: True,
    )

    class _StrengthLedger:
        def get_active_for_symbols(self, symbols):
            return {
                "600519.SH": [
                    {
                        "finding_key": "profitability.roe_high",
                        "rule_id": "profitability.roe_high",
                        "strength": "high",
                        "summary": "ROE 较高",
                    }
                ]
            }

    class _WeaknessLedger:
        def get_active_for_symbols(self, symbols):
            return {"600519.SH": []}

    monkeypatch.setattr(
        "services.portfolio_symbol_risk.get_symbol_strength_ledger",
        lambda db=None: _StrengthLedger(),
    )
    monkeypatch.setattr(
        "services.portfolio_symbol_risk.get_symbol_weakness_ledger",
        lambda db=None: _WeaknessLedger(),
    )

    from services.portfolio_symbol_risk import enrich_items_with_internal_swot

    enriched = enrich_items_with_internal_swot(
        [{"symbol": "600519.SH", "name": "茅台"}],
        db=object(),
    )

    assert enriched[0]["internal_swot"]["strength"]["findings"][0]["finding_key"] == (
        "profitability.roe_high"
    )
