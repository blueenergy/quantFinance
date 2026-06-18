"""Unit tests for cross-plan live lineage aggregation helpers."""

from services.portfolio_lineage_live import (
    build_lineage_live_positions,
    merge_lineage_live_executions,
)


def test_merge_lineage_live_executions_sorts_and_tags_plan():
    signals = [
        {"order_id": "o1", "plan_id": "plan-a", "symbol": "000001.SZ"},
        {"order_id": "o2", "plan_id": "plan-b", "symbol": "600000.SH"},
    ]
    executions = [
        {"order_id": "o2", "symbol": "600000.SH", "side": "BUY", "filled_size": 100, "filled_price": 11.0, "timestamp": 200.0},
        {"order_id": "o1", "symbol": "000001.SZ", "side": "BUY", "filled_size": 200, "filled_price": 10.0, "timestamp": 100.0},
    ]
    plan_meta = {"plan-a": {"base_date": "20250101"}, "plan-b": {"base_date": "20250120"}}
    merged = merge_lineage_live_executions(signals, executions, plan_meta)
    assert [row["order_id"] for row in merged] == ["o1", "o2"]
    assert merged[0]["source_plan_base_date"] == "20250101"
    assert merged[1]["source_plan_base_date"] == "20250120"


def test_build_lineage_live_positions_weighted_cost_and_unrealized():
    grouped = [
        {
            "order_id": "o1",
            "symbol": "000001.SZ",
            "side": "BUY",
            "filled_size": 100,
            "filled_price": 10.0,
            "timestamp": 1.0,
            "total_fee": 0.0,
        },
        {
            "order_id": "o2",
            "symbol": "000001.SZ",
            "side": "BUY",
            "filled_size": 100,
            "filled_price": 12.0,
            "timestamp": 2.0,
            "total_fee": 0.0,
        },
    ]
    price_map = {"000001.SZ": 11.0}
    out = build_lineage_live_positions(grouped, price_map, realtime_docs={})
    pos = {p["symbol"]: p for p in out["positions"]}["000001.SZ"]
    assert pos["shares"] == 200
    assert pos["avg_cost"] == 11.0
    assert abs(pos["unrealized_pnl"]) < 0.01
    assert out["summary"]["holding_count"] == 1
    assert out["summary"]["total_market_value"] == 2200.0
