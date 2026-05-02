"""主题补涨验证订阅：扫描 quant_data 验证文档，向订阅用户写入 notifications；邮件为可选项。

订阅文档（finance.theme_lag_validation_subscriptions）建议字段：
- user_id: str（与 JWT current_user['id'] 一致）
- index_store_key: str（与验证文档一致，如 csi1000）
- enabled: bool，默认 true
- created_at / updated_at: **UTC 的 BSON datetime**（库内可读；API 经 serialize_doc 为 ISO 8601 字符串）
- last_notified_eval_utc_ts: float，上次已通知的 eval_completed_at（UTC epoch）；缺省视为 0

纸面 trade_executions 由 quant-data-engine 在验证落库时按订阅用户写入；本任务仅负责站内通知 + 可选邮件。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict

log = logging.getLogger(__name__)


def _eval_completed_at_to_utc_ts(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return 0.0


def _ensure_subscription_indexes(subs_coll) -> None:
    try:
        subs_coll.create_index(
            [("user_id", 1), ("index_store_key", 1)],
            name="tlvs_user_index_unique",
            unique=True,
            background=True,
        )
    except Exception:
        pass


def _email_enabled_for_user(collections: Dict[str, Any], user_id: str) -> bool:
    settings_coll = collections["notification_settings"]
    row = settings_coll.find_one({"user_id": user_id})
    if not row:
        return False
    return bool(row.get("email_notifications_enabled"))


def _send_optional_theme_lag_email(
    collections: Dict[str, Any],
    user_id: str,
    subject: str,
    text_body: str,
    html_body: str,
) -> None:
    if not _email_enabled_for_user(collections, user_id):
        return
    users_coll = collections["users"]
    from bson import ObjectId

    user = None
    try:
        user = users_coll.find_one({"_id": ObjectId(user_id)})
    except Exception:
        user = users_coll.find_one({"_id": user_id})
    if not user:
        user = users_coll.find_one({"id": user_id})
    if not user or not user.get("email"):
        return
    from services.email_service import email_service

    ok = email_service.send_email(
        to_email=user["email"],
        subject=subject,
        html_body=html_body,
        text_body=text_body,
    )
    if ok:
        log.info("theme_lag_validation: optional email sent to user_id=%s", user_id)
    else:
        log.warning("theme_lag_validation: email skipped or failed for user_id=%s", user_id)


def run_theme_lag_validation_subscription_scan(job_id: str) -> None:
    from db import get_db_collections, get_data_db

    try:
        collections = get_db_collections()
        data = get_data_db()
        subs_coll = collections["theme_lag_validation_subscriptions"]
        val_coll = data["theme_lag_candidate_validation"]
        notifications_coll = collections["notifications"]

        _ensure_subscription_indexes(subs_coll)

        cursor = subs_coll.find({"enabled": {"$ne": False}})
        now_ts = time.time()
        now_dt = datetime.now(timezone.utc)
        for sub in cursor:
            uid = sub.get("user_id")
            if uid is None:
                continue
            user_id = str(uid)
            sk = str(sub.get("index_store_key", "")).strip()
            if not sk:
                continue

            latest = val_coll.find_one(
                {"index_store_key": sk},
                sort=[("anchor_trade_date", -1)],
            )
            if not latest:
                continue

            ev_ts = _eval_completed_at_to_utc_ts(latest.get("eval_completed_at"))
            if ev_ts <= 0:
                continue

            last = float(sub.get("last_notified_eval_utc_ts") or 0)
            if ev_ts <= last:
                continue

            anchor = latest.get("anchor_trade_date", "")
            run_status = latest.get("run_status", "")
            agg = latest.get("aggregate") or {}
            title = "主题补涨验证已更新"
            message = (
                f"指数 {sk} 锚定日 {anchor} 的验证结果已更新（run_status={run_status}）。"
                "可在主题补涨相关页面查看详情。"
            )
            notification: Dict[str, Any] = {
                "user_id": user_id,
                "type": "theme_lag_validation",
                "title": title,
                "message": message,
                "data": {
                    "index_store_key": sk,
                    "anchor_trade_date": anchor,
                    "run_status": run_status,
                    "eval_completed_at_utc_ts": ev_ts,
                    "scheduler_job_id": job_id,
                    "aggregate": agg,
                },
                "priority": "medium",
                "is_read": False,
                "created_at": now_ts,
                "read_at": None,
            }
            notifications_coll.insert_one(notification)

            subject = f"[悟空量化] {title} · {sk} {anchor}"
            text_body = f"{message}\n\n指数: {sk}\n锚定日: {anchor}\n"
            html_body = (
                f"<p>{message}</p><ul>"
                f"<li>指数: {sk}</li><li>锚定日: {anchor}</li>"
                f"<li>状态: {run_status}</li></ul>"
            )
            _send_optional_theme_lag_email(collections, user_id, subject, text_body, html_body)

            subs_coll.update_one(
                {"_id": sub["_id"]},
                {
                    "$set": {
                        "last_notified_eval_utc_ts": ev_ts,
                        "last_notified_anchor_trade_date": anchor,
                        "updated_at": now_dt,
                    }
                },
            )
            log.info(
                "[%s] notified user_id=%s index=%s anchor=%s ev_ts=%s",
                job_id,
                user_id,
                sk,
                anchor,
                ev_ts,
            )
    except Exception as e:
        log.error("[%s] theme_lag_validation subscription scan failed: %s", job_id, e, exc_info=True)
