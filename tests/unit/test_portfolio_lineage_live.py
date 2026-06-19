"""Unit tests for cross-plan live lineage aggregation helpers."""

from services.portfolio_lineage_live import (
    build_lineage_live_positions,
    build_lineage_live_trades,
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


def test_build_lineage_live_trades_fifo_splits_one_sell_across_buy_lots():
    executions = [
        {
            "order_id": "buy-1",
            "symbol": "000001.SZ",
            "name": "平安银行",
            "side": "BUY",
            "filled_size": 1000,
            "filled_price": 10.0,
            "total_fee": 10.0,
            "timestamp": 1.0,
            "display_time": "2025-01-02 09:31:00",
            "source_plan_base_date": "20250101",
            "source_plan_id": "plan-a",
        },
        {
            "order_id": "buy-2",
            "symbol": "000001.SZ",
            "name": "平安银行",
            "side": "BUY",
            "filled_size": 500,
            "filled_price": 11.0,
            "total_fee": 5.0,
            "timestamp": 2.0,
            "display_time": "2025-01-20 09:31:00",
            "source_plan_base_date": "20250120",
            "source_plan_id": "plan-b",
        },
        {
            "order_id": "sell-1",
            "symbol": "000001.SZ",
            "name": "平安银行",
            "side": "SELL",
            "filled_size": 1200,
            "filled_price": 12.0,
            "total_fee": 12.0,
            "timestamp": 3.0,
            "display_time": "2025-02-10 09:31:00",
            "source_plan_id": "plan-c",
        },
    ]

    trades = build_lineage_live_trades(executions, {"000001.SZ": 13.0})

    assert [row["status"] for row in trades] == ["closed", "closed", "open"]
    assert [row["quantity"] for row in trades] == [1000, 200, 300]
    assert trades[0]["rebalance_date"] == "20250101"
    assert trades[1]["rebalance_date"] == "20250120"
    assert trades[0]["buy_price"] == 10.0
    assert trades[0]["sell_price"] == 12.0
    assert trades[0]["fee"] == 20.0
    assert trades[0]["net_pnl"] == 1980.0
    assert trades[1]["fee"] == 4.0
    assert trades[1]["net_pnl"] == 196.0
    assert trades[2]["sell_price"] is None
    assert trades[2]["holding_return"] == round(13.0 / 11.0 - 1, 6)
    assert trades[2]["net_pnl"] == 597.0
    assert round(sum(row["fee"] for row in trades), 2) == 27.0


def test_build_lineage_live_trades_fifo_splits_one_buy_across_multiple_sells():
    executions = [
        {
            "order_id": "buy-1",
            "symbol": "600000.SH",
            "stock_name": "浦发银行",
            "side": "BUY",
            "filled_size": 1000,
            "filled_price": 10.0,
            "total_fee": 10.0,
            "timestamp": 1.0,
            "display_time": "2025-01-02 09:31:00",
            "source_plan_base_date": "20250101",
        },
        {
            "order_id": "sell-1",
            "symbol": "600000.SH",
            "side": "SELL",
            "filled_size": 400,
            "filled_price": 11.0,
            "total_fee": 4.0,
            "timestamp": 2.0,
            "display_time": "2025-01-10 09:31:00",
        },
        {
            "order_id": "sell-2",
            "symbol": "600000.SH",
            "side": "SELL",
            "filled_size": 300,
            "filled_price": 12.0,
            "total_fee": 3.0,
            "timestamp": 3.0,
            "display_time": "2025-01-20 09:31:00",
        },
    ]

    trades = build_lineage_live_trades(executions, {"600000.SH": 9.0})

    assert [row["quantity"] for row in trades] == [400, 300, 300]
    assert [row["status"] for row in trades] == ["closed", "closed", "open"]
    assert trades[0]["fee"] == 8.0
    assert trades[0]["net_pnl"] == 392.0
    assert trades[1]["fee"] == 6.0
    assert trades[1]["net_pnl"] == 594.0
    assert trades[2]["fee"] == 3.0
    assert trades[2]["net_pnl"] == -303.0
    assert trades[2]["name"] == "浦发银行"
    assert round(sum(row["fee"] for row in trades), 2) == 17.0


def test_build_lineage_live_positions_adds_symbol_name():
    grouped = [
        {
            "order_id": "o1",
            "symbol": "000001.SZ",
            "side": "BUY",
            "filled_size": 100,
            "filled_price": 10.0,
            "timestamp": 1.0,
        }
    ]

    out = build_lineage_live_positions(grouped, {"000001.SZ": 11.0}, name_map={"000001": "平安银行"})

    assert out["positions"][0]["name"] == "平安银行"
