from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from datetime import datetime
import logging
import time

log = logging.getLogger(__name__)

# Define global scheduler
scheduler = BackgroundScheduler(timezone='Asia/Shanghai')


# Default trigger configurations
def check_worker_timeouts_task():
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


# Map of configurable tasks to logic
TASK_MAP = {
    'check_worker_timeouts': check_worker_timeouts_task,
    'send_daily_reports': send_daily_reports_task,
    'market_analysis_1000': generate_market_analysis_task,
    'market_analysis_1130': generate_market_analysis_task,
    'market_analysis_1630': generate_market_analysis_task,
    'global_analysis_0830': generate_global_analysis_task,
}

# Default trigger configurations
DEFAULT_SCHEDULES = {
    'check_worker_timeouts': {
        'name': "Worker状态监控",
        'type': "interval",
        'seconds': 60
    },
    'send_daily_reports': {
        'name': "发送每日盈亏报告",
        'type': "cron",
        'hour': 17,
        'minute': 0
    },
    'market_analysis_1000': {
        'name': "早盘市场分析 (10:00)",
        'type': "cron",
        'hour': 10,
        'minute': 0,
        'day_of_week': 'mon-fri'
    },
    'market_analysis_1130': {
        'name': "午盘市场分析 (11:30)",
        'type': "cron",
        'hour': 11,
        'minute': 30,
        'day_of_week': 'mon-fri'
    },
    'market_analysis_1630': {
        'name': "收盘市场分析 (16:30)",
        'type': "cron",
        'hour': 16,
        'minute': 30,
        'day_of_week': 'mon-fri'
    },
    'global_analysis_0830': {
        'name': "盘前全球市场分析 (08:30)",
        'type': "cron",
        'hour': 8,
        'minute': 30,
        'day_of_week': 'mon-fri'
    }
}

def get_trigger_from_config(config_data):
    """Create an APScheduler trigger from a config dict."""
    config_type = config_data.get('type')
    if config_type == 'interval':
        return IntervalTrigger(seconds=config_data.get('seconds', 60))
    elif config_type == 'cron':
        return CronTrigger(
            hour=config_data.get('hour'),
            minute=config_data.get('minute'),
            day_of_week=config_data.get('day_of_week')
        )
    return None

def sync_job(job_id, config_data):
    """Sync a single job to the scheduler based on config."""
    task_func = TASK_MAP.get(job_id)
    if not task_func:
        return False
        
    trigger = get_trigger_from_config(config_data)
    if not trigger:
        return False
        
    is_active = config_data.get('is_active', True)
    
    # Remove existing job if it exists to cleanly replace it
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
        
    if is_active:
        scheduler.add_job(
            task_func,
            trigger=trigger,
            id=job_id,
            name=config_data.get('name', job_id),
            replace_existing=True
        )
        log.info(f"Loaded schedule task: {job_id} ({config_data.get('name')})")
    else:
        log.info(f"Task disabled: {job_id} ({config_data.get('name')})")
        
    return True

def apply_schedule_config():
    """Load config from DB and apply to scheduler."""
    from db import get_db_collections
    collections = get_db_collections()
    config_coll = collections.get("schedule_config")
    
    if config_coll is None:
        log.error("Failed to get schedule_config collection")
        return False
        
    # Get all customized configs
    custom_configs = {doc['job_id']: doc for doc in config_coll.find({})}
    
    # Process all default jobs
    for job_id, default_config in DEFAULT_SCHEDULES.items():
        if job_id in custom_configs:
            # Use combined config (db overrides defaults)
            db_config = custom_configs[job_id]
            merged_config = {**default_config, **db_config}
            sync_job(job_id, merged_config)
        else:
            # First time: Save default to DB and use it
            default_config['job_id'] = job_id
            default_config['is_active'] = True
            default_config['updated_at'] = datetime.now()
            config_coll.insert_one(default_config)
            sync_job(job_id, default_config)
            
    return True

def start_scheduler():
    """Initialize and start the scheduler with configured jobs."""
    try:
        scheduler.start()
        log.info("APScheduler started successfully. Now applying configurations from database.")
        apply_schedule_config()
    except Exception as e:
        log.error(f"Failed to start APScheduler: {e}")

def stop_scheduler():
    """Graceful shutdown for scheduler."""
    if scheduler.running:
        scheduler.shutdown()
        log.info("APScheduler stopped.")
