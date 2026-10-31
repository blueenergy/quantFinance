import time

from services.portfolio_snapshot import build_portfolio_snapshot


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
