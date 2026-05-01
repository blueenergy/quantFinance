from fastapi import APIRouter, Depends, HTTPException, Body
from typing import List, Dict, Any
from auth import get_current_admin_user, log_admin_action
from db import get_db_collections
from scheduler import sync_job, scheduler, DEFAULT_SCHEDULES
from datetime import datetime

router = APIRouter()

@router.get("/jobs", summary="获取所有定时任务配置")
def get_scheduler_jobs(admin_user: dict = Depends(get_current_admin_user)):
    """获取所有定时任务的状态和配置"""
    try:
        collections = get_db_collections()
        config_coll = collections.get("schedule_config")
        
        # Get all stored configs
        stored_configs = {doc['job_id']: doc for doc in config_coll.find({})}
        
        jobs = []
        for job_id, default_config in DEFAULT_SCHEDULES.items():
            stored = stored_configs.get(job_id)
            if stored:
                config = {**default_config, **stored, "job_id": job_id}
                config.pop("_id", None)
            else:
                config = default_config.copy()
                config["job_id"] = job_id

            # Check actual APScheduler status
            aps_job = scheduler.get_job(job_id)
            is_running = aps_job is not None
            next_run_time = aps_job.next_run_time.isoformat() if is_running and aps_job.next_run_time else None
            
            # Remove MongoDB _id for JSON serialization
            if "_id" in config:
                config.pop("_id")
                
            jobs.append({
                "job_id": job_id,
                "name": config.get("name"),
                "type": config.get("type"),
                "config": {
                    "seconds": config.get("seconds"),
                    "hour": config.get("hour"),
                    "minute": config.get("minute"),
                    "day_of_week": config.get("day_of_week")
                },
                "is_active": config.get("is_active", default_config.get("is_active", True)),
                "is_running": is_running,
                "next_run_time": next_run_time,
                "updated_at": config.get("updated_at", "").isoformat() if isinstance(config.get("updated_at"), datetime) else None
            })
            
        return {
            "success": True,
            "data": jobs
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取任务列表失败: {str(e)}")

@router.put("/jobs/{job_id}", summary="修改定时任务配置")
def update_scheduler_job(
    job_id: str,
    payload: dict = Body(...),
    admin_user: dict = Depends(get_current_admin_user)
):
    """动态修改定时任务的执行规则"""
    try:
        if job_id not in DEFAULT_SCHEDULES:
            raise HTTPException(status_code=404, detail="未找到该任务")
            
        collections = get_db_collections()
        config_coll = collections.get("schedule_config")
        
        # Prepare merged config（与 apply_schedule_config 一致：先合并默认值，避免 DB 文档缺字段导致 trigger 构建失败）
        base = DEFAULT_SCHEDULES[job_id].copy()
        stored = config_coll.find_one({"job_id": job_id})
        current_config = {**base, **(stored or {}), "job_id": job_id}
        current_config.pop("_id", None)
        if current_config.get("type") not in ("interval", "cron"):
            current_config["type"] = base["type"]

        # Update fields from payload
        if "is_active" in payload:
            current_config["is_active"] = payload["is_active"]
            
        if "config" in payload:
            cfg = payload["config"]
            job_type = current_config.get("type")
            if job_type == "interval" and "seconds" in cfg:
                current_config["seconds"] = int(cfg["seconds"])
            elif job_type == "cron":
                if "hour" in cfg: current_config["hour"] = cfg["hour"]
                if "minute" in cfg: current_config["minute"] = cfg["minute"]
                if "day_of_week" in cfg: current_config["day_of_week"] = cfg["day_of_week"]
                
        current_config["updated_at"] = datetime.now()
        
        # Save to DB
        config_coll.update_one(
            {"job_id": job_id},
            {"$set": current_config},
            upsert=True
        )
        
        # Sync immediately to memory
        sync_success = sync_job(job_id, current_config)
        
        log_admin_action(
            admin_user=admin_user,
            action="UPDATE_SCHEDULER_JOB",
            details=f"Updated job {job_id} configuration"
        )
        
        if not sync_success:
             raise HTTPException(status_code=500, detail="配置已保存，但同步到内存调度器时失败，可能参数不合法")
             
        return {
            "success": True,
            "message": "调度规则修改成功并已生效"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"更新任务失败: {str(e)}")
