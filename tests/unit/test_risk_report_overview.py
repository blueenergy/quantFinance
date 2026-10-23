"""市场风险预警 overview：无当日 A 股数据时返回最近可看的 A 股日期。"""
import asyncio

from routers import risk_report


class FakeRiskReports:
    def __init__(self, docs):
        self.docs = docs

    def find(self, query, projection=None):
        scan_date = query["scan_date"]
        return [dict(d) for d in self.docs if d["scan_date"] == scan_date]

    def find_one(self, query, projection=None, sort=None):
        allowed = set(query["sector"]["$in"])
        cutoff = query["scan_date"]["$lte"]
        rows = [
            dict(d) for d in self.docs
            if d["sector"] in allowed and d["scan_date"] <= cutoff
        ]
        rows.sort(key=lambda d: d["scan_date"], reverse=True)
        return rows[0] if rows else None


def _run_overview(monkeypatch, docs, date):
    monkeypatch.setattr(
        risk_report,
        "get_data_db",
        lambda: {"risk_reports": FakeRiskReports(docs)},
    )
    return asyncio.run(
        risk_report.get_overview(date=date, current_user={"id": "test"})
    )


def test_overview_returns_latest_a_share_date_when_today_has_only_us(monkeypatch):
    docs = [
        {"sector": "us_tech", "scan_date": "20261023", "overall_risk_score": 87},
        {"sector": "us_macro", "scan_date": "20261023", "overall_risk_score": 52},
        {"sector": "semicon", "scan_date": "20261022", "overall_risk_score": 60},
        {"sector": "finance", "scan_date": "20261022", "overall_risk_score": 40},
        {"sector": "us_tech", "scan_date": "20261022", "overall_risk_score": 70},
    ]

    result = _run_overview(monkeypatch, docs, "20261023")

    assert result["scan_date"] == "20261023"
    assert [s["sector"] for s in result["sectors"]] == ["us_tech", "us_macro"]
    assert result["latest_a_share_date"] == "20261022"


def test_overview_latest_a_share_date_is_none_without_a_share_history(monkeypatch):
    docs = [{"sector": "us_tech", "scan_date": "20261023", "overall_risk_score": 87}]

    result = _run_overview(monkeypatch, docs, "20261023")

    assert result["latest_a_share_date"] is None
