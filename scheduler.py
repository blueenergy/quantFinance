from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from datetime import datetime
import logging
import time

log = logging.getLogger(__name__)

# Define global scheduler
scheduler = BackgroundScheduler(timezone='Asia/Shanghai')

def check_worker_timeouts_task():
    """Check for worker timeouts and send alerts."""
    from db import get_db_collections
    from services.email_service import send_worker_timeout_alert
    
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
    except Exception as e:
        log.error(f"Failed to check worker timeouts: {e}", exc_info=True)


def send_daily_reports_task():
    """Send daily worker activity reports to all users."""
    from db import get_db_collections
    from services.email_service import send_daily_report
    from datetime import timedelta
    
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
            send_daily_report(
                to_email=user["email"],
                username=user.get("username", "User"),
                report_data=report_data
            )
            log.info(f"Daily report sent to {user['email']}")
            
    except Exception as e:
        log.error(f"Failed to send daily reports: {e}", exc_info=True)


def generate_market_analysis_task():
    """Schedule AI market analysis task to MongoDB (picked up by quantAnalyzer)."""
    from db import get_db_collections
    
    try:
        collections = get_db_collections()
        tasks_col = collections["analysis_tasks"]
        users_coll = collections["users"]
        
        today_str = datetime.now().strftime('%Y-%m-%d')
        
        admin_user = users_coll.find_one({"username": "admin", "is_admin": True})
        if not admin_user:
            user_id = "system"
        else:
            user_id = str(admin_user["_id"])
            
        existing = tasks_col.find_one({
            "type": "market_analysis",
            "params.date": today_str,
            "status": {"$in": ["pending", "processing"]}
        })
        
        if existing:
            log.info(f"[Market Analysis] Task already pending for {today_str}, skipping schedule.")
            return
            
        task_doc = {
            "type": "market_analysis",
            "status": "pending",
            "priority": 10,
            "user_id": user_id,
            "stock_name": "大盘指数",
            "symbol": "MARKET",
            "params": {
                "date": today_str
            },
            "created_at": datetime.now(),
            "source": "scheduler"
        }
        
        result = tasks_col.insert_one(task_doc)
        log.info(f"[Market Analysis] Scheduled task {result.inserted_id} for {today_str}")
        
    except Exception as e:
        log.error(f"[Market Analysis] Scheduling failed: {e}", exc_info=True)


def generate_global_analysis_task():
    """Schedule Global Market Analysis task (Runs at 08:30 AM)."""
    from db import get_db_collections
    
    try:
        collections = get_db_collections()
        tasks_col = collections["analysis_tasks"]
        users_coll = collections["users"]
        
        today_str = datetime.now().strftime('%Y-%m-%d')
        admin_user = users_coll.find_one({"username": "admin", "is_admin": True})
        user_id = str(admin_user["_id"]) if admin_user else "system"
            
        existing = tasks_col.find_one({
            "type": "global_analysis",
            "params.date": today_str,
            "status": {"$in": ["pending", "processing"]}
        })
        
        if existing:
            log.info(f"[Global Analysis] Task already pending for {today_str}, skipping.")
            return
            
        task_doc = {
            "type": "global_analysis",
            "status": "pending",
            "priority": 15,
            "user_id": user_id,
            "stock_name": "全球市场联动",
            "symbol": "GLOBAL",
            "params": {
                "date": today_str
            },
            "created_at": datetime.now(),
            "source": "scheduler"
        }
        
        result = tasks_col.insert_one(task_doc)
        log.info(f"[Global Analysis] Scheduled task {result.inserted_id} for {today_str}")
        
    except Exception as e:
        log.error(f"[Global Analysis] Scheduling failed: {e}", exc_info=True)


def start_scheduler():
    """Initialize and start the scheduler with all configured jobs."""
    try:
        # Check worker timeouts every 60 seconds
        scheduler.add_job(
            check_worker_timeouts_task, 
            trigger=IntervalTrigger(seconds=60), 
            id='check_worker_timeouts', 
            replace_existing=True
        )
        
        # Send daily reports at 17:00 (5 PM) every day
        scheduler.add_job(
            send_daily_reports_task,
            trigger=CronTrigger(hour=17, minute=0),
            id='send_daily_reports',
            replace_existing=True
        )
        
        # Market analysis 3 times a day on weekdays
        scheduler.add_job(
            generate_market_analysis_task,
            trigger=CronTrigger(hour=10, minute=0, day_of_week='mon-fri'),
            id='market_analysis_1000',
            replace_existing=True
        )
        scheduler.add_job(
            generate_market_analysis_task,
            trigger=CronTrigger(hour=11, minute=30, day_of_week='mon-fri'),
            id='market_analysis_1130',
            replace_existing=True
        )
        scheduler.add_job(
            generate_market_analysis_task,
            trigger=CronTrigger(hour=16, minute=30, day_of_week='mon-fri'),
            id='market_analysis_1630',
            replace_existing=True
        )
        
        # Global market analysis at 08:30 AM before open
        scheduler.add_job(
            generate_global_analysis_task,
            trigger=CronTrigger(hour=8, minute=30, day_of_week='mon-fri'),
            id='global_analysis_0830',
            replace_existing=True
        )
        
        # Start the scheduler background loop
        scheduler.start()
        log.info("APScheduler started successfully with Asia/Shanghai timezone.")
    except Exception as e:
        log.error(f"Failed to start APScheduler: {e}")

def stop_scheduler():
    """Graceful shutdown for scheduler."""
    if scheduler.running:
        scheduler.shutdown()
        log.info("APScheduler stopped.")
