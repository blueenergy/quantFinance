from routers.hermes_quant_tools import _institutional_holdings_summary_payload
from services.holder_taxonomy import classify_holder, parse_categories
from services.institutional_holdings import build_institutional_holdings_summary

YI = 1e8


def _row(ts_code, holder_name, amount, *, ratio=1.0, float_ratio=1.0, end_date="20260930"):
    return {
        "ts_code": ts_code,
        "holder_name": holder_name,
        "end_date": end_date,
        "raw": {
            "hold_amount": amount,
            "hold_ratio": ratio,
            "hold_float_ratio": float_ratio,
        },
    }


CURRENT = [
    _row(
        "600000.SH",
        "中国人寿保险股份有限公司-传统-普通保险产品",
        100 * YI,
        ratio=2.0,
        float_ratio=2.5,
    ),
    _row(
        "600000.SH",
        "全国社保基金一一三组合",
        50 * YI,
        ratio=1.0,
        float_ratio=1.2,
    ),
    _row(
        "000001.SZ",
        "高盛国际-自有资金",
        30 * YI,
        ratio=0.5,
        float_ratio=0.6,
    ),
    _row(
        "000001.SZ",
        "中国银行-摩根士丹利数字经济混合型证券投资基金",
        999 * YI,
        ratio=9.0,
        float_ratio=9.0,
    ),
]
PREVIOUS = [
    _row(
        "600000.SH",
        "中国人寿保险股份有限公司-传统-普通保险产品",
        80 * YI,
        ratio=1.8,
        float_ratio=2.2,
        end_date="20260630",
    ),
    _row(
        "000002.SZ",
        "全国社保基金一一三组合",
        40 * YI,
        ratio=0.8,
        float_ratio=0.9,
        end_date="20260630",
    ),
]
STOCK_INFO = {
    "600000.SH": {"name": "浦发银行", "industry": "银行"},
    "000001.SZ": {"name": "平安银行", "industry": "银行"},
    "000002.SZ": {"name": "万科A", "industry": "房地产"},
}


def test_holder_taxonomy_core_and_exclusions():
    assert classify_holder("全国社保基金四一三组合") == "social_security"
    assert classify_holder("招商基金管理有限公司-社保基金1903组合") == "social_security"
    assert classify_holder("基本养老保险基金八零二组合") == "pension"
    assert classify_holder("国泰佳泰股票专项型养老金产品-招商银行股份有限公司") == "pension"
    assert classify_holder("中国人寿保险股份有限公司-传统-普通保险产品") == "insurance"
    assert classify_holder("瑞众人寿保险有限责任公司-自有资金") == "insurance"
    assert classify_holder("中国平安保险(集团)股份有限公司") == "insurance"
    assert classify_holder("中国平安银行股份有限公司") is None
    assert classify_holder("高盛国际-自有资金") == "foreign_bank"
    assert classify_holder("MORGANSTANLEY&CO.INTERNATIONALPLC.") == "foreign_bank"
    assert classify_holder("BARCLAYSBANKPLC") == "foreign_bank"
    assert classify_holder("香港中央结算有限公司") == "hksc"
    assert classify_holder("贝莱德基金") == "foreign_asset_manager"
    assert classify_holder("先锋领航基金") == "foreign_asset_manager"
    assert classify_holder("程先锋") is None
    assert classify_holder("中国银行-摩根士丹利数字经济混合型证券投资基金") is None
    assert classify_holder("国投瑞银国家安全灵活配置混合型证券投资基金") is None


def test_parse_categories_aliases():
    assert parse_categories("险资,社保") == ("insurance", "social_security")
    assert parse_categories("外资") == ("foreign_bank", "foreign_asset_manager")
    assert parse_categories("all") == parse_categories(None)
    assert "insurance" in parse_categories("unknown")


def test_summary_groups_categories_and_changes():
    result = build_institutional_holdings_summary(
        CURRENT,
        PREVIOUS,
        period="20260930",
        previous_period="20260630",
        stock_info=STOCK_INFO,
        top_n=5,
    )
    by_category = result["summary"]["by_category"]
    assert by_category["insurance"]["hold_amount_yi"] == 100.0
    assert by_category["insurance"]["increased_stocks"] == 1
    assert by_category["social_security"]["new_stocks"] == 1
    assert by_category["social_security"]["exited_stocks"] == 1
    assert by_category["social_security"]["net_change_amount_yi"] == 10.0
    assert by_category["foreign_bank"]["new_stocks"] == 1
    # The domestic JV fund product must not be counted as a foreign bank.
    assert by_category["foreign_bank"]["hold_amount_yi"] == 30.0
    top_insurance = result["top_stocks"]["insurance"][0]
    assert top_insurance["ts_code"] == "600000.SH"
    assert top_insurance["change_type"] == "increased"
    assert top_insurance["change_amount_yi"] == 20.0
    exited = result["top_exited_stocks"]["social_security"][0]
    assert exited["ts_code"] == "000002.SZ"
    assert exited["change_amount_yi"] == -40.0


def test_summary_new_only_and_industry_filter():
    result = build_institutional_holdings_summary(
        CURRENT,
        PREVIOUS,
        period="20260930",
        previous_period="20260630",
        stock_info=STOCK_INFO,
        industry="银行",
        new_only=True,
        top_n=5,
    )
    # Previous 000002.SZ is outside the bank filter, so social security has a
    # new stock rather than an exit, and insurance's increased stock is hidden.
    social = result["summary"]["by_category"]["social_security"]
    assert social["new_stocks"] == 1
    assert social["exited_stocks"] == 0
    assert result["top_stocks"]["insurance"] == []
    assert result["top_stocks"]["social_security"][0]["ts_code"] == "600000.SH"


class _FakeColl:
    def __init__(self, docs):
        self.docs = docs

    def _match(self, doc, query):
        end = query.get("end_date")
        if isinstance(end, dict) and "$lt" in end:
            return str(doc.get("end_date")) < str(end["$lt"])
        return doc.get("end_date") == end

    def find(self, query, projection=None):
        return [doc for doc in self.docs if self._match(doc, query)]

    def distinct(self, field, query):
        return list({doc.get(field) for doc in self.docs if self._match(doc, query)})


def test_payload_resolves_previous_period_and_filters(monkeypatch):
    monkeypatch.setattr(
        "routers.hermes_quant_tools._holder_stock_info_map",
        lambda codes: STOCK_INFO,
    )
    coll = _FakeColl(CURRENT + PREVIOUS)
    result = _institutional_holdings_summary_payload(
        period="20260930",
        categories="险资,社保",
        collection=coll,
        top_n=5,
    )
    assert result["success"] is True
    assert result["previous_period"] == "20260630"
    assert set(result["summary"]["by_category"]) == {"insurance", "social_security"}
    assert result["summary"]["by_category"]["insurance"]["hold_amount_yi"] == 100.0


def test_payload_rejects_invalid_period():
    result = _institutional_holdings_summary_payload(period="2026Q3", collection=_FakeColl([]))
    assert result["success"] is False
    assert "YYYYMMDD" in result["error"]


def test_summary_flags_partial_previous_coverage():
    result = build_institutional_holdings_summary(
        CURRENT,
        PREVIOUS[:1],
        period="20260930",
        previous_period="20260630",
        stock_info=STOCK_INFO,
        top_n=5,
    )
    assert result["comparison"]["reliable"] is False
    assert result["comparison"]["previous_coverage_ratio"] == 0.5
    assert any("覆盖不足" in caveat for caveat in result["caveats"])
