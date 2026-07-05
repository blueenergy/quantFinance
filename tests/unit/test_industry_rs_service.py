import pandas as pd

from services.industry_rs import _relative_strength_score, build_industry_rs_details


def test_build_industry_rs_details_requires_sw_daily_collection():
    result = build_industry_rs_details(
        data_db={"volume_price": None, "sw_index_member": None},
        symbol="000001.SZ",
        score_date="20250701",
    )
    assert "error" in result


def test_relative_strength_score_returns_window_breakdown():
    n = 200
    stock = pd.Series([10 + i * 0.05 for i in range(n)], index=[f"202501{i:02d}" for i in range(1, n + 1)])
    index = pd.Series([100 + i * 0.02 for i in range(n)], index=[f"202501{i:02d}" for i in range(1, n + 1)])

    score, details = _relative_strength_score(stock, index)

    assert 0 <= score <= 100
    assert len(details["windows"]) == 3
    ok_rows = [w for w in details["windows"] if w.get("status") == "ok"]
    assert len(ok_rows) == 3
    first = ok_rows[0]
    assert first["window"] == 20
    assert "stock_return" in first
    assert "index_return" in first
    assert "rs" in first
    assert abs(first["rs"] - (first["stock_return"] - first["index_return"])) < 1e-5
