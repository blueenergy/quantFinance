from celery import Celery
from celery.schedules import crontab
import logging

log = logging.getLogger(__name__)

import os

celery_app = Celery(
    'tasks',
    broker=os.getenv('REDIS_URL', 'redis://localhost:6379/0'),
    backend=os.getenv('REDIS_URL', 'redis://localhost:6379/0')
)

celery_app.conf.update(
    task_always_eager=True,
    task_eager_propagates=True,
)



# 
# fetch_ladder_data_for_analysis logic has been moved to quantAnalyzer/services/market_analyzer.py
# 

celery_app.conf.beat_schedule = {
    # Check worker timeouts every minute
    'check-worker-timeouts': {
        'task': 'celery_app.check_worker_timeouts_task',
        'schedule': 60.0,  # Every 60 seconds
    },
    # Send daily reports at 17:00 (5 PM) every day
    'send-daily-reports': {
        'task': 'celery_app.send_daily_reports_task',
        'schedule': crontab(hour=17, minute=0),  # 5 PM daily
    },
    # Generate AI market analysis multiple times on trading days
    'generate-market-analysis-1000': {
        'task': 'celery_app.generate_market_analysis_task',
        'schedule': crontab(hour=10, minute=0, day_of_week='1-5'),  # Mon-Fri 10:00
    },
    'generate-market-analysis-1130': {
        'task': 'celery_app.generate_market_analysis_task',
        'schedule': crontab(hour=11, minute=30, day_of_week='1-5'),  # Mon-Fri 11:30
    },
    'generate-market-analysis-1530': {
        'task': 'celery_app.generate_market_analysis_task',
        'schedule': crontab(hour=15, minute=30, day_of_week='1-5'),  # Mon-Fri 15:30
    },
}

celery_app.conf.timezone = 'Asia/Shanghai'

@celery_app.task
def deep_analysis_task(symbol):
    import time
    time.sleep(10)  # 模拟耗时任务
    return f"{symbol} 深度分析完成"

@celery_app.task
def long_task(symbol):
    # 你的耗时逻辑
    return f"{symbol} 分析完成"


@celery_app.task(name='celery_app.check_worker_timeouts_task')
def check_worker_timeouts_task():
    """Check for worker timeouts and send alerts.
    
    Runs every minute to check if any workers have stopped sending heartbeats.
    """
    from db import get_db_collections
    from services.email_service import send_worker_timeout_alert
    from datetime import datetime
    import time
    
    try:
        collections = get_db_collections()
        worker_status_coll = collections["worker_status"]
        notifications_coll = collections["notifications"]
        settings_coll = collections["notification_settings"]
        users_coll = collections["users"]
        
        now = time.time()
        
        # Get all users with timeout alerts enabled
        enabled_settings = settings_coll.find({
            "worker_timeout_enabled": True
        })
        
        for setting in enabled_settings:
            user_id = setting.get("user_id")
            threshold_minutes = setting.get("worker_timeout_threshold_minutes", 3)
            threshold_seconds = threshold_minutes * 60
            
            # Find workers that have timed out
            cutoff_time = now - threshold_seconds
            
            timed_out_workers = worker_status_coll.find({
                "user_id": user_id,
                "last_heartbeat": {"$lt": cutoff_time},
                "status": {"$ne": "stopped"}  # Don't alert for intentionally stopped workers
            })
            
            for worker in timed_out_workers:
                worker_id = worker.get("worker_id")
                
                # Check if we already sent alert recently (avoid spam)
                recent_alert = notifications_coll.find_one({
                    "user_id": user_id,
                    "type": "worker_timeout",
                    "data.worker_id": worker_id,
                    "created_at": {"$gt": now - 1800}  # No alert in last 30 min
                })
                
                if recent_alert:
                    continue  # Skip if already alerted recently
                
                # Create notification
                last_heartbeat_time = datetime.fromtimestamp(
                    worker.get("last_heartbeat", now)
                ).strftime("%Y-%m-%d %H:%M:%S")
                
                notification = {
                    "user_id": user_id,
                    "type": "worker_timeout",
                    "title": f"⚠️ Worker 超时警告",
                    "message": f"{worker.get('symbol')} ({worker.get('strategy')}) 已超过 {threshold_minutes} 分钟未响应",
                    "data": {
                        "worker_id": worker_id,
                        "symbol": worker.get("symbol"),
                        "strategy": worker.get("strategy"),
                        "last_heartbeat": worker.get("last_heartbeat"),
                        "last_heartbeat_time": last_heartbeat_time
                    },
                    "priority": "high",
                    "is_read": False,
                    "created_at": now
                }
                
                notifications_coll.insert_one(notification)
                log.info(f"Created timeout alert for worker {worker_id} (user {user_id})")
                
                # Send email if enabled
                if setting.get("email_notifications_enabled"):
                    user = users_coll.find_one({"_id": user_id})
                    if user and user.get("email"):
                        send_worker_timeout_alert(
                            to_email=user["email"],
                            username=user.get("username", "User"),
                            worker_info={
                                "symbol": worker.get("symbol"),
                                "strategy": worker.get("strategy"),
                                "last_heartbeat_time": last_heartbeat_time
                            }
                        )
        
        return {"success": True, "checked_at": now}
        
    except Exception as e:
        log.error(f"Failed to check worker timeouts: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@celery_app.task(name='celery_app.send_daily_reports_task')
def send_daily_reports_task():
    """Send daily worker activity reports to all users.
    
    Runs daily at configured time (default: 17:00).
    """
    from db import get_db_collections
    from services.email_service import send_daily_report
    from datetime import datetime, timedelta
    import time
    
    try:
        collections = get_db_collections()
        settings_coll = collections["notification_settings"]
        users_coll = collections["users"]
        activity_coll = collections["worker_activity"]
        signals_coll = collections["trade_signals"]
        executions_coll = collections["trade_executions"]
        worker_status_coll = collections["worker_status"]
        
        # Get today's date range
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow = today + timedelta(days=1)
        today_ts = today.timestamp()
        tomorrow_ts = tomorrow.timestamp()
        
        # Get all users with daily reports enabled
        enabled_settings = settings_coll.find({
            "daily_report_enabled": True,
            "email_notifications_enabled": True
        })
        
        sent_count = 0
        
        for setting in enabled_settings:
            user_id = setting.get("user_id")
            
            # Get user info
            user = users_coll.find_one({"_id": user_id})
            if not user or not user.get("email"):
                continue
            
            # Collect today's statistics
            total_scans = activity_coll.count_documents({
                "user_id": user_id,
                "activity_type": "scan",
                "timestamp": {"$gte": today_ts, "$lt": tomorrow_ts}
            })
            
            total_signals = signals_coll.count_documents({
                "user_id": user_id,
                "timestamp": {"$gte": today_ts, "$lt": tomorrow_ts}
            })
            
            total_trades = executions_coll.count_documents({
                "user_id": user_id,
                "mode": "live",
                "timestamp": {"$gte": today_ts, "$lt": tomorrow_ts}
            })
            
            # Get worker summaries
            workers = []
            worker_docs = worker_status_coll.find({"user_id": user_id})
            
            for worker in worker_docs:
                worker_id = worker.get("worker_id")
                
                # Get today's activities for this worker
                worker_scans = activity_coll.count_documents({
                    "user_id": user_id,
                    "worker_id": worker_id,
                    "activity_type": "scan",
                    "timestamp": {"$gte": today_ts, "$lt": tomorrow_ts}
                })
                
                worker_signals = signals_coll.count_documents({
                    "user_id": user_id,
                    "symbol": worker.get("symbol"),
                    "strategy": worker.get("strategy"),
                    "timestamp": {"$gte": today_ts, "$lt": tomorrow_ts}
                })
                
                # Calculate uptime
                started_at = worker.get("started_at", time.time())
                uptime_seconds = time.time() - started_at
                uptime_hours = int(uptime_seconds / 3600)
                uptime_minutes = int((uptime_seconds % 3600) / 60)
                uptime_str = f"{uptime_hours}h {uptime_minutes}m" if uptime_hours > 0 else f"{uptime_minutes}m"
                
                workers.append({
                    "symbol": worker.get("symbol"),
                    "strategy": worker.get("strategy"),
                    "scans": worker_scans,
                    "signals": worker_signals,
                    "uptime": uptime_str
                })
            
            # Prepare report data
            report_data = {
                "date": today.strftime("%Y-%m-%d"),
                "total_scans": total_scans,
                "total_signals": total_signals,
                "total_trades": total_trades,
                "workers": workers
            }
            
            # Send email
            success = send_daily_report(
                to_email=user["email"],
                username=user.get("username", "User"),
                report_data=report_data
            )
            
            if success:
                sent_count += 1
                log.info(f"Daily report sent to {user['email']}")
        
        return {
            "success": True,
            "sent_count": sent_count,
            "date": today.strftime("%Y-%m-%d")
        }
        
    except Exception as e:
        log.error(f"Failed to send daily reports: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@celery_app.task(name='celery_app.generate_market_analysis_task')
def generate_market_analysis_task():
    """
    Schedule AI market analysis task.
    Instead of running it here (which blocks if eager=True), we push a task to MongoDB.
    quantAnalyzer will pick it up and execute it asynchronously.
    """
    from db import get_db_collections
    from datetime import datetime
    
    try:
        collections = get_db_collections()
        tasks_col = collections["analysis_tasks"]
        users_coll = collections["users"]
        
        # 1. Determine Analysis Date (Today)
        # We use current system date. quantAnalyzer will fetch market data for this date.
        today_str = datetime.now().strftime('%Y-%m-%d')
        
        # 2. Get Admin User for Attribution
        admin_user = users_coll.find_one({"username": "admin", "is_admin": True})
        if not admin_user:
            log.error("[Market Analysis] Admin user not found, assigning to 'system'")
            user_id = "system"
        else:
            user_id = str(admin_user["_id"])
            
        # 3. Check if task already pending/completed for today to avoid duplicates
        # We allow re-triggering if failed, but avoid queue spamming
        existing = tasks_col.find_one({
            "type": "market_analysis",
            "params.date": today_str,
            "status": {"$in": ["pending", "processing"]}
        })
        
        if existing:
            log.info(f"[Market Analysis] Task already pending for {today_str}, skipping schedule.")
            return {"success": True, "message": "already_queued"}
            
        # 4. Push Task
        task_doc = {
            "type": "market_analysis",
            "status": "pending",
            "priority": 10, # High priority
            "user_id": user_id,
            "stock_name": "大盘指数", # For UI display compatible
            "symbol": "MARKET",       # For UI display compatible
            "params": {
                "date": today_str
            },
            "created_at": datetime.now(),
            "source": "scheduler"
        }
        
        result = tasks_col.insert_one(task_doc)
        log.info(f"[Market Analysis] Scheduled task {result.inserted_id} for {today_str}")
        return {"success": True, "task_id": str(result.inserted_id)}
        
    except Exception as e:
        log.error(f"[Market Analysis] Scheduling failed: {e}", exc_info=True)
        return {"success": False, "error": str(e)}