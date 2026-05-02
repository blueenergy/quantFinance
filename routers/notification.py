"""Notification API endpoints.

Provides notification services:
- Worker timeout alerts
- Signal generation notifications
- Daily report email scheduling
"""

from fastapi import APIRouter, Depends, Query, HTTPException, BackgroundTasks
from typing import List, Optional, Dict, Any
from datetime import datetime, timedelta
from auth import get_current_active_user
from db import get_db_collections
from utils.stock_utils import serialize_doc
import time

router = APIRouter()


@router.get("/notifications", summary="获取用户通知")
def get_notifications(
    unread_only: bool = Query(False, description="只获取未读通知"),
    limit: int = Query(50, ge=1, le=200, description="返回数量"),
    current_user: dict = Depends(get_current_active_user)
):
    """Get user notifications.
    
    Notification types:
    - worker_timeout: Worker heartbeat timeout
    - signal_generated: New trading signal
    - daily_report: Daily report ready
    - theme_lag_validation: 主题补涨验证 quant_data 结果更新（站内必达；邮件随通知设置）
    - system_alert: System alerts
    """
    try:
        collections = get_db_collections()
        notifications_coll = collections["notifications"]
        
        user_id = current_user["id"]
        
        # Build query
        query = {"user_id": user_id}
        if unread_only:
            query["is_read"] = False
        
        # Fetch notifications
        cursor = notifications_coll.find(query).sort("created_at", -1).limit(limit)
        notifications = [serialize_doc(doc) for doc in cursor]
        
        # Count unread
        unread_count = notifications_coll.count_documents({
            "user_id": user_id,
            "is_read": False
        })
        
        return {
            "success": True,
            "data": {
                "notifications": notifications,
                "unread_count": unread_count,
                "total": len(notifications)
            }
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取通知失败: {str(e)}")


@router.post("/notifications/{notification_id}/read", summary="标记通知已读")
def mark_notification_read(
    notification_id: str,
    current_user: dict = Depends(get_current_active_user)
):
    """Mark notification as read."""
    try:
        collections = get_db_collections()
        notifications_coll = collections["notifications"]
        
        user_id = current_user["id"]
        
        result = notifications_coll.update_one(
            {"_id": notification_id, "user_id": user_id},
            {"$set": {"is_read": True, "read_at": time.time()}}
        )
        
        if result.matched_count == 0:
            raise HTTPException(status_code=404, detail="通知不存在")
        
        return {"success": True}
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"标记失败: {str(e)}")


@router.post("/notifications/read-all", summary="标记所有通知已读")
def mark_all_notifications_read(current_user: dict = Depends(get_current_active_user)):
    """Mark all notifications as read."""
    try:
        collections = get_db_collections()
        notifications_coll = collections["notifications"]
        
        user_id = current_user["id"]
        now = time.time()
        
        result = notifications_coll.update_many(
            {"user_id": user_id, "is_read": False},
            {"$set": {"is_read": True, "read_at": now}}
        )
        
        return {
            "success": True,
            "updated_count": result.modified_count
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"标记失败: {str(e)}")


@router.delete("/notifications/{notification_id}", summary="删除通知")
def delete_notification(
    notification_id: str,
    current_user: dict = Depends(get_current_active_user)
):
    """Delete a notification."""
    try:
        collections = get_db_collections()
        notifications_coll = collections["notifications"]
        
        user_id = current_user["id"]
        
        result = notifications_coll.delete_one({
            "_id": notification_id,
            "user_id": user_id
        })
        
        if result.deleted_count == 0:
            raise HTTPException(status_code=404, detail="通知不存在")
        
        return {"success": True}
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"删除失败: {str(e)}")


@router.get("/notifications/settings", summary="获取通知设置")
def get_notification_settings(current_user: dict = Depends(get_current_active_user)):
    """Get user notification settings."""
    try:
        collections = get_db_collections()
        settings_coll = collections["notification_settings"]
        
        user_id = current_user["id"]
        
        settings = settings_coll.find_one({"user_id": user_id})
        
        if not settings:
            # Return default settings
            return {
                "success": True,
                "data": {
                    "worker_timeout_enabled": True,
                    "worker_timeout_threshold_minutes": 3,
                    "signal_notification_enabled": True,
                    "daily_report_enabled": True,
                    "daily_report_time": "17:00",  # 5 PM
                    "email_notifications_enabled": True,
                    "email": current_user.get("email")
                }
            }
        
        return {
            "success": True,
            "data": serialize_doc(settings)
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取设置失败: {str(e)}")


@router.put("/notifications/settings", summary="更新通知设置")
def update_notification_settings(
    settings: Dict[str, Any],
    current_user: dict = Depends(get_current_active_user)
):
    """Update notification settings."""
    try:
        collections = get_db_collections()
        settings_coll = collections["notification_settings"]
        
        user_id = current_user["id"]
        
        # Allowed fields
        allowed_fields = {
            "worker_timeout_enabled",
            "worker_timeout_threshold_minutes",
            "signal_notification_enabled",
            "daily_report_enabled",
            "daily_report_time",
            "email_notifications_enabled"
        }
        
        update_doc = {k: v for k, v in settings.items() if k in allowed_fields}
        update_doc["updated_at"] = time.time()
        
        settings_coll.update_one(
            {"user_id": user_id},
            {"$set": update_doc},
            upsert=True
        )
        
        return {"success": True, "data": update_doc}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"更新设置失败: {str(e)}")


@router.post("/notifications/test-email", summary="发送测试邮件")
def send_test_email(
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_active_user)
):
    """Send a test email to verify email configuration."""
    try:
        from services.email_service import send_test_email as send_email
        
        email = current_user.get("email")
        if not email:
            raise HTTPException(status_code=400, detail="用户未设置邮箱")
        
        # Send in background
        background_tasks.add_task(
            send_email,
            to_email=email,
            username=current_user.get("username")
        )
        
        return {
            "success": True,
            "message": f"测试邮件已发送到 {email}"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"发送失败: {str(e)}")


# Internal API for creating notifications (called by workers/tasks)
@router.post("/notifications/create", summary="创建通知（内部使用）")
def create_notification(
    payload: Dict[str, Any],
    current_user: dict = Depends(get_current_active_user)
):
    """Create a notification (internal use).
    
    Payload:
    - type: worker_timeout/signal_generated/daily_report/theme_lag_validation/system_alert
    - title: Notification title
    - message: Notification message
    - data: Additional data (optional)
    - priority: low/medium/high (default: medium)
    """
    try:
        collections = get_db_collections()
        notifications_coll = collections["notifications"]
        
        user_id = current_user["id"]
        now = time.time()
        
        notification = {
            "user_id": user_id,
            "type": payload.get("type", "system_alert"),
            "title": payload.get("title", ""),
            "message": payload.get("message", ""),
            "data": payload.get("data", {}),
            "priority": payload.get("priority", "medium"),
            "is_read": False,
            "created_at": now,
            "read_at": None
        }
        
        result = notifications_coll.insert_one(notification)
        
        return {
            "success": True,
            "notification_id": str(result.inserted_id)
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"创建通知失败: {str(e)}")
