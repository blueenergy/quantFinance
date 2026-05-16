from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from datetime import datetime, timezone
import logging
import os
import time
from pymongo.errors import DuplicateKeyError

log = logging.getLogger(__name__)

# Define global scheduler
scheduler = BackgroundScheduler(timezone='Asia/Shanghai')
_SCHEDULER_INSTANCE_ID = f"{os.uname().nodename}:{os.getpid()}"
_SCHEDULER_PRIMARY_LOCK_ID = "scheduler_primary_lock"
_SCHEDULER_STATUS_ID = "primary"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _get_scheduler_status_collection():
    from db import get_db_collections

    return get_db_collections()["scheduler_status"]


def acquire_primary_scheduler_lock(ttl_seconds: int = 90) -> bool:
    """Acquire the single-runner lease for the standalone scheduler process."""
    now = time.time()
    try:
        result = _get_scheduler_status_collection().update_one(
            {
                "_id": _SCHEDULER_PRIMARY_LOCK_ID,
                "$or": [
                    {"locked_until": {"$lte": now}},
                    {"locked_until": {"$exists": False}},
                ],
            },
            {
                "$set": {
                    "locked_by": _SCHEDULER_INSTANCE_ID,
                    "locked_until": now + ttl_seconds,
                    "updated_at": _utc_now(),
                },
                "$setOnInsert": {"created_at": _utc_now()},
            },
            upsert=True,
        )
    except DuplicateKeyError:
        return False
    return bool(result.upserted_id or result.modified_count)


def renew_primary_scheduler_lock(ttl_seconds: int = 90) -> bool:
    """Renew the single-runner lease; returns False if another runner owns it."""
    now = time.time()
    result = _get_scheduler_status_collection().update_one(
        {"_id": _SCHEDULER_PRIMARY_LOCK_ID, "locked_by": _SCHEDULER_INSTANCE_ID},
        {
            "$set": {
                "locked_until": now + ttl_seconds,
                "updated_at": _utc_now(),
            }
        },
    )
    return bool(result.modified_count)


def release_primary_scheduler_lock() -> None:
    _get_scheduler_status_collection().update_one(
        {"_id": _SCHEDULER_PRIMARY_LOCK_ID, "locked_by": _SCHEDULER_INSTANCE_ID},
        {
            "$set": {
                "locked_until": time.time(),
                "updated_at": _utc_now(),
            }
        },
    )


def schedule_config_signature() -> str:
    """Return a compact signature for detecting schedule_config changes."""
    from db import get_db_collections

    config_coll = get_db_collections()["schedule_config"]
    parts = []
    for doc in config_coll.find({}, {"job_id": 1, "updated_at": 1, "is_active": 1, "type": 1, "seconds": 1, "hour": 1, "minute": 1, "day_of_week": 1}):
        updated = doc.get("updated_at")
        if hasattr(updated, "timestamp"):
            updated_value = str(updated.timestamp())
        else:
            updated_value = str(updated)
        parts.append(
            "|".join(
                [
                    str(doc.get("job_id", "")),
                    str(doc.get("is_active", "")),
                    str(doc.get("type", "")),
                    str(doc.get("seconds", "")),
                    str(doc.get("hour", "")),
                    str(doc.get("minute", "")),
                    str(doc.get("day_of_week", "")),
                    updated_value,
                ]
            )
        )
    return "\n".join(sorted(parts))


def scheduler_jobs_snapshot() -> dict:
    """Return the scheduler in-memory job state for admin/status APIs."""
    jobs = {}
    for job in scheduler.get_jobs():
        jobs[job.id] = {
            "id": job.id,
            "name": job.name,
            "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
        }
    return jobs


def write_scheduler_status(state: str = "running", error: str = None) -> None:
    """Persist runner heartbeat and loaded APScheduler job snapshot."""
    _get_scheduler_status_collection().update_one(
        {"_id": _SCHEDULER_STATUS_ID},
        {
            "$set": {
                "instance_id": _SCHEDULER_INSTANCE_ID,
                "state": state,
                "heartbeat_at": _utc_now(),
                "heartbeat_ts": time.time(),
                "jobs": scheduler_jobs_snapshot(),
                "scheduler_running": scheduler.running,
                "error": error,
                "updated_at": _utc_now(),
            },
            "$setOnInsert": {"created_at": _utc_now()},
        },
        upsert=True,
    )


def _acquire_scheduler_job_lock(job_id: str, ttl_seconds: int = 1800) -> bool:
    """Best-effort Mongo lease so duplicated scheduler instances do not run the same job."""
    from db import get_db_collections

    now = time.time()
    lock_id = f"scheduler_lock:{job_id}"
    try:
        result = get_db_collections()["job_status"].update_one(
            {
                "_id": lock_id,
                "$or": [
                    {"locked_until": {"$lte": now}},
                    {"locked_until": {"$exists": False}},
                ],
            },
            {
                "$set": {
                    "job_id": lock_id,
                    "locked_by": _SCHEDULER_INSTANCE_ID,
                    "locked_until": now + ttl_seconds,
                    "updated_at": now,
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
    except DuplicateKeyError:
        return False
    return bool(result.upserted_id or result.modified_count)


def _release_scheduler_job_lock(job_id: str) -> None:
    from db import get_db_collections

    get_db_collections()["job_status"].update_one(
        {"_id": f"scheduler_lock:{job_id}", "locked_by": _SCHEDULER_INSTANCE_ID},
        {"$set": {"locked_until": time.time(), "updated_at": time.time()}},
    )


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


def generate_market_analysis_task(job_id: str):
    """Schedule AI market analysis task to MongoDB (picked up by quantAnalyzer).
    
    Job types:
    - market_analysis_1000: Intraday (早盘 10:00)
    - market_analysis_1130: Intraday (午盘 11:30)
    - market_analysis_1630: Post-market (收盘 16:30) - includes THS money flow
    """
    from db import get_db_collections
    from services.analysis_tasks import build_task_doc
    
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
            "params.job_id": job_id,
            "status": {"$in": ["pending", "processing"]}
        })
        
        if existing:
            log.info(f"[Market Analysis] Task '{job_id}' already pending for {today_str}, skipping schedule.")
            return
        
        # Determine analysis type based on job_id
        # 16:30 is post-market, others are intraday
        analysis_type = "post_market" if "1630" in job_id else "intraday"

        # Extract HHMM time slot from job_id suffix (e.g. "market_analysis_1130" → "1130").
        # Falls back to current local HHMM for any non-conformant id (e.g. manual triggers).
        time_slot = job_id.rsplit("_", 1)[-1] if job_id.rsplit("_", 1)[-1].isdigit() and len(job_id.rsplit("_", 1)[-1]) == 4 else datetime.now().strftime("%H%M")

        task_doc = build_task_doc(
            current_user={
                "id": user_id,
                "username": "system" if user_id == "system" else "admin",
            },
            task_type="market_analysis",
            priority=10,
            symbol="MARKET",
            stock_name="大盘指数",
            params={
                "date": today_str,
                "job_id": job_id,
                "analysis_type": analysis_type,
                "time_slot": time_slot,
            },
            source="scheduler",
        )
        
        result = tasks_col.insert_one(task_doc)
        log.info(f"[Market Analysis] Scheduled task {result.inserted_id} for {today_str} slot={time_slot} (type: {analysis_type})")
        
    except Exception as e:
        log.error(f"[Market Analysis] Scheduling failed: {e}", exc_info=True)


def generate_global_analysis_task(job_id: str):
    """Schedule Global Market Analysis task (Runs at 08:30 AM)."""
    from db import get_db_collections
    from services.analysis_tasks import build_task_doc
    
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
            "params.job_id": job_id,
            "status": {"$in": ["pending", "processing"]}
        })
        
        if existing:
            log.info(f"[Global Analysis] Task '{job_id}' already pending for {today_str}, skipping.")
            return
            
        task_doc = build_task_doc(
            current_user={
                "id": user_id,
                "username": "system" if user_id == "system" else "admin",
            },
            task_type="global_analysis",
            priority=15,
            symbol="GLOBAL",
            stock_name="全球市场联动",
            params={
                "date": today_str,
                "job_id": job_id,
            },
            source="scheduler",
        )
        
        result = tasks_col.insert_one(task_doc)
        log.info(f"[Global Analysis] Scheduled task {result.inserted_id} for {today_str}")
        
    except Exception as e:
        log.error(f"[Global Analysis] Scheduling failed: {e}", exc_info=True)


def theme_lag_validation_subscription_scan_task(job_id: str):
    """按订阅扫描 theme_lag_candidate_validation，写入 notifications；邮件见用户通知设置。"""
    from services.theme_lag_validation_subscription_task import run_theme_lag_validation_subscription_scan

    run_theme_lag_validation_subscription_scan(job_id)


def market_insight_focus_review_task():
    """Run post-market validation for Fire Eye focus items."""
    from services.market_insight_engine import review_market_insight_focus

    job_id = "market_insight_focus_review_1530"
    if not _acquire_scheduler_job_lock(job_id):
        log.info("[Market Insight Review] Another scheduler instance owns the lock, skipping.")
        return
    try:
        result = review_market_insight_focus(horizon_minutes=60)
        log.info(
            "[Market Insight Review] Reviewed %s focus items for %s",
            result.get("written", 0),
            result.get("trade_date"),
        )
    except Exception as e:
        log.error(f"[Market Insight Review] Scheduling failed: {e}", exc_info=True)
    finally:
        _release_scheduler_job_lock(job_id)


def hot_stock_poll_task(source: str):
    """热股轮询任务 - 每30分钟检查 THS / DC 热榜是否有新数据。"""
    from db import get_db_collections, get_analyzer_db
    from services.analysis_tasks import build_task_doc

    try:
        collections = get_db_collections()
        tasks_col = collections["analysis_tasks"]
        analysis_col = get_analyzer_db()["hot_stock_analysis"]

        # Check already running task for this source
        existing = tasks_col.find_one({
            "type": "hot_stock_analysis",
            "params.source": source,
            "status": {"$in": ["pending", "processing"]},
        })
        if existing:
            log.info(f"[HotStock Poll] {source.upper()} task already in queue, skipping.")
            return

        # Get last known rank_time from analysis history
        last_doc = analysis_col.find_one({"source": source}, sort=[("created_at", -1)])
        last_rank_time = last_doc.get("rank_time") if last_doc else None

        task_doc = build_task_doc(
            current_user={"id": "system", "username": "system"},
            task_type="hot_stock_analysis",
            priority=20,
            params={
                "source": source,
                "last_rank_time": last_rank_time,
            },
            source="scheduler",
        )
        result = tasks_col.insert_one(task_doc)
        log.info(f"[HotStock Poll] {source.upper()} task submitted ({result.inserted_id}), last_rank_time={last_rank_time}")

    except Exception as e:
        log.error(f"[HotStock Poll] {source.upper()} scheduling failed: {e}", exc_info=True)


# Map of configurable tasks to logic（启用/停用请用 Admin 与 DB 中的 is_active，勿在此注释注册项）
TASK_MAP = {
    'check_worker_timeouts': check_worker_timeouts_task,
    'send_daily_reports': send_daily_reports_task,
    'market_analysis_1000': generate_market_analysis_task,
    'market_analysis_1130': generate_market_analysis_task,
    'market_analysis_1630': generate_market_analysis_task,
    'global_analysis_0830': generate_global_analysis_task,
    'hot_stock_poll_ths': lambda: hot_stock_poll_task('ths'),
    'hot_stock_poll_dc': lambda: hot_stock_poll_task('dc'),
    'theme_lag_validation_subscribe_1530': lambda: theme_lag_validation_subscription_scan_task(
        'theme_lag_validation_subscribe_1530'
    ),
    'theme_lag_validation_subscribe_1630': lambda: theme_lag_validation_subscription_scan_task(
        'theme_lag_validation_subscribe_1630'
    ),
    'market_insight_focus_review_1530': market_insight_focus_review_task,
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
        'day_of_week': 'mon-fri',
        'is_active': False,  # 默认关闭省 token；可在管理后台开启
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
    },
    'hot_stock_poll_ths': {
        'name': "同花顺热股轮询",
        'type': "interval",
        'seconds': 3600,
        'is_active': False,  # 默认关闭；需要时在管理后台启用
    },
    'hot_stock_poll_dc': {
        'name': "东方财富热股轮询",
        'type': "interval",
        'seconds': 1800,
        'is_active': False,  # 默认关闭；需要时在管理后台启用
    },
    'theme_lag_validation_subscribe_1530': {
        'name': "主题补涨验证订阅扫描 (15:30)",
        'type': "cron",
        'hour': 15,
        'minute': 30,
        'day_of_week': 'mon-fri',
        'is_active': False,  # 默认关闭；避免首次落库即跑扫描导致意外通知/邮件，在管理后台再启用
    },
    'theme_lag_validation_subscribe_1630': {
        'name': "主题补涨验证订阅扫描 (16:30)",
        'type': "cron",
        'hour': 16,
        'minute': 30,
        'day_of_week': 'mon-fri',
        'is_active': False,  # 同上
    },
    'market_insight_focus_review_1530': {
        'name': "火眼金睛焦点盘后验证 (15:30)",
        'type': "cron",
        'hour': 15,
        'minute': 30,
        'day_of_week': 'mon-fri',
        'is_active': True,
    },
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
        # DEFAULT_SCHEDULES 与 TASK_MAP 应保持一致；若仅有配置无执行函数，清掉陈旧 job 并视为已持久化。
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
            log.warning(f"No TASK_MAP entry for {job_id}; removed stale scheduler job if any.")
        return True

    trigger = get_trigger_from_config(config_data)
    if not trigger:
        return False
        
    is_active = config_data.get('is_active', True)
    
    # Remove existing job if it exists to cleanly replace it
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
        
    if is_active:
        import functools
        
        # Pass job_id if the function expects it (by name match)
        if "market_analysis_" in job_id or "global_analysis" in job_id:
            task_callable = functools.partial(task_func, job_id=job_id)
        else:
            task_callable = task_func

        scheduler.add_job(
            task_callable,
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
            # First time: Save default to DB and use it（copy 避免修改模块内 DEFAULT_SCHEDULES）
            row = default_config.copy()
            row["job_id"] = job_id
            row.setdefault("is_active", True)
            row["updated_at"] = datetime.now()
            config_coll.insert_one(row)
            sync_job(job_id, row)
            
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
