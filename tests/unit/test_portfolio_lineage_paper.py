"""Unit tests for cross-plan paper lineage aggregation helpers."""

from services.portfolio_lineage_paper import (
    build_lineage_paper_trades,
    merge_lineage_paper_executions,
)


def test_merge_lineage_paper_executions_tags_plan_and_skips_blocked():
    executions = [
        {
            "execution_id": "e1",
            "plan_id": "plan-a",
            "symbol": "000001.SZ",
            "name": "Ping",
            "action": "buy",
            "quantity": 100,
            "price": 10.0,
            "execute_date": "20250107",
            "created_at": "2025-01-07T01:30:00",
        },
        {
            "execution_id": "e2",
            "plan_id": "plan-a",
            "symbol": "600000.SH",
            "action": "blocked",
            "quantity": 0,
            "price": 11.0,
            "execute_date": "20250107",
        },
        {
            "execution_id": "e3",
            "plan_id": "plan-b",
            "symbol": "000001.SZ",
            "name": "Ping",
            "action": "sell",
            "quantity": 50,
            "price": 12.0,
            "execute_date": "20250120",
            "created_at": "2025-01-20T01:30:00",
        },
    ]
    plan_meta = {"plan-a": {"base_date": "20250106"}, "plan-b": {"base_date": "20250118"}}
    merged = merge_lineage_paper_executions(executions, plan_meta)
    assert len(merged) == 2
    assert merged[0]["source_plan_base_date"] == "20250106"
    assert merged[1]["source_plan_base_date"] == "20250118"
    trades = build_lineage_paper_trades(merged, {"000001.SZ": 13.0})
    assert any(row["status"] == "closed" for row in trades)
    assert any(row["status"] == "open" for row in trades)


def test_build_lineage_paper_trades_pairs_suffix_mismatch_and_sets_sell_date():
    executions = [
        {
            "execution_id": "buy-1",
            "plan_id": "plan-a",
            "source_plan_id": "plan-a",
            "source_plan_base_date": "20250106",
            "symbol": "000001.SZ",
            "name": "Ping",
            "action": "buy",
            "quantity": 100,
            "price": 10.0,
            "execute_date": "20250107",
            "created_at": "2025-01-07T09:30:00",
        },
        {
            "execution_id": "sell-1",
            "plan_id": "plan-b",
            "source_plan_id": "plan-b",
            "source_plan_base_date": "20250120",
            "symbol": "000001",
            "name": "Ping",
            "action": "sell",
            "quantity": 40,
            "price": 12.0,
            "execute_date": "20250121",
            "created_at": "2025-01-21T09:30:00",
        },
    ]

    trades = build_lineage_paper_trades(executions, {"000001.SZ": 13.0})

    closed = next(row for row in trades if row["status"] == "closed")
    assert closed["buy_date"] == "20250107"
    assert closed["sell_date"] == "20250121"
    assert closed["quantity"] == 40
    assert closed["sell_order_id"] == "sell-1"
