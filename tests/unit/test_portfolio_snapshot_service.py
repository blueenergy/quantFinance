import asyncio
import time

import pytest
from bson import ObjectId
from fastapi import HTTPException

from routers import hermes_quant_tools as h
from services.portfolio_snapshot import (
    build_all_portfolio_snapshots,
    build_portfolio_snapshot,
)


class _Cursor(list):
    def sort(self, field, direction=-1):
        return _Cursor(sorted(self, key=lambda row: row.get(field) or "", reverse=direction == -1))

    def limit(self, n):
        return _Cursor(self[:n])


class _Collection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    def find(self, query=None, projection=None):
        rows = [dict(doc) for doc in self.docs if self._matches(doc, query or {})]
        if projection:
            include_keys = {key for key, value in projection.items() if value}
            if include_keys:
                rows = [{key: row.get(key) for key in include_keys if key in row} for row in rows]
        return _Cursor(rows)

    def find_one(self, query=None, projection=None, sort=None):
        rows = self.find(query, projection)
        if sort:
            field, direction = sort[0]
            rows = rows.sort(field, direction)
        return rows[0] if rows else None

    def _matches(self, doc, query):
        for key, expected in query.items():
            value = doc.get(key)
            if isinstance(expected, dict):
                if "$in" in expected and value not in expected["$in"]:
                    return False
                continue
            if value != expected:
                return False
        return True


def _now():
    return time.time()


def _account(**overrides):
    row = {
        "user_id": "user-1",
        "securities_account_id": "sec-1",
        "account_id": "acct-1",
        "broker": "国金证券",
        "account_type": "stock",
        "live_trading_enabled": True,
        "total_asset": 300.0,
        "market_value": 200.0,
        "cash": 100.0,
        "available_cash": 90.0,
        "synced_at": _now() - 60,
    }
    row.update(overrides)
    return row


def _position(symbol, qty, avg, last, mv, **overrides):
    row = {
        "user_id": "user-1",
        "securities_account_id": "sec-1",
        "symbol": symbol,
        "qty": qty,
        "can_use_volume": qty,
        "avg_price": avg,
        "last_price": last,
        "market_value": mv,
    }
    row.update(overrides)
    return row


def _equity(date, total, **overrides):
    row = {
        "user_id": "user-1",
        "securities_account_id": "sec-1",
        "date": date,
        "total_asset": total,
        "market_value": total - 100,
        "cash": 100.0,
        "available_cash": 100.0,
        "updated_at": None,
        "source_synced_at": _now() - 60,
        "source_synced_age_seconds": 60,
        "is_stale": False,
    }
    row.update(overrides)
    return row


def _data(positions, **overrides):
    data = {
        "stock_info": _Collection(
            [
                {"symbol": "600114.SH", "name": "东睦股份", "industry": "机械基件", "sw_l1": "机械设备"},
                {"symbol": "000001.SZ", "name": "平安银行", "industry": "银行", "sw_l1": "银行"},
            ]
        ),
        "volume_price": _Collection([{"trade_date": "20261030"}]),
    }
    data.update(overrides)
    return data


def test_build_snapshot_computes_weights_pnl_and_data_as_of():
    finance = {
        "trader_accounts": _Collection([_account()]),
        "trader_positions": _Collection(
            [
                _position("600114.SH", 10, 10.0, 8.0, 80.0, unrealized_pnl=-21.0, unrealized_pnl_pct=-21.0),
                _position("000001.SZ", 20, 5.0, 6.0, 120.0),
            ]
        ),
        "trader_account_equity": _Collection(
            [_equity("20261029", 290.0), _equity("20261030", 300.0)]
        ),
    }

    snap = build_portfolio_snapshot("user-1", finance, _data(finance["trader_positions"].docs))

    assert snap["success"] is True
    assert snap["data_as_of"] == "20261030"
    assert snap["account"]["total_asset"] == 300.0
    assert snap["account"]["total_asset_basis"] == "source"
    assert snap["data_quality"]["positions_count"] == 2
    assert snap["data_quality"]["position_weight_sum"] == 1.0
    assert snap["data_quality"]["equity_vs_account_diff"] == 0.0

    by_symbol = {row["symbol"]: row for row in snap["positions"]}
    assert by_symbol["000001.SZ"]["weight"] == 0.6
    assert by_symbol["600114.SH"]["weight"] == 0.4
    assert by_symbol["000001.SZ"]["weight_of_total_asset"] == 0.4
    assert by_symbol["600114.SH"]["unrealized_pnl"] == -21.0
    assert by_symbol["600114.SH"]["unrealized_pnl_pct"] == -21.0
    assert by_symbol["600114.SH"]["computed_unrealized_pnl"] == -20.0
    assert by_symbol["600114.SH"]["computed_unrealized_pnl_pct"] == -20.0
    assert by_symbol["000001.SZ"]["unrealized_pnl"] == 20.0
    assert by_symbol["000001.SZ"]["unrealized_pnl_pct"] == 20.0
    assert by_symbol["600114.SH"]["name"] == "东睦股份"


def test_build_snapshot_computes_total_and_flags_equity_diff():
    finance = {
        "trader_accounts": _Collection([_account(total_asset=0.0, market_value=900.0, cash=100.0)]),
        "trader_positions": _Collection([_position("600114.SH", 100, 9.0, 9.0, 900.0)]),
        "trader_account_equity": _Collection([_equity("20261030", 800.0)]),
    }

    snap = build_portfolio_snapshot("user-1", finance, _data(finance["trader_positions"].docs))

    assert snap["account"]["total_asset"] == 1000.0
    assert snap["account"]["total_asset_basis"] == "market_value_plus_cash"
    assert snap["data_quality"]["equity_vs_account_diff"] == -200.0
    assert snap["data_quality"]["equity_vs_account_diff_pct"] == -20.0
    warnings = " ".join(snap["data_quality"]["warnings"])
    assert "total_asset is 0" in warnings
    assert "differs from the account snapshot" in warnings


def test_build_snapshot_flags_stale_account_and_missing_info():
    finance = {
        "trader_accounts": _Collection([_account(synced_at=_now() - 7 * 3600)]),
        "trader_positions": _Collection([_position("600114.SH", 10, 10.0, 8.0, 80.0)]),
        "trader_account_equity": _Collection(
            [_equity("20261030", 300.0, is_stale=True)]
        ),
    }
    data = {
        "stock_info": _Collection([]),
        "volume_price": _Collection([]),
    }

    snap = build_portfolio_snapshot("user-1", finance, data)

    assert snap["account"]["synced_is_stale"] is True
    assert snap["data_quality"]["missing_stock_info"] == ["600114.SH"]
    warnings = " ".join(snap["data_quality"]["warnings"])
    assert "account synced" in warnings
    assert "missing stock_info" in warnings
    assert "is_stale=true" in warnings
    assert snap["data_as_of"] == "20261030"
    assert snap["positions"][0]["name"] == ""


def test_build_snapshot_resolves_etf_name_from_fund_table():
    finance = {
        "trader_accounts": _Collection([_account(total_asset=100.0, market_value=100.0, cash=0.0)]),
        "trader_positions": _Collection(
            [_position("510300.SH", 100, 4.0, 1.0, 100.0)]
        ),
        "trader_account_equity": _Collection([_equity("20261030", 100.0)]),
    }
    data = {
        "stock_info": _Collection([]),
        "etf_basic": _Collection([{"ts_code": "510300.SH", "name": "华泰柏瑞沪深300ETF"}]),
        "volume_price": _Collection([{"trade_date": "20261030"}]),
    }

    snap = build_portfolio_snapshot("user-1", finance, data)

    assert snap["positions"][0]["name"] == "华泰柏瑞沪深300ETF"
    assert snap["data_quality"]["missing_stock_info"] == []


def test_build_snapshot_selects_requested_account():
    finance = {
        "trader_accounts": _Collection(
            [
                _account(securities_account_id="sec-1", account_id="acct-1", synced_at=_now() - 60),
                _account(securities_account_id="sec-2", account_id="acct-2", synced_at=_now() - 30),
            ]
        ),
        "trader_positions": _Collection(
            [_position("600114.SH", 10, 10.0, 8.0, 80.0, securities_account_id="sec-2")]
        ),
        "trader_account_equity": _Collection(
            [_equity("20261030", 300.0, securities_account_id="sec-2")]
        ),
    }

    snap = build_portfolio_snapshot(
        "user-1",
        finance,
        _data(finance["trader_positions"].docs),
        securities_account_id="sec-2",
    )

    assert snap["account"]["account_id"] == "acct-2"
    assert snap["data_quality"]["positions_count"] == 1


UID_PERSONAL = "6a5122cb2703714b7ba223bf"
UID_SYSTEM = "6a563b1b433b707247eca2d0"


def _two_account_finance():
    finance = {
        "users": _Collection(
            [
                {"_id": ObjectId(UID_PERSONAL), "username": "shuyolin", "is_admin": True},
                {"_id": ObjectId(UID_SYSTEM), "username": "blue_energy", "is_admin": False},
            ]
        ),
        "trader_accounts": _Collection(
            [
                _account(
                    user_id=UID_PERSONAL,
                    securities_account_id="sec-1",
                    account_id="8885156549",
                    total_asset=300.0,
                    market_value=200.0,
                    cash=100.0,
                ),
                _account(
                    user_id=UID_SYSTEM,
                    securities_account_id="sec-2",
                    account_id="62129066",
                    total_asset=500.0,
                    market_value=400.0,
                    cash=100.0,
                ),
            ]
        ),
        "trader_positions": _Collection(
            [
                _position(
                    "600114.SH", 10, 10.0, 8.0, 80.0,
                    user_id=UID_PERSONAL, securities_account_id="sec-1",
                ),
                _position(
                    "600114.SH", 20, 10.0, 8.0, 160.0,
                    user_id=UID_SYSTEM, securities_account_id="sec-2",
                ),
            ]
        ),
        "trader_account_equity": _Collection(
            [
                _equity("20261030", 300.0, user_id=UID_PERSONAL, securities_account_id="sec-1"),
                _equity("20261030", 500.0, user_id=UID_SYSTEM, securities_account_id="sec-2"),
            ]
        ),
    }
    data = {
        "stock_info": _Collection(
            [
                {"symbol": "600114.SH", "name": "东睦股份", "industry": "机械基件", "sw_l1": "机械设备"},
            ]
        ),
        "volume_price": _Collection([{"trade_date": "20261030"}]),
    }
    return finance, data


def test_build_all_snapshots_returns_each_account_with_username():
    finance, data = _two_account_finance()

    result = build_all_portfolio_snapshots(finance, data)

    assert result["success"] is True
    assert result["all_accounts"] is True
    assert result["totals"] == {
        "accounts": 2,
        "total_asset": 800.0,
        "market_value": 600.0,
        "cash": 200.0,
    }
    by_account = {item["account"]["account_id"]: item for item in result["accounts"]}
    assert set(by_account) == {"8885156549", "62129066"}
    assert by_account["8885156549"]["username"] == "shuyolin"
    assert by_account["8885156549"]["is_admin"] is True
    assert by_account["62129066"]["username"] == "blue_energy"
    assert by_account["62129066"]["is_admin"] is False
    assert len(by_account["8885156549"]["positions"]) == 1
    assert len(by_account["62129066"]["positions"]) == 1


def test_portfolio_all_accounts_rejects_non_admin(monkeypatch):
    finance, data = _two_account_finance()
    monkeypatch.setattr(h, "get_db_collections", lambda: finance)
    monkeypatch.setattr(h, "get_data_db", lambda: data)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            h.user_portfolio_snapshot(
                all_accounts=True,
                equity_limit=20,
                user={"id": UID_SYSTEM, "is_admin": False},
                _=True,
            )
        )

    assert exc.value.status_code == 403


def test_portfolio_all_accounts_returns_both_for_admin(monkeypatch):
    finance, data = _two_account_finance()
    monkeypatch.setattr(h, "get_db_collections", lambda: finance)
    monkeypatch.setattr(h, "get_data_db", lambda: data)

    result = asyncio.run(
        h.user_portfolio_snapshot(
            all_accounts=True,
            equity_limit=20,
            user={"id": UID_PERSONAL, "is_admin": True},
            _=True,
        )
    )

    assert result["success"] is True
    assert result["totals"]["accounts"] == 2
    assert {item["account"]["account_id"] for item in result["accounts"]} == {
        "8885156549",
        "62129066",
    }


def test_portfolio_single_account_shape_unchanged(monkeypatch):
    finance, data = _two_account_finance()
    monkeypatch.setattr(h, "get_db_collections", lambda: finance)
    monkeypatch.setattr(h, "get_data_db", lambda: data)

    result = asyncio.run(
        h.user_portfolio_snapshot(
            securities_account_id=None,
            all_accounts=False,
            equity_limit=20,
            user={"id": UID_PERSONAL, "is_admin": True},
            _=True,
        )
    )

    assert result["success"] is True
    assert result["account"]["account_id"] == "8885156549"
    assert "accounts" not in result
