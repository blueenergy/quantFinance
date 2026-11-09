"""Position guard for disabling a live strategy that still holds shares.

The strategy-manager owns the shell/state semantics; this module asks it for
the managed position and only falls back to a broker-only probe when it is
unreachable. The fallback never authorizes an automatic sell: without the
manager we cannot tell a strategy sleeve from a personal core position.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx

from auth import create_access_token
from services.live_trade_signals import CN_TZ, default_activate_after, default_valid_until


logger = logging.getLogger(__name__)

GUARD_CODE = "STRATEGY_POSITION_GUARD"
GUARD_UNAVAILABLE_CODE = "STRATEGY_POSITION_GUARD_UNAVAILABLE"
POSITION_CLEAR_UNAVAILABLE_CODE = "POSITION_CLEAR_UNAVAILABLE"
POSITION_CLEAR_FAILED_CODE = "POSITION_CLEAR_FAILED"

ACTIVE_SIGNAL_STATUSES = [
    "pending",
    "retry_pending",
    "submitted",
    "partial_filled",
    "cancel_requested",
]

_GUARD_TIMEOUT_SECONDS = 5.0


def _guard_base_url() -> Optional[str]:
    raw = (os.getenv("REMOTE_WORKER_API") or "").strip()
    if not raw:
        return None
    return raw.rstrip("/workers").rstrip("/")


def fetch_position_guard(
    current_user: dict,
    symbol: str,
    strategy_key: str,
) -> Optional[Dict[str, Any]]:
    """Ask the strategy-manager for the managed position; None when unreachable."""
    base = _guard_base_url()
    if not base:
        return None
    try:
        token = create_access_token(
            data={
                "sub": current_user.get("username"),
                "user_id": str(current_user.get("id")),
            }
        )
        response = httpx.get(
            f"{base}/configs/position-guard",
            params={"symbol": symbol, "strategy_key": strategy_key},
            headers={"Authorization": f"Bearer {token}"},
            timeout=_GUARD_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:  # noqa: BLE001 - fallback is deliberate
        logger.warning(
            "position guard unavailable for %s/%s: %s", symbol, strategy_key, exc
        )
        return None
    if not isinstance(data, dict) or not data.get("ok"):
        return None
    return data


def reset_strategy_state(
    current_user: dict,
    symbol: str,
    strategy_key: str,
) -> Optional[Dict[str, Any]]:
    """Ask the strategy-manager to zero a stopped strategy's state file.

    The manager stops the worker (if still alive) before writing, so the
    worker's final ``save_state`` cannot overwrite the reset.
    """
    base = _guard_base_url()
    if not base:
        return None
    try:
        token = create_access_token(
            data={
                "sub": current_user.get("username"),
                "user_id": str(current_user.get("id")),
            }
        )
        response = httpx.post(
            f"{base}/configs/reset-position",
            params={"symbol": symbol, "strategy_key": strategy_key},
            headers={"Authorization": f"Bearer {token}"},
            timeout=_GUARD_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:  # noqa: BLE001 - reported as a warning to the caller
        logger.warning(
            "position state reset unavailable for %s/%s: %s", symbol, strategy_key, exc
        )
        return None
    return data if isinstance(data, dict) else None


def _id_variants(value: Any) -> list[Any]:
    variants: list[Any] = [str(value)]
    try:
        from bson import ObjectId

        variants.insert(0, ObjectId(str(value)))
    except Exception:  # noqa: BLE001 - str form is still tried
        pass
    return variants


def _resolve_account(
    collections: Dict[str, Any],
    user_id: str,
    config_doc: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    accounts_col = collections.get("securities_accounts")
    if accounts_col is None:
        return None

    params = config_doc.get("params") or {}
    explicit = config_doc.get("securities_account_id") or params.get("securities_account_id")
    if explicit:
        for query_id in _id_variants(explicit):
            doc = accounts_col.find_one({"_id": query_id, "user_id": user_id})
            if doc:
                return doc
        return None

    accounts = list(accounts_col.find({"user_id": user_id}))
    if len(accounts) == 1:
        return accounts[0]
    return None


def build_fallback_guard(
    collections: Dict[str, Any],
    user_id: str,
    config_doc: Dict[str, Any],
    symbol: str,
    strategy_key: str,
) -> Dict[str, Any]:
    """Broker-only guard used when the strategy-manager is down.

    ``managed_qty`` stays ``None`` and ``can_auto_clear`` stays false: a broker
    position cannot be split into a strategy sleeve without the shell's params,
    so the only safe action is an explicit "keep".
    """
    account = _resolve_account(collections, user_id, config_doc)
    position_doc = None
    positions_col = collections.get("trader_positions")
    if account and positions_col is not None:
        position_doc = positions_col.find_one(
            {
                "user_id": user_id,
                "symbol": symbol,
                "securities_account_id": str(account["_id"]),
            },
            sort=[("updated_at", -1)],
        )

    position_doc = position_doc or {}
    broker_qty = int(position_doc.get("qty") or position_doc.get("volume") or 0)
    if position_doc and "can_use_volume" in position_doc:
        can_use = int(position_doc.get("can_use_volume") or 0)
    else:
        can_use = broker_qty

    return {
        "ok": False,
        "symbol": symbol,
        "strategy_key": strategy_key,
        "strategy_name": None,
        "enabled": bool(config_doc.get("enabled", True)),
        "account": {
            "bound": account is not None,
            "securities_account_id": str(account.get("_id")) if account else None,
            "account_id": account.get("account_id") if account else None,
            "broker": account.get("broker") if account else None,
        },
        "position": {
            "source": "fallback_broker",
            "broker_qty": broker_qty,
            "can_use_volume": can_use,
            "managed_qty": None,
            "core_qty": None,
            "avg_price": float(position_doc.get("avg_price") or 0),
            "last_price": float(position_doc.get("last_price") or 0),
            "market_value": float(position_doc.get("market_value") or 0),
            "unrealized_pnl": float(position_doc.get("unrealized_pnl") or 0),
            "unrealized_pnl_pct": float(position_doc.get("unrealized_pnl_pct") or 0),
            "entry_price": 0.0,
            "entry_date": None,
            "snapshot_at": position_doc.get("updated_at"),
        },
        "pending": {
            "buy": {"count": 0, "qty": 0, "order_ids": []},
            "sell": {"count": 0, "qty": 0, "order_ids": []},
        },
        "session": None,
        "can_auto_clear": False,
        "clear_qty": None,
        "warnings": [
            "策略管理服务不可用；无法区分策略管理量与底仓，已禁止自动清仓"
        ],
    }


def build_flatten_signal(
    *,
    user_id: str,
    symbol: str,
    account: Dict[str, Any],
    position: Dict[str, Any],
    clear_qty: int,
    strategy_key: str,
) -> Dict[str, Any]:
    """Build the live SELL that flattens the strategy's managed sleeve."""
    now = datetime.now(timezone.utc)
    activate_after = default_activate_after(now)
    valid_until = default_valid_until(now, activate_after)
    price = float(position.get("last_price") or 0) or float(position.get("avg_price") or 0)
    ts = time.time()
    reference_price = round(price, 3) if price else None

    return {
        "order_id": f"strategy-disable-{uuid.uuid4().hex[:12]}",
        "symbol": symbol,
        "action": "sell",
        "size": int(clear_qty),
        "price": reference_price,
        "order_type": "market",
        "status": "pending",
        "timestamp": ts,
        "created_at": ts,
        "mode": "live",
        "strategy": strategy_key,
        "source": "strategy_disable",
        "user_id": user_id,
        "securities_account_id": account.get("securities_account_id"),
        "broker": account.get("broker"),
        "account_id": account.get("account_id"),
        "is_executable": True,
        "retry_count": 0,
        "date": datetime.now(CN_TZ).strftime("%Y%m%d"),
        "datetime": datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "activate_after": activate_after,
        "valid_until": valid_until,
        "reference_price": reference_price,
        "created_by": user_id,
        "meta": {
            "reason": "strategy disabled; flatten managed position",
            "strategy_key": strategy_key,
            "managed_qty": position.get("managed_qty"),
            "core_qty": position.get("core_qty"),
            "broker_qty": position.get("broker_qty"),
            "can_use_volume": position.get("can_use_volume"),
            "clear_qty": int(clear_qty),
        },
    }


def find_active_disable_sell(
    collections: Dict[str, Any],
    user_id: str,
    account_id: Optional[str],
    symbol: str,
) -> Optional[Dict[str, Any]]:
    """Return an in-flight strategy-disable SELL for the same config, if any."""
    query: Dict[str, Any] = {
        "user_id": user_id,
        "symbol": symbol,
        "mode": "live",
        "action": {"$in": ["sell", "SELL"]},
        "source": "strategy_disable",
        "status": {"$in": ACTIVE_SIGNAL_STATUSES},
    }
    if account_id:
        query["securities_account_id"] = account_id
    return collections["trade_signals"].find_one(
        query, sort=[("created_at", -1)]
    )


def cancel_active_buys(
    collections: Dict[str, Any],
    user_id: str,
    account_id: Optional[str],
    symbol: str,
) -> Dict[str, int]:
    """Cancel same-symbol BUY signals that could fill after the stop.

    Pending signals are marked cancelled; already-submitted ones are marked
    ``cancel_requested`` so quantTrader asks QMT to cancel. Portfolio-plan
    buys are left untouched.
    """
    query: Dict[str, Any] = {
        "user_id": user_id,
        "symbol": symbol,
        "mode": "live",
        "status": {"$in": ["pending", "retry_pending", "submitted", "partial_filled"]},
        "action": {"$in": ["buy", "BUY"]},
        "source": {"$ne": "portfolio_plan"},
    }
    if account_id:
        query["securities_account_id"] = account_id

    now = time.time()
    cancelled = 0
    cancel_requested = 0
    for row in collections["trade_signals"].find(query):
        order_id = row.get("order_id")
        if not order_id:
            continue
        if str(row.get("status") or "") in {"pending", "retry_pending"}:
            update = {
                "$set": {
                    "status": "cancelled",
                    "cancelled_at": now,
                    "updated_at": now,
                    "cancel_reason": "strategy_disable",
                }
            }
            cancelled += 1
        else:
            update = {
                "$set": {
                    "status": "cancel_requested",
                    "cancel_requested_at": now,
                    "updated_at": now,
                    "cancel_reason": "strategy_disable",
                }
            }
            cancel_requested += 1
        collections["trade_signals"].update_one(
            {"user_id": user_id, "order_id": order_id}, update
        )
    return {"cancelled": cancelled, "cancel_requested": cancel_requested}
