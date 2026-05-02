"""用户订阅 theme_lag_candidate_validation 更新：写入 finance.theme_lag_validation_subscriptions。"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_active_user
from db import get_db_collections
from models.all_models import ThemeLagValidationSubscriptionUpsert
from utils.stock_utils import serialize_doc

log = logging.getLogger(__name__)

router = APIRouter()

# 与 quant_data index_store_key 常见取值对齐（小写、字母数字与 _ . -）
_INDEX_STORE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$", re.IGNORECASE)


def _user_id(current_user: dict) -> str:
    return str(current_user["id"])


def _normalize_index_store_key(raw: str) -> str:
    s = (raw or "").strip().lower()
    if not _INDEX_STORE_KEY_RE.match(s):
        raise HTTPException(
            status_code=400,
            detail="index_store_key 无效：仅允许 1–64 位小写字母数字及 _ . -，且须以小写字母或数字开头",
        )
    return s


def _serialize_subscription(doc: Dict[str, Any]) -> Dict[str, Any]:
    out = serialize_doc(doc)
    if "_id" in out:
        out["id"] = out.pop("_id")
    return out


@router.get("/theme-lag-validation-subscriptions", summary="列出当前用户的验证订阅")
def list_theme_lag_validation_subscriptions(
    current_user: dict = Depends(get_current_active_user),
):
    collections = get_db_collections()
    coll = collections["theme_lag_validation_subscriptions"]
    uid = _user_id(current_user)
    cursor = coll.find({"user_id": uid}).sort("index_store_key", 1)
    items: List[Dict[str, Any]] = [_serialize_subscription(d) for d in cursor]
    return {"success": True, "data": {"items": items}}


@router.post("/theme-lag-validation-subscriptions", summary="订阅或更新验证通知")
def upsert_theme_lag_validation_subscription(
    body: ThemeLagValidationSubscriptionUpsert,
    current_user: dict = Depends(get_current_active_user),
):
    collections = get_db_collections()
    coll = collections["theme_lag_validation_subscriptions"]
    uid = _user_id(current_user)
    sk = _normalize_index_store_key(body.index_store_key)
    now = datetime.now(timezone.utc)

    coll.update_one(
        {"user_id": uid, "index_store_key": sk},
        {
            "$set": {
                "enabled": body.enabled,
                "updated_at": now,
            },
            "$setOnInsert": {
                "user_id": uid,
                "index_store_key": sk,
                "created_at": now,
            },
        },
        upsert=True,
    )
    doc = coll.find_one({"user_id": uid, "index_store_key": sk})
    log.info("theme_lag_validation_subscriptions upsert user_id=%s index_store_key=%s enabled=%s", uid, sk, body.enabled)
    return {"success": True, "data": _serialize_subscription(doc) if doc else {}}


@router.delete(
    "/theme-lag-validation-subscriptions/{index_store_key}",
    summary="退订指定指数的验证通知",
)
def delete_theme_lag_validation_subscription(
    index_store_key: str,
    current_user: dict = Depends(get_current_active_user),
):
    collections = get_db_collections()
    coll = collections["theme_lag_validation_subscriptions"]
    uid = _user_id(current_user)
    sk = _normalize_index_store_key(index_store_key)
    result = coll.delete_one({"user_id": uid, "index_store_key": sk})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="未找到该订阅")
    log.info("theme_lag_validation_subscriptions delete user_id=%s index_store_key=%s", uid, sk)
    return {"success": True}
