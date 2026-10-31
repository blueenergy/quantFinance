"""Read-only portfolio snapshot for MCP / post-close review workflows.

The snapshot intentionally uses the same production collections as the
existing portfolio analysis path (``trader_accounts`` / ``trader_positions`` /
``trader_account_equity``) so an agent sees exactly what the account-level
services see. It does not write anything.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
ACCOUNT_SNAPSHOT_STALE_SECONDS = 6 * 60 * 60


def _numeric(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _iso_utc(value: Any) -> Optional[str]:
    """Render an epoch/datetime as an explicit UTC ISO string."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _iso_cst(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).astimezone(SHANGHAI_TZ).isoformat()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(SHANGHAI_TZ).isoformat()
    return str(value)


def _age_seconds(value: Any, *, now_ts: Optional[float] = None) -> Optional[float]:
    ts = None
    if isinstance(value, (int, float)):
        ts = float(value)
    elif isinstance(value, datetime):
        ts = value.replace(tzinfo=value.tzinfo or timezone.utc).timestamp()
    if ts is None:
        return None
    return round((now_ts if now_ts is not None else time.time()) - ts, 2)


def _latest_trade_date(data_collections: Dict[str, Any]) -> Optional[str]:
    try:
        row = data_collections["volume_price"].find_one(
            {},
            projection={"trade_date": 1},
            sort=[("trade_date", -1)],
        )
        if row and row.get("trade_date"):
            return str(row["trade_date"])
    except Exception:
        pass
    return None


def _select_account(
    finance_collections: Dict[str, Any],
    user_id: str,
    securities_account_id: Optional[str],
) -> Dict[str, Any]:
    query: Dict[str, Any] = {"user_id": user_id}
    if securities_account_id:
        query["securities_account_id"] = str(securities_account_id)
    account = finance_collections["trader_accounts"].find_one(query, sort=[("synced_at", -1)])
    if account is None and securities_account_id:
        account = finance_collections["trader_accounts"].find_one(
            {"user_id": user_id, "account_id": str(securities_account_id)},
            sort=[("synced_at", -1)],
        )
    return account or {}


def _positions(
    finance_collections: Dict[str, Any],
    user_id: str,
    account: Dict[str, Any],
) -> List[Dict[str, Any]]:
    sec_id = account.get("securities_account_id")
    query: Dict[str, Any] = {"user_id": user_id}
    if sec_id:
        query["securities_account_id"] = str(sec_id)
    rows = list(finance_collections["trader_positions"].find(query))
    if not rows and sec_id:
        rows = list(finance_collections["trader_positions"].find({"user_id": user_id}))
    return rows


def _equity_rows(
    finance_collections: Dict[str, Any],
    user_id: str,
    account: Dict[str, Any],
    limit: int,
) -> List[Dict[str, Any]]:
    sec_id = account.get("securities_account_id")
    query: Dict[str, Any] = {"user_id": user_id}
    if sec_id:
        query["securities_account_id"] = str(sec_id)
    cursor = finance_collections["trader_account_equity"].find(query).sort("date", -1).limit(limit)
    rows = list(cursor)
    if not rows and sec_id:
        cursor = finance_collections["trader_account_equity"].find({"user_id": user_id}).sort("date", -1).limit(limit)
        rows = list(cursor)
    return list(reversed(rows))


def _stock_info_map(data_collections: Dict[str, Any], symbols: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    symbols = [s for s in symbols if s]
    if not symbols:
        return {}
    rows = data_collections["stock_info"].find(
        {"symbol": {"$in": symbols}},
        {"symbol": 1, "name": 1, "industry": 1, "sw_l1": 1},
    )
    info = {str(row.get("symbol")): row for row in rows if row.get("symbol")}

    # ETFs are usually absent from stock_info; resolve their names from the
    # fund tables so a portfolio snapshot does not report an empty name.
    missing = [symbol for symbol in symbols if symbol not in info]
    for collection_name in ("etf_basic", "fund_basic"):
        if not missing:
            break
        try:
            collection = data_collections[collection_name]
        except Exception:
            continue
        try:
            fund_rows = collection.find(
                {"ts_code": {"$in": missing}},
                {"ts_code": 1, "name": 1},
            )
        except Exception:
            continue
        for row in fund_rows:
            code = str(row.get("ts_code") or "")
            if code in missing and row.get("name"):
                info[code] = {
                    "symbol": code,
                    "name": row.get("name"),
                    "industry": "",
                    "sw_l1": "",
                }
        missing = [symbol for symbol in missing if symbol not in info]
    return info


def build_portfolio_snapshot(
    user_id: str,
    finance_collections: Dict[str, Any],
    data_collections: Dict[str, Any],
    *,
    securities_account_id: Optional[str] = None,
    equity_limit: int = 20,
) -> Dict[str, Any]:
    """Build one read-only account/positions/equity snapshot.

    ``finance_collections`` is the dict returned by ``get_db_collections()``;
    ``data_collections`` is ``get_data_db()`` (or an equivalent mapping).
    """
    limit = max(1, min(int(equity_limit or 20), 120))
    now_ts = time.time()
    account = _select_account(finance_collections, user_id, securities_account_id)
    if not account:
        return {"success": False, "error": "未找到账户数据", "user_id": user_id}

    sec_account: Dict[str, Any] = {}
    sec_id = account.get("securities_account_id")
    if sec_id:
        try:
            sec_account = (
                finance_collections["securities_accounts"].find_one(
                    {"user_id": user_id, "securities_account_id": str(sec_id)}
                )
                or {}
            )
        except Exception:
            sec_account = {}

    positions = _positions(finance_collections, user_id, account)
    symbol_list = [str(row.get("symbol")) for row in positions if row.get("symbol")]
    info_map = _stock_info_map(data_collections, symbol_list)

    market_value = _numeric(account.get("market_value"))
    cash = _numeric(account.get("cash"))
    total_asset_source = _numeric(account.get("total_asset", account.get("total_assets")))
    total_asset = total_asset_source or (market_value + cash)
    total_asset_basis = "source" if total_asset_source else "market_value_plus_cash"

    def _position_market_value(row: Dict[str, Any]) -> float:
        qty = _numeric(row.get("qty")) or _numeric(row.get("can_use_volume"))
        last_price = _numeric(row.get("last_price"))
        return _numeric(row.get("market_value")) or (qty * last_price)

    position_mv_sum = sum(_position_market_value(row) for row in positions)

    position_rows: List[Dict[str, Any]] = []
    missing_info: List[str] = []
    for row in positions:
        symbol = str(row.get("symbol") or "")
        info = info_map.get(symbol, {})
        if symbol and not info:
            missing_info.append(symbol)
        qty = _numeric(row.get("qty")) or _numeric(row.get("can_use_volume"))
        avg_price = _numeric(row.get("avg_price"))
        last_price = _numeric(row.get("last_price"))
        mv = _position_market_value(row)
        pnl = (last_price - avg_price) * qty if avg_price and last_price else None
        pnl_pct = ((last_price / avg_price) - 1.0) * 100.0 if avg_price and last_price else None
        stored_pnl = row.get("unrealized_pnl")
        stored_pnl_pct = row.get("unrealized_pnl_pct")
        position_rows.append(
            {
                "symbol": symbol,
                "name": info.get("name") or row.get("stock_name") or row.get("name") or "",
                "industry": info.get("industry") or "",
                "sw_l1": info.get("sw_l1") or "",
                "qty": qty,
                "can_use_volume": _numeric(row.get("can_use_volume")),
                "avg_price": round(avg_price, 4) if avg_price else None,
                "last_price": round(last_price, 4) if last_price else None,
                "market_value": round(mv, 2),
                "weight": round(mv / position_mv_sum, 4) if position_mv_sum else None,
                "weight_of_total_asset": round(mv / total_asset, 4) if total_asset else None,
                "unrealized_pnl": (
                    round(_numeric(stored_pnl), 2)
                    if stored_pnl is not None
                    else (round(pnl, 2) if pnl is not None else None)
                ),
                "unrealized_pnl_pct": (
                    round(_numeric(stored_pnl_pct), 2)
                    if stored_pnl_pct is not None
                    else (round(pnl_pct, 2) if pnl_pct is not None else None)
                ),
                "computed_unrealized_pnl": round(pnl, 2) if pnl is not None else None,
                "computed_unrealized_pnl_pct": round(pnl_pct, 2) if pnl_pct is not None else None,
            }
        )
    position_rows.sort(key=lambda item: item["market_value"], reverse=True)

    equity_rows = _equity_rows(finance_collections, user_id, account, limit)
    equity_history: List[Dict[str, Any]] = []
    for row in equity_rows:
        equity_history.append(
            {
                "date": str(row.get("date") or ""),
                "total_asset": _numeric(row.get("total_asset")),
                "market_value": _numeric(row.get("market_value")),
                "cash": _numeric(row.get("cash")),
                "available_cash": _numeric(row.get("available_cash")),
                "updated_at_utc": _iso_utc(row.get("updated_at")),
                "updated_at_cst": _iso_cst(row.get("updated_at")),
                "source_synced_at_utc": _iso_utc(row.get("source_synced_at")),
                "source_synced_at_cst": _iso_cst(row.get("source_synced_at")),
                "source_synced_age_seconds": row.get("source_synced_age_seconds"),
                "is_stale": row.get("is_stale"),
            }
        )

    account_synced_at = account.get("synced_at")
    account_sync_age = _age_seconds(account_synced_at, now_ts=now_ts)
    account_sync_is_stale = bool(
        account_sync_age is None or account_sync_age > ACCOUNT_SNAPSHOT_STALE_SECONDS
    )

    latest_equity = equity_rows[-1] if equity_rows else {}
    latest_equity_total = _numeric(latest_equity.get("total_asset"))
    equity_diff = round(latest_equity_total - total_asset, 2) if latest_equity else None
    equity_diff_pct = (
        round((equity_diff / total_asset) * 100.0, 3) if equity_diff is not None and total_asset else None
    )

    warnings: List[str] = []
    if total_asset_source == 0:
        warnings.append("trader_accounts.total_asset is 0; total_asset computed as market_value + cash")
    if account_sync_is_stale:
        warnings.append(f"account synced {account_sync_age}s ago; treat prices as stale")
    if equity_diff is not None and total_asset and abs(equity_diff_pct or 0) > 0.5:
        warnings.append(
            "latest trader_account_equity total differs from the account snapshot by "
            f"{equity_diff} ({equity_diff_pct}%); the equity row may be an EOD initial mark "
            "taken before the final broker sync"
        )
    if latest_equity.get("is_stale"):
        warnings.append("latest trader_account_equity row is marked is_stale=true")
    if missing_info:
        warnings.append(f"missing stock_info for {len(missing_info)} symbols: {', '.join(missing_info[:5])}")

    data_as_of = _latest_trade_date(data_collections) or str(latest_equity.get("date") or "")

    return {
        "success": True,
        "snapshot_version": "v0.2",
        "user_id": user_id,
        "data_as_of": data_as_of,
        "generated_at": datetime.now(SHANGHAI_TZ).isoformat(),
        "account": {
            "user_id": user_id,
            "account_id": account.get("account_id"),
            "securities_account_id": account.get("securities_account_id"),
            "broker": account.get("broker") or sec_account.get("broker"),
            "account_type": account.get("account_type") or sec_account.get("account_type"),
            "live_trading_enabled": sec_account.get("live_trading_enabled"),
            "risk_limits": sec_account.get("risk_limits"),
            "total_asset": round(total_asset, 2),
            "total_asset_basis": total_asset_basis,
            "market_value": round(market_value, 2),
            "cash": round(cash, 2),
            "available_cash": round(_numeric(account.get("available_cash")), 2),
            "pnl": _numeric(account.get("pnl")),
            "pnl_ratio": _numeric(account.get("pnl_ratio")),
            "synced_at_utc": _iso_utc(account_synced_at),
            "synced_at_cst": _iso_cst(account_synced_at),
            "synced_age_seconds": account_sync_age,
            "synced_is_stale": account_sync_is_stale,
        },
        "positions": position_rows,
        "equity_history": equity_history,
        "data_quality": {
            "positions_count": len(position_rows),
            "position_market_value_sum": round(position_mv_sum, 2),
            "position_weight_sum": round(
                sum(item["weight"] or 0.0 for item in position_rows), 6
            ),
            "missing_stock_info": missing_info,
            "latest_equity_date": str(latest_equity.get("date") or ""),
            "latest_equity_total": latest_equity_total if latest_equity else None,
            "equity_vs_account_diff": equity_diff,
            "equity_vs_account_diff_pct": equity_diff_pct,
            "warnings": warnings,
        },
        "source": {
            "account": "finance.trader_accounts",
            "positions": "finance.trader_positions",
            "equity": "finance.trader_account_equity",
            "names": "quant_data.stock_info",
            "trade_date": "quant_data.volume_price",
        },
    }
