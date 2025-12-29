"""Worker monitoring API endpoints.

Provides real-time visibility into worker activities:
- Current worker status (running/stopped)
- Today's work summary (scans, analyses, signals generated)
- Activity timeline
- Historical statistics
"""

from fastapi import APIRouter, Depends, Query, HTTPException
from typing import List, Optional, Dict, Any
from datetime import datetime, timedelta
from auth import get_current_active_user
from db import get_db_collections
from utils.stock_utils import serialize_doc
import time

router = APIRouter()


@router.get("/worker/status", summary="获取当前 Worker 运行状态")
def get_worker_status(current_user: dict = Depends(get_current_active_user)):
    """Get current status of all workers.
    
    Returns:
        - Active workers (name, symbol, strategy, uptime)
        - Worker health metrics
        - Trading schedule info
    """
    try:
        collections = get_db_collections()
        worker_status_coll = collections["worker_status"]
        
        # Get latest status from each worker
        # Worker should heartbeat every minute with status update
        now = time.time()
        cutoff_time = now - 300  # Consider alive if heartbeat within 5 min
        
        user_id = current_user["id"]
        
        # Aggregate active workers
        pipeline = [
            {"$match": {
                "user_id": user_id,  # Filter by current user
                "last_heartbeat": {"$gte": cutoff_time}
            }},
            {"$sort": {"last_heartbeat": -1}},
            {"$group": {
                "_id": "$worker_id",
                "latest": {"$first": "$$ROOT"}
            }},
            {"$replaceRoot": {"newRoot": "$latest"}}
        ]
        
        active_workers = list(worker_status_coll.aggregate(pipeline))
        
        # Enrich with uptime calculation
        for worker in active_workers:
            started_at = worker.get("started_at", now)
            worker["uptime_seconds"] = int(now - started_at)
            worker["status_age_seconds"] = int(now - worker.get("last_heartbeat", now))
        
        # Get trading schedule info
        from datetime import datetime as dt
        current_hour = dt.now().hour
        current_minute = dt.now().minute
        
        is_market_hours = (
            (current_hour == 9 and current_minute >= 30) or
            (current_hour > 9 and current_hour < 11) or
            (current_hour == 11 and current_minute <= 30) or
            (current_hour == 13) or
            (current_hour == 14)
        )
        
        return {
            "success": True,
            "data": {
                "active_workers": [serialize_doc(w) for w in active_workers],
                "total_active": len(active_workers),
                "market_status": "open" if is_market_hours else "closed",
                "timestamp": now
            }
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取状态失败: {str(e)}")


@router.get("/worker/activity/today", summary="获取今日 Worker 工作汇总")
def get_today_worker_activity(
    current_user: dict = Depends(get_current_active_user)
):
    """Get today's worker activity summary.
    
    Returns:
        - Total scans performed
        - Signals generated
        - Trades executed
        - Activity timeline
    """
    try:
        collections = get_db_collections()
        activity_coll = collections["worker_activity"]
        signals_coll = collections["trade_signals"]
        executions_coll = collections["trade_executions"]
        
        # Get today's date range
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow = today + timedelta(days=1)
        today_ts = today.timestamp()
        tomorrow_ts = tomorrow.timestamp()
        
        user_id = current_user["id"]
        
        # Get today's activities
        activities = list(activity_coll.find({
            "user_id": user_id,
            "timestamp": {"$gte": today_ts, "$lt": tomorrow_ts}
        }).sort("timestamp", 1))
        
        # Count signals generated today
        signals_today = signals_coll.count_documents({
            "user_id": user_id,
            "timestamp": {"$gte": today_ts, "$lt": tomorrow_ts}
        })
        
        # Count executions today
        executions_today = executions_coll.count_documents({
            "user_id": user_id,
            "timestamp": {"$gte": today_ts, "$lt": tomorrow_ts},
            "mode": "live"
        })
        
        # Group activities by type
        activity_summary = {
            "scan": 0,
            "analysis": 0,
            "signal_check": 0,
            "order_submitted": 0
        }
        
        for act in activities:
            activity_type = act.get("activity_type", "unknown")
            if activity_type in activity_summary:
                activity_summary[activity_type] += 1
        
        # Build timeline (hourly buckets)
        timeline = {}
        for act in activities:
            ts = act.get("timestamp", today_ts)
            hour = datetime.fromtimestamp(ts).strftime("%H:00")
            if hour not in timeline:
                timeline[hour] = {
                    "scans": 0,
                    "signals": 0,
                    "trades": 0
                }
            
            activity_type = act.get("activity_type")
            if activity_type == "scan":
                timeline[hour]["scans"] += 1
        
        # Add signals to timeline
        signals_by_hour = signals_coll.aggregate([
            {
                "$match": {
                    "user_id": user_id,
                    "timestamp": {"$gte": today_ts, "$lt": tomorrow_ts}
                }
            },
            {
                "$project": {
                    "hour": {
                        "$dateToString": {
                            "format": "%H:00",
                            "date": {"$toDate": {"$multiply": ["$timestamp", 1000]}}
                        }
                    }
                }
            },
            {
                "$group": {
                    "_id": "$hour",
                    "count": {"$sum": 1}
                }
            }
        ])
        
        for item in signals_by_hour:
            hour = item["_id"]
            if hour not in timeline:
                timeline[hour] = {"scans": 0, "signals": 0, "trades": 0}
            timeline[hour]["signals"] = item["count"]
        
        # Convert timeline to sorted list
        timeline_list = [
            {"hour": hour, **data}
            for hour, data in sorted(timeline.items())
        ]
        
        return {
            "success": True,
            "data": {
                "summary": {
                    "total_scans": activity_summary["scan"],
                    "total_analyses": activity_summary["analysis"],
                    "signals_generated": signals_today,
                    "trades_executed": executions_today,
                },
                "timeline": timeline_list,
                "activities": [serialize_doc(act) for act in activities[-50:]],  # Last 50 activities
            }
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取活动数据失败: {str(e)}")


@router.get("/worker/activity/stats", summary="获取 Worker 统计数据")
def get_worker_stats(
    days: int = Query(7, ge=1, le=30, description="统计天数"),
    current_user: dict = Depends(get_current_active_user)
):
    """Get worker statistics over time.
    
    Returns:
        - Daily work summary
        - Most active strategies
        - Most monitored symbols
    """
    try:
        collections = get_db_collections()
        activity_coll = collections["worker_activity"]
        signals_coll = collections["trade_signals"]
        
        # Get date range
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)
        start_ts = start_date.timestamp()
        end_ts = end_date.timestamp()
        
        user_id = current_user["id"]
        
        # Aggregate daily activities
        daily_pipeline = [
            {
                "$match": {
                    "user_id": user_id,
                    "timestamp": {"$gte": start_ts, "$lt": end_ts}
                }
            },
            {
                "$project": {
                    "date": {
                        "$dateToString": {
                            "format": "%Y-%m-%d",
                            "date": {"$toDate": {"$multiply": ["$timestamp", 1000]}}
                        }
                    },
                    "activity_type": 1
                }
            },
            {
                "$group": {
                    "_id": {
                        "date": "$date",
                        "type": "$activity_type"
                    },
                    "count": {"$sum": 1}
                }
            },
            {"$sort": {"_id.date": 1}}
        ]
        
        daily_data = list(activity_coll.aggregate(daily_pipeline))
        
        # Restructure daily data
        daily_summary = {}
        for item in daily_data:
            date = item["_id"]["date"]
            activity_type = item["_id"]["type"]
            count = item["count"]
            
            if date not in daily_summary:
                daily_summary[date] = {"scans": 0, "analyses": 0, "signals": 0}
            
            if activity_type == "scan":
                daily_summary[date]["scans"] = count
            elif activity_type == "analysis":
                daily_summary[date]["analyses"] = count
        
        # Get most active strategies
        strategy_pipeline = [
            {
                "$match": {
                    "user_id": user_id,
                    "timestamp": {"$gte": start_ts, "$lt": end_ts}
                }
            },
            {
                "$group": {
                    "_id": "$strategy",
                    "signal_count": {"$sum": 1}
                }
            },
            {"$sort": {"signal_count": -1}},
            {"$limit": 10}
        ]
        
        top_strategies = list(signals_coll.aggregate(strategy_pipeline))
        
        # Get most monitored symbols
        symbol_pipeline = [
            {
                "$match": {
                    "user_id": user_id,
                    "timestamp": {"$gte": start_ts, "$lt": end_ts}
                }
            },
            {
                "$group": {
                    "_id": "$symbol",
                    "activity_count": {"$sum": 1}
                }
            },
            {"$sort": {"activity_count": -1}},
            {"$limit": 10}
        ]
        
        top_symbols = list(activity_coll.aggregate(symbol_pipeline))
        
        return {
            "success": True,
            "data": {
                "daily_summary": [
                    {"date": date, **stats}
                    for date, stats in sorted(daily_summary.items())
                ],
                "top_strategies": [
                    {"strategy": item["_id"], "signals": item["signal_count"]}
                    for item in top_strategies
                ],
                "top_symbols": [
                    {"symbol": item["_id"], "activities": item["activity_count"]}
                    for item in top_symbols
                ]
            }
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取统计数据失败: {str(e)}")


@router.post("/worker/heartbeat", summary="Worker 心跳上报（内部使用）")
def worker_heartbeat(
    payload: Dict[str, Any],
    current_user: dict = Depends(get_current_active_user)
):
    """Worker heartbeat endpoint (called by workers).
    
    Payload should contain:
    - worker_id: Unique worker identifier
    - symbol: Symbol being monitored
    - strategy: Strategy name
    - status: Worker status (running/idle/error)
    - metrics: Optional metrics dict
    """
    try:
        collections = get_db_collections()
        worker_status_coll = collections["worker_status"]
        
        worker_id = payload.get("worker_id")
        if not worker_id:
            raise HTTPException(status_code=400, detail="worker_id is required")
        
        user_id = current_user["id"]
        now = time.time()

        # Use user_id from payload if available, otherwise fall back to authenticated user
        payload_user_id = payload.get("user_id")
        if payload_user_id:
            user_id = payload_user_id
        else:
            user_id = current_user["id"]  # Fallback to authenticated user if no user_id in payload
    
        # Update or insert worker status
        status_doc = {
            "user_id": user_id,
            "worker_id": worker_id,
            "symbol": payload.get("symbol"),
            "strategy": payload.get("strategy"),
            "status": payload.get("status", "running"),
            "last_heartbeat": now,
            "metrics": payload.get("metrics", {}),
            "updated_at": now
        }
        
        # Set started_at only on first heartbeat
        worker_status_coll.update_one(
            {"user_id": user_id, "worker_id": worker_id},
            {
                "$set": status_doc,
                "$setOnInsert": {"started_at": now}
            },
            upsert=True
        )
        
        return {"success": True, "timestamp": now}
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"心跳上报失败: {str(e)}")


@router.post("/worker/activity/log", summary="记录 Worker 活动（内部使用）")
def log_worker_activity(
    payload: Dict[str, Any],
    current_user: dict = Depends(get_current_active_user)
):
    """Log worker activity.
    
    Payload should contain:
    - worker_id: Worker identifier
    - activity_type: scan/analysis/signal_check/order_submitted
    - symbol: Symbol (optional)
    - details: Additional details (optional)
    """
    try:
        collections = get_db_collections()
        activity_coll = collections["worker_activity"]
        
        user_id = current_user["id"]
        now = time.time()
        
        activity_doc = {
            "user_id": user_id,
            "worker_id": payload.get("worker_id"),
            "activity_type": payload.get("activity_type"),
            "symbol": payload.get("symbol"),
            "strategy": payload.get("strategy"),
            "details": payload.get("details", {}),
            "timestamp": now,
            "date": datetime.now().strftime("%Y-%m-%d")
        }
        
        activity_coll.insert_one(activity_doc)
        
        return {"success": True, "activity_id": str(activity_doc["_id"])}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"记录活动失败: {str(e)}")
