"""End-to-end readiness for a securities account's live trading.

``live_trading_enabled`` is only a permission bit.  A user can enable it before
the admin has configured the executor mapping, or while the QMT strategy /
container is down, and live signals would then queue forever.  This module
combines the permission, the mapping and fresh heartbeats into one status that
both the API and the dashboard can show and enforce.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from fastapi import HTTPException

READY_STALE_SECONDS = 180


def _age_seconds(value: Any, now_ts: float) -> Optional[float]:
    if isinstance(value, (int, float)):
        return max(0.0, now_ts - float(value))
    return None


def _latest_row(collection: Any, query: Dict[str, Any]) -> Dict[str, Any]:
    """Latest trader row; tolerant of limited test/legacy collection fakes."""
    try:
        return collection.find_one(query, sort=[("synced_at", -1)]) or {}
    except TypeError:
        pass
    try:
        rows = list(collection.find(query))
    except Exception:
        return collection.find_one(query) or {}
    if not rows:
        return {}
    return max(rows, key=lambda row: row.get("synced_at") or 0)


def account_trading_readiness(
    collections: Dict[str, Any],
    account: Dict[str, Any],
    *,
    now_ts: Optional[float] = None,
    stale_seconds: int = READY_STALE_SECONDS,
) -> Dict[str, Any]:
    """Return readiness fields for one ``securities_accounts`` document."""
    now = now_ts if now_ts is not None else time.time()
    user_id = str(account.get("user_id") or "")
    sec_id = str(account.get("_id") or account.get("securities_account_id") or "")

    live = bool(account.get("live_trading_enabled"))
    mapping_complete = bool(
        account.get("qmt_account_id")
        and account.get("qmt_mailbox_root")
        and account.get("trader_client_id")
    )

    worker_coll = collections.get("worker_status")
    account_coll = collections.get("trader_accounts")
    if worker_coll is None or account_coll is None:
        # Limited collection maps (tests / old deployments): do not gate.
        return {
            "trading_ready": True,
            "trading_status": "unknown",
            "trading_status_reason": "",
            "mapping_complete": mapping_complete,
            "instance_state": "unknown",
            "worker_seen_at": None,
            "account_synced_at": None,
            "trading_ready_age_seconds": None,
        }

    worker = worker_coll.find_one(
        {"user_id": user_id, "worker_id": f"quantTrader:{sec_id}"}
    ) or {}
    worker_age = _age_seconds(worker.get("last_seen_at"), now)

    account_row = _latest_row(
        account_coll, {"user_id": user_id, "securities_account_id": sec_id}
    )
    synced_at = account_row.get("synced_at")
    sync_age = _age_seconds(synced_at, now)

    instance_ok = worker_age is not None and worker_age <= stale_seconds
    sync_ok = sync_age is not None and sync_age <= stale_seconds

    if not live:
        status = "not_live"
        reason = "未开启实盘交易信号"
    elif not mapping_complete:
        status = "no_mapping"
        reason = "执行端点映射不完整（QMT 账户号 / mailbox / client_id）"
    elif not instance_ok:
        status = "instance_down"
        reason = "执行实例未运行或心跳过期"
    elif not sync_ok:
        status = "sync_stale"
        reason = "账户同步过期（QMT 策略可能未运行）"
    else:
        status = "ready"
        reason = ""

    ready = status == "ready"
    return {
        "trading_ready": ready,
        "trading_status": status,
        "trading_status_reason": reason,
        "mapping_complete": mapping_complete,
        "instance_state": "running" if instance_ok else ("stale" if worker_age is not None else "missing"),
        "worker_seen_at": worker.get("last_seen_at"),
        "account_synced_at": synced_at,
        "trading_ready_age_seconds": None if sync_age is None else round(sync_age, 1),
    }


def require_trading_ready(collections: Dict[str, Any], account: Dict[str, Any]) -> Dict[str, Any]:
    """Raise 400 when the account cannot actually execute live signals."""
    readiness = account_trading_readiness(collections, account)
    if not readiness["trading_ready"]:
        raise HTTPException(
            status_code=400,
            detail=f"证券账户未就绪：{readiness['trading_status_reason']}",
        )
    return readiness
