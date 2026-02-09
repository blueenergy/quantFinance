from celery import Celery
from celery.schedules import crontab
import logging

log = logging.getLogger(__name__)

celery_app = Celery(
    'tasks',
    broker='redis://localhost:6379/0',
    backend='redis://localhost:6379/0'
)

celery_app.conf.update(
    task_always_eager=True,
    task_eager_propagates=True,
)


def fetch_ladder_data_for_analysis(target_date: str = None) -> dict:
    """
    获取连板天梯数据用于 LLM 市场分析。
    
    Returns:
        dict: 包含情绪得分、梯队分布、主线板块等信息
    """
    from db import get_db_collections
    from datetime import datetime
    
    collections = get_db_collections()
    ladder_col = collections["limit_up_ladder"]
    daily_col = collections["daily_limit_up"]
    
    if not target_date:
        target_date = datetime.now().strftime("%Y%m%d")
    
    # 获取天梯聚合数据
    ladder = ladder_col.find_one({"date": target_date})
    
    # 如果今天没数据，尝试获取最近的
    if not ladder:
        ladder = ladder_col.find_one(
            {"date": {"$lte": target_date}},
            sort=[("date", -1)]
        )
    
    if not ladder:
        return None
    
    actual_date = ladder.get("date", target_date)
    indicators = ladder.get("indicators", {})
    
    # 计算梯队分布
    tier_distribution = {}
    total_limit_up = 0
    max_streak = 0
    
    for tier_key, tier_data in ladder.get("tiers", {}).items():
        count = tier_data.get("count", 0)
        tier_distribution[tier_key] = count
        total_limit_up += count
    
    # 获取最高连板
    highest_stock = daily_col.find_one(
        {"date": actual_date},
        sort=[("streak", -1)]
    )
    if highest_stock:
        max_streak = highest_stock.get("streak", 0)
    
    # 获取主线板块 (取涨停数最多的前3个板块)
    sector_ranking = ladder.get("sector_ranking", [])
    mainlines = [s.get("sector", "") for s in sector_ranking[:3] if s.get("sector")]
    
    # 计算晋级率 (2板以上 / 总涨停)
    high_tier_count = sum(tier_distribution.get(k, 0) for k in ["2", "3", "4+"])
    promotion_rate = high_tier_count / total_limit_up if total_limit_up > 0 else 0
    
    # 情绪等级
    sentiment_score = indicators.get("sentiment_score", 50)
    if sentiment_score >= 80:
        sentiment_level = "极度乐观"
    elif sentiment_score >= 60:
        sentiment_level = "谨慎乐观"
    elif sentiment_score >= 40:
        sentiment_level = "中性观望"
    elif sentiment_score >= 20:
        sentiment_level = "谨慎悲观"
    else:
        sentiment_level = "极度悲观"
    
    return {
        "date": actual_date,
        "sentiment_score": sentiment_score,
        "sentiment_level": sentiment_level,
        "total_limit_up": total_limit_up,
        "max_streak": max_streak,
        "high_tier_count": high_tier_count,
        "promotion_rate": round(promotion_rate, 3),
        "mainlines": mainlines,
        "tier_distribution": tier_distribution,
        "highest_stock": {
            "name": highest_stock.get("name", "") if highest_stock else "",
            "streak": max_streak
        } if highest_stock else None
    }


# Celery Beat schedule configuration
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
    """Generate AI market analysis using admin's LLM config and cache it by trading date."""
    from db import get_db_collections
    from market_analysis_cache import market_analysis_cache
    from llm_service import llm_manager
    from routers import market as market_router
    from datetime import datetime
    import asyncio

    try:
        # 获取市场数据（包含 analysis_date 和 market_data）
        market_bundle = market_router.fetch_market_data()
        if not market_bundle:
            log.warning("[市场分析定时任务] 无市场数据，跳过本次分析")
            return {"success": False, "reason": "no_market_data"}
        
        analysis_date = market_bundle.get("analysis_date")
        if not analysis_date:
            log.warning("[市场分析定时任务] 市场数据未包含分析日期，跳过本次分析")
            return {"success": False, "reason": "no_analysis_date"}
        
        # 新增: 获取连板天梯数据
        ladder_data = fetch_ladder_data_for_analysis(analysis_date)
        if ladder_data:
            market_bundle["ladder_data"] = ladder_data
            log.info(f"[市场分析定时任务] 获取连板天梯数据: 情绪={ladder_data.get('sentiment_score')}, "
                     f"涨停={ladder_data.get('total_limit_up')}, 主线={ladder_data.get('mainlines')}")
        else:
            log.warning("[市场分析定时任务] 未获取到连板天梯数据，将使用基础市场分析")
        
        collections = get_db_collections()
        users_coll = collections["users"]
        
        # 使用固定 admin 账号作为全局配置来源
        admin_user = users_coll.find_one({"username": "admin", "is_admin": True})
        if not admin_user:
            log.error("[市场分析定时任务] 未找到 admin 用户 (username='admin', is_admin=True)，无法生成大盘分析")
            return {"success": False, "reason": "no_admin_user"}
        
        llm_configs = admin_user.get("llm_configs", [])
        active_config_id = admin_user.get("active_llm_config_id")
        active_config = None
        
        # 如果没有激活配置但有可用配置，默认激活第一个
        if not active_config_id and llm_configs:
            active_config = llm_configs[0]
            active_config_id = str(active_config.get("_id"))
            users_coll.update_one(
                {"_id": admin_user["_id"]},
                {"$set": {"active_llm_config_id": active_config_id}}
            )
        else:
            for config in llm_configs:
                if str(config.get("_id")) == active_config_id:
                    active_config = config
                    break
        
        if not active_config:
            log.error("[市场分析定时任务] admin 用户没有激活的 LLM 配置")
            return {"success": False, "reason": "no_active_llm_config"}
        
        provider = active_config.get("provider")
        api_key = active_config.get("api_key")
        base_url = active_config.get("base_url")
        model = active_config.get("model")
        
        if not provider or not model or not api_key:
            log.error("[市场分析定时任务] admin 用户 LLM 配置不完整 (provider/model/api_key 缺失)")
            return {"success": False, "reason": "invalid_llm_config"}
        
        # 获取 LLM 服务并执行异步市场分析
        llm_service = llm_manager.get_service(
            provider=provider,
            model=model,
            api_key=api_key,
            base_url=base_url
        )
        
        analysis_result = asyncio.run(llm_service.analyze_market(market_bundle))
        
        if not analysis_result.get("success"):
            log.error(f"[市场分析定时任务] LLM 分析失败: {analysis_result.get('error')}")
            return {"success": False, "reason": "llm_analysis_failed", "error": analysis_result.get("error")}
        
        analysis = analysis_result.get("analysis", {})
        actual_analysis_date = market_bundle.get("analysis_date")
        if not actual_analysis_date:
            log.error("[市场分析定时任务] 分析结果缺少 analysis_date")
            return {"success": False, "reason": "missing_analysis_date"}
        
        result = {
            "success": True,
            "analysis_date": actual_analysis_date,
            "mood": analysis.get("mood", "市场情绪复杂"),
            "summary": analysis.get("summary", "市场分析结果"),
            "keyPoints": analysis.get("keyPoints", []),
            "outlook": analysis.get("outlook", "请关注市场动态"),
            "riskLevel": analysis.get("riskLevel", "medium"),
            "riskNote": analysis.get("riskNote", "注意投资风险"),
            # 新增: 连板天梯相关分析字段
            "ladderInsight": analysis.get("ladderInsight", ""),
            "mainlineAnalysis": analysis.get("mainlineAnalysis", ""),
            "analysis_type": "market_overview",
            "provider": analysis_result.get("provider"),
            "model": analysis_result.get("model"),
            "cache_info": {
                'from_cache': False,
                'cache_date': actual_analysis_date,
                'created_at': datetime.now(),
                'reason': "定时任务生成的分析结果"
            }
        }
        
        cache_success = market_analysis_cache.save_analysis(result, actual_analysis_date)
        if cache_success:
            log.info(f"[市场分析定时任务] 分析结果已缓存到数据库，日期: {actual_analysis_date}")
        else:
            log.error("[市场分析定时任务] 缓存保存失败，但分析正常返回")
        
        return {"success": True, "analysis_date": actual_analysis_date}
    except Exception as e:
        log.error(f"[市场分析定时任务] 执行失败: {e}", exc_info=True)
        return {"success": False, "error": str(e)}