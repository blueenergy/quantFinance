from bson import ObjectId
from fastapi import APIRouter, Depends, Query, HTTPException, Body
from typing import List
from auth import get_current_admin_user, log_admin_action, create_admin_user
from db import get_db_collections, get_analyzer_db
from datetime import timedelta, datetime
from utils.stock_utils import serialize_doc
import time
from models.all_models import (
    WorkerNodeRegisterRequest,
    WorkerNodeHeartbeat,
    WorkerGroupConfig,
    WorkerGroupsReport,
)
from task_router import TaskRouter



router = APIRouter()
collections = get_db_collections()
users_collection = collections["users"]
user_watchlists_collection = collections["user_watchlists"]
login_logs_collection = collections["login_logs"]
admin_logs_collection = collections["admin_logs"]
analysis_history_collection = get_analyzer_db()["analysis_history"]

def get_limit_up_analysis_collection():
    """Lazy access to limit_up_analysis collection"""
    return get_analyzer_db()["limit_up_analysis"]

@router.get("/users")
def get_all_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: str = Query(""),
    admin_user: dict = Depends(get_current_admin_user)
):
    """管理员获取所有用户列表"""
    try:
        # 记录管理员操作
        log_admin_action(
            admin_user=admin_user,
            action="VIEW_USERS",
            details=f"Page: {page}, Search: {search}"
        )
        
        # 构建查询条件
        query = {}
        if search:
            query = {
                "$or": [
                    {"username": {"$regex": search, "$options": "i"}},
                    {"email": {"$regex": search, "$options": "i"}},
                    {"full_name": {"$regex": search, "$options": "i"}}
                ]
            }
        
        # 计算总数
        total = users_collection.count_documents(query)
        
        # 分页查询
        skip = (page - 1) * page_size
        users_cursor = users_collection.find(
            query,
            {"hashed_password": 0}  # 不返回密码哈希
        ).sort("created_at", -1).skip(skip).limit(page_size)
        
        # 处理用户数据，使用serialize_doc处理ObjectId和datetime
        users = []
        for user in users_cursor:
            user_data = serialize_doc(user)
            user_data["id"] = user_data.pop("_id")  # 将_id重命名为id
            users.append(user_data)
        
        return {
            "success": True,
            "data": {
                "users": users,
                "pagination": {
                    "page": page,
                    "page_size": page_size,
                    "total": total,
                    "total_pages": (total + page_size - 1) // page_size
                }
            }
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取用户列表失败: {str(e)}")

@router.get("/users/{user_id}")
def get_user_detail(
    user_id: str,
    admin_user: dict = Depends(get_current_admin_user)
    ):
    """管理员获取用户详细信息"""
    try:
        collections = get_db_collections()
        users_collection = collections["users"]
        user_watchlists_collection = collections["user_watchlists"]
        login_logs_collection = collections["login_logs"]
        # 记录管理员操作
        log_admin_action(
            admin_user=admin_user,
            action="VIEW_USER_DETAIL",
            target_user_id=user_id,
            details=f"View user {user_id} details"
        )
        
        # 查找用户
        user = users_collection.find_one(
            {"_id": ObjectId(user_id)},
            {"hashed_password": 0}
        )
        
        if not user:
            raise HTTPException(status_code=404, detail="用户不存在")
        
        # 使用serialize_doc处理ObjectId和datetime
        user_data = serialize_doc(user)
        user_data["id"] = user_data.pop("_id")
        
        # 获取用户的自选股信息
        watchlist_doc = user_watchlists_collection.find_one({"user_id": user_data["id"]})
        watchlist_count = len(watchlist_doc.get("symbols", [])) if watchlist_doc else 0
        
        # 获取用户的最近登录记录
        recent_logins_cursor = login_logs_collection.find(
            {"user_id": user_data["id"]},
            sort=[("login_time", -1)]
        ).limit(5)
        
        recent_logins = [serialize_doc(log) for log in recent_logins_cursor]
        
        return {
            "success": True,
            "data": {
                "user": user_data,
                "statistics": {
                    "watchlist_count": watchlist_count,
                    "recent_logins": recent_logins
                }
            }
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取用户详情失败: {str(e)}")

@router.get("/login-logs")
def get_login_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    user_id: str = Query(""),
    success_only: bool = Query(False),
    admin_user: dict = Depends(get_current_admin_user)
    ):
    """管理员获取登录日志"""
    try:
        collections = get_db_collections()
        login_logs_collection = collections["login_logs"]
        # 记录管理员操作
        log_admin_action(
            admin_user=admin_user,
            action="VIEW_LOGIN_LOGS",
            details=f"Page: {page}, UserID: {user_id}, SuccessOnly: {success_only}"
        )
        
        # 构建查询条件
        query = {}
        if user_id:
            query["user_id"] = user_id
        if success_only:
            query["success"] = True
        
        # 计算总数
        total = login_logs_collection.count_documents(query)
        
        # 分页查询
        skip = (page - 1) * page_size
        logs_cursor = login_logs_collection.find(query).sort("login_time", -1).skip(skip).limit(page_size)
        
        # 处理日志数据，使用serialize_doc处理ObjectId和datetime
        logs = [serialize_doc(log) for log in logs_cursor]
        
        return {
            "success": True,
            "data": {
                "logs": logs,
                "pagination": {
                    "page": page,
                    "page_size": page_size,
                    "total": total,
                    "total_pages": (total + page_size - 1) // page_size
                }
            }
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取登录日志失败: {str(e)}")


@router.get("/users/pending")
def get_pending_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    admin_user: dict = Depends(get_current_admin_user)
):
    """管理员获取待审批的用户列表"""
    try:
        # 记录管理员操作
        log_admin_action(
            admin_user=admin_user,
            action="VIEW_PENDING_USERS",
            details=f"Page: {page}"
        )
        
        # 查询待审批的用户
        query = {"approval_status": "pending"}
        
        # 计算总数
        total = users_collection.count_documents(query)
        
        # 分页查询
        skip = (page - 1) * page_size
        users_cursor = users_collection.find(
            query,
            {"hashed_password": 0}  # 不返回密码哈希
        ).sort("created_at", -1).skip(skip).limit(page_size)
        
        # 处理用户数据，使用serialize_doc处理ObjectId和datetime
        users = []
        for user in users_cursor:
            user_data = serialize_doc(user)
            user_data["id"] = user_data.pop("_id")  # 将_id重命名为id
            users.append(user_data)
        
        return {
            "success": True,
            "data": {
                "users": users,
                "pagination": {
                    "page": page,
                    "page_size": page_size,
                    "total": total,
                    "total_pages": (total + page_size - 1) // page_size
                }
            }
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取待审批用户列表失败: {str(e)}")


@router.post("/users/{user_id}/approve")
def approve_user(
    user_id: str,
    admin_user: dict = Depends(get_current_admin_user)
):
    """管理员批准用户注册"""
    try:
        # 查找用户
        user = users_collection.find_one({"_id": ObjectId(user_id)})
        
        if not user:
            raise HTTPException(status_code=404, detail="用户不存在")
        
        if user.get("approval_status") == "approved":
            raise HTTPException(status_code=400, detail="用户已经被批准")
        
        # 更新用户状态
        now = datetime.utcnow()
        update_result = users_collection.update_one(
            {"_id": ObjectId(user_id)},
            {
                "$set": {
                    "approval_status": "approved",
                    "approved_by": admin_user["username"],
                    "approved_at": now,
                    "is_active": True  # 激活用户
                }
            }
        )
        
        if update_result.modified_count == 0:
            raise HTTPException(status_code=500, detail="更新用户状态失败")
        
        # 记录管理员操作
        log_admin_action(
            admin_user=admin_user,
            action="APPROVE_USER",
            target_user_id=user_id,
            details=f"Approved user {user.get('username')} ({user.get('email')})"
        )
        
        return {
            "success": True,
            "message": f"已成功批准用户 {user.get('username')}",
            "data": {
                "user_id": user_id,
                "username": user.get("username"),
                "approved_at": now.isoformat()
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"批准用户失败: {str(e)}")


@router.post("/users/{user_id}/reject")
def reject_user(
    user_id: str,
    reason: str = Body("", embed=True),
    admin_user: dict = Depends(get_current_admin_user)
):
    """管理员拒绝用户注册"""
    try:
        # 查找用户
        user = users_collection.find_one({"_id": ObjectId(user_id)})
        
        if not user:
            raise HTTPException(status_code=404, detail="用户不存在")
        
        if user.get("approval_status") == "rejected":
            raise HTTPException(status_code=400, detail="用户已经被拒绝")
        
        # 更新用户状态
        now = datetime.utcnow()
        update_data = {
            "approval_status": "rejected",
            "approved_by": admin_user["username"],
            "approved_at": now,
            "is_active": False  # 保持未激活状态
        }
        
        if reason:
            update_data["rejection_reason"] = reason
        
        update_result = users_collection.update_one(
            {"_id": ObjectId(user_id)},
            {"$set": update_data}
        )
        
        if update_result.modified_count == 0:
            raise HTTPException(status_code=500, detail="更新用户状态失败")
        
        # 记录管理员操作
        log_admin_action(
            admin_user=admin_user,
            action="REJECT_USER",
            target_user_id=user_id,
            details=f"Rejected user {user.get('username')} ({user.get('email')}). Reason: {reason}"
        )
        
        return {
            "success": True,
            "message": f"已拒绝用户 {user.get('username')}",
            "data": {
                "user_id": user_id,
                "username": user.get("username"),
                "rejected_at": now.isoformat(),
                "reason": reason
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"拒绝用户失败: {str(e)}")


@router.post("/worker-nodes/register")
def register_worker_node(payload: WorkerNodeRegisterRequest):
    """Worker 节点注册/更新接口（供 Node Agent 调用）"""
    try:
        collections = get_db_collections()
        worker_nodes = collections["worker_nodes"]
        now = datetime.utcnow()

        existing = worker_nodes.find_one({"node_id": payload.node_id})
        doc = {
            "node_id": payload.node_id,
            "host": payload.host,
            "labels": payload.labels or {},
            "capacity": payload.capacity or {},
            "last_heartbeat": now,
            "status": "online",
            "updated_at": now,
        }
        if existing:
            worker_nodes.update_one({"_id": existing["_id"]}, {"$set": doc})
        else:
            doc["created_at"] = now
            worker_nodes.insert_one(doc)

        return {"success": True, "data": {"node_id": payload.node_id}}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Worker 节点注册失败: {str(e)}")


@router.post("/worker-nodes/heartbeat")
def worker_node_heartbeat(payload: WorkerNodeHeartbeat):
    """Worker 节点心跳上报接口"""
    try:
        collections = get_db_collections()
        worker_nodes = collections["worker_nodes"]
        now = datetime.utcnow()

        update = {
            "last_heartbeat": datetime.fromtimestamp(payload.timestamp),
            "status": "online",
            "processes": payload.processes or [],
            "updated_at": now,
        }
        result = worker_nodes.update_one(
            {"node_id": payload.node_id}, {"$set": update}, upsert=True
        )

        return {"success": True, "data": {"matched": result.matched_count}}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Worker 节点心跳失败: {str(e)}")


@router.get("/worker-groups/assignments")
def get_worker_group_assignments(node_id: str = Query(..., description="节点ID")):
    """Node Agent 拉取分配给自己的 Worker 组配置"""
    try:
        collections = get_db_collections()
        worker_groups = collections["worker_groups"]

        docs = list(worker_groups.find({"node_id": node_id}))
        groups = []
        for d in docs:
            # 返回所有分配给该节点的组，不管状态是否同步
            groups.append({
                "group_id": d.get("group_id"),
                "node_id": d.get("node_id"),
                "symbols": d.get("symbols", []),
                "user_id": d.get("user_id"),
                "mode": d.get("mode", "live"),
                "desired_state": d.get("desired_state", "running"),
                "current_state": d.get("current_state"),
                "config": d.get("config", {}),
            })

        return {"success": True, "data": {"groups": groups}}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取 Worker 组分配失败: {str(e)}")


@router.post("/worker-groups/report")
def report_worker_groups(payload: WorkerGroupsReport):
    """Node Agent 上报 Worker 组当前状态"""
    try:
        collections = get_db_collections()
        worker_groups = collections["worker_groups"]
        now = datetime.utcnow()

        updated = 0
        for g in payload.groups:
            update = {
                "node_id": payload.node_id,
                "symbols": g.symbols,
                "user_id": g.user_id,
                "mode": g.mode,
                "desired_state": g.desired_state,
                "current_state": g.current_state,
                "config": g.config or {},
                "updated_at": now,
            }
            result = worker_groups.update_one(
                {"group_id": g.group_id}, {"$set": update}, upsert=True
            )
            updated += result.matched_count or 1

        return {"success": True, "data": {"updated": updated}}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"上报 Worker 组状态失败: {str(e)}")


@router.get("/worker-nodes/status")
def get_worker_nodes_status(admin_user: dict = Depends(get_current_admin_user)):
    """管理员查看所有 Worker 节点状态"""
    try:
        collections = get_db_collections()
        worker_nodes = collections["worker_nodes"]
        
        nodes = list(worker_nodes.find({}))
        now = datetime.utcnow()
        
        online_count = 0
        total_processes = 0
        
        for node in nodes:
            node["_id"] = str(node["_id"])
            last_hb = node.get("last_heartbeat")
            
            # 判断在线状态：5分钟内有心跳视为在线
            if last_hb and (now - last_hb).total_seconds() < 300:
                node["status"] = "online"
                online_count += 1
            else:
                node["status"] = "offline"
            
            # 统计进程数
            processes = node.get("processes", [])
            node["process_count"] = len(processes)
            total_processes += len(processes)
        
        summary = {
            "total_nodes": len(nodes),
            "online_nodes": online_count,
            "total_processes": total_processes,
        }
        
        return {"success": True, "data": {"nodes": nodes, "summary": summary}}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取节点状态失败: {str(e)}")


@router.get("/worker-groups/status")
def get_worker_groups_status(admin_user: dict = Depends(get_current_admin_user)):
    """管理员查看所有 Worker 组状态"""
    try:
        collections = get_db_collections()
        worker_groups = collections["worker_groups"]
        
        groups = list(worker_groups.find({}))
        
        running_count = 0
        stopped_count = 0
        
        for group in groups:
            group["_id"] = str(group["_id"])
            current_state = group.get("current_state")
            
            if current_state == "running":
                running_count += 1
            elif current_state == "stopped":
                stopped_count += 1
        
        summary = {
            "total_groups": len(groups),
            "running_groups": running_count,
            "stopped_groups": stopped_count,
        }
        
        return {"success": True, "data": {"groups": groups, "summary": summary}}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取 Worker 组状态失败: {str(e)}")


@router.get("/statistics")
def get_admin_statistics(admin_user: dict = Depends(get_current_admin_user)):
    """管理员获取系统统计信息"""
    try:
        collections = get_db_collections()
        users_collection = collections["users"]
        login_logs_collection = collections["login_logs"]
        user_watchlists_collection = collections["user_watchlists"]
        # 记录管理员操作
        log_admin_action(
            admin_user=admin_user,
            action="VIEW_STATISTICS",
            details="View system statistics"
        )
        
        # 用户统计
        total_users = users_collection.count_documents({})
        active_users = users_collection.count_documents({"is_active": True})
        admin_users = users_collection.count_documents({"is_admin": True})
        
        # 今日新注册用户
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        today_new_users = users_collection.count_documents({
            "created_at": {"$gte": today}
        })
        
        # 登录统计
        total_logins = login_logs_collection.count_documents({})
        successful_logins = login_logs_collection.count_documents({"success": True})
        failed_logins = login_logs_collection.count_documents({"success": False})
        
        # 今日登录统计
        today_logins = login_logs_collection.count_documents({
            "login_time": {"$gte": today}
        })
        today_successful_logins = login_logs_collection.count_documents({
            "login_time": {"$gte": today},
            "success": True
        })
        
        # 自选股统计
        total_watchlists = user_watchlists_collection.count_documents({})
        
        # 最近7天的登录趋势
        seven_days_ago = datetime.now() - timedelta(days=7)
        daily_logins = []
        for i in range(7):
            day_start = seven_days_ago + timedelta(days=i)
            day_end = day_start + timedelta(days=1)
            day_count = login_logs_collection.count_documents({
                "login_time": {"$gte": day_start, "$lt": day_end},
                "success": True
            })
            daily_logins.append({
                "date": day_start.strftime("%Y-%m-%d"),
                "count": day_count
            })
        
        return {
            "success": True,
            "data": {
                "users": {
                    "total": total_users,
                    "active": active_users,
                    "admin": admin_users,
                    "today_new": today_new_users
                },
                "logins": {
                    "total": total_logins,
                    "successful": successful_logins,
                    "failed": failed_logins,
                    "today_total": today_logins,
                    "today_successful": today_successful_logins
                },
                "watchlists": {
                    "total": total_watchlists
                },
                "trends": {
                    "daily_logins": daily_logins
                }
            }
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取统计信息失败: {str(e)}")

@router.get("/data-collection/status", summary="获取数据采集状态")
def get_data_collection_status(admin_user: dict = Depends(get_current_admin_user)):
    """管理员查看数据采集状态
    
    返回：
    - 正在采集的股票列表
    - 每只股票的最新数据时间
    - 采集统计信息
    """
    try:
        collections = get_db_collections()
        minute_bars_coll = collections["minute_bars"]
        
        # 记录管理员操作
        log_admin_action(
            admin_user=admin_user,
            action="VIEW_DATA_COLLECTION_STATUS",
            details="View data collection status"
        )
        
        # 统计每只股票的分钟K线数据
        pipeline = [
            {
                "$group": {
                    "_id": "$symbol",
                    "total_bars": {"$sum": 1},
                    "latest_date": {"$max": "$trade_date"},
                    "earliest_date": {"$min": "$trade_date"}
                }
            },
            {"$sort": {"latest_date": -1}}
        ]
        
        stocks_data = list(minute_bars_coll.aggregate(pipeline))
        
        # 处理数据
        stocks_list = []
        today = datetime.now().strftime("%Y%m%d")
        active_today = 0
        
        for stock in stocks_data:
            symbol = stock["_id"]
            total_bars = stock["total_bars"]
            latest_date = stock.get("latest_date", "")
            earliest_date = stock.get("earliest_date", "")
            
            # 判断是否今天有数据
            is_active_today = latest_date.startswith(today) if latest_date else False
            if is_active_today:
                active_today += 1
            
            # 计算最后更新时间距离现在多久
            if latest_date and len(latest_date) >= 12:
                try:
                    latest_dt = datetime.strptime(latest_date[:12], "%Y%m%d%H%M")
                    minutes_ago = int((datetime.now() - latest_dt).total_seconds() / 60)
                except:
                    minutes_ago = None
            else:
                minutes_ago = None
            
            stocks_list.append({
                "symbol": symbol,
                "total_bars": total_bars,
                "latest_date": latest_date,
                "earliest_date": earliest_date,
                "is_active_today": is_active_today,
                "minutes_since_update": minutes_ago
            })
        
        # 总体统计
        total_stocks = len(stocks_list)
        total_bars = sum(s["total_bars"] for s in stocks_list)
        
        # 今天的数据量
        today_bars = minute_bars_coll.count_documents({
            "trade_date": {"$regex": f"^{today}"}
        })
        
        return {
            "success": True,
            "data": {
                "summary": {
                    "total_stocks": total_stocks,
                    "active_today": active_today,
                    "total_bars": total_bars,
                    "today_bars": today_bars,
                    "last_update": datetime.now().isoformat()
                },
                "stocks": stocks_list
            }
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取数据采集状态失败: {str(e)}")

@router.put("/users/{user_id}/status")
def update_user_status(
    user_id: str,
    is_active: bool = Body(..., embed=True),
    admin_user: dict = Depends(get_current_admin_user)
    ):
    """管理员更新用户状态（激活/禁用）"""
    try:
        collections = get_db_collections()
        users_collection = collections["users"]
        # 不能禁用自己
        if user_id == admin_user["id"]:
            raise HTTPException(status_code=400, detail="不能禁用自己的账户")
        
        # 查找目标用户
        target_user = users_collection.find_one({"_id": ObjectId(user_id)})
        if not target_user:
            raise HTTPException(status_code=404, detail="用户不存在")
        
        # 准备更新数据
        update_data = {"is_active": is_active}
        
        # 如果用户处于待审批状态，且正在激活，则自动批准
        if is_active and target_user.get("approval_status") == "pending":
            now = datetime.utcnow()
            update_data.update({
                "approval_status": "approved",
                "approved_by": admin_user["username"],
                "approved_at": now
            })
        
        # 更新用户状态
        result = users_collection.update_one(
            {"_id": ObjectId(user_id)},
            {"$set": update_data}
        )
        
        if result.modified_count == 0:
            raise HTTPException(status_code=400, detail="更新失败")
        
        # 记录管理员操作
        action_detail = f"Set user active status to {is_active}"
        if is_active and target_user.get("approval_status") == "pending":
            action_detail += " and approved user"
        
        log_admin_action(
            admin_user=admin_user,
            action="UPDATE_USER_STATUS",
            target_user_id=user_id,
            target_username=target_user.get("username"),
            details=action_detail
        )
        
        return {
            "success": True,
            "message": f"用户状态已更新为 {'激活' if is_active else '禁用'}"
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"更新用户状态失败: {str(e)}")


@router.get("/actions")
def get_admin_actions(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    admin_user: dict = Depends(get_current_admin_user)
):
    """管理员获取管理员操作日志"""
    try:
        collections = get_db_collections()
        admin_logs_collection = collections["admin_logs"]
        # 记录管理员操作
        log_admin_action(
            admin_user=admin_user,
            action="VIEW_ADMIN_LOGS",
            details=f"Page: {page}"
        )
        
        # 计算总数
        total = admin_logs_collection.count_documents({})
        
        # 分页查询
        skip = (page - 1) * page_size
        actions = list(admin_logs_collection.find({}).sort("timestamp", -1).skip(skip).limit(page_size))
        
        # 处理日志数据
        for action in actions:
            action["_id"] = str(action["_id"])
        
        return {
            "success": True,
            "data": {
                "actions": actions,
                "pagination": {
                    "page": page,
                    "page_size": page_size,
                    "total": total,
                    "total_pages": (total + page_size - 1) // page_size
                }
            }
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取管理员日志失败: {str(e)}")

@router.post("/init")
def initialize_admin():
    """初始化管理员账户（仅限初次使用）"""
    try:
        # 检查是否已有管理员
        existing_admin = users_collection.find_one({"is_admin": True})
        if existing_admin:
            raise HTTPException(status_code=400, detail="管理员账户已存在")
        
        # 创建默认管理员
        result = create_admin_user(
            username="admin",
            password="admin123",  # 默认密码，需要立即修改
            email="admin@quantfinance.com",
            full_name="系统管理员"
        )
        
        return {
            "success": True,
            "message": "管理员账户创建成功",
            "warning": "请立即修改默认密码！",
            "credentials": {
                "username": "admin",
                "password": "admin123"
            }
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"初始化管理员失败: {str(e)}")
    


@router.get("/analysis-logs")
def get_analysis_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    user_id: str = Query(""),
    symbol: str = Query(""),
    admin_user: dict = Depends(get_current_admin_user)
):
    """管理员获取AI分析日志（用户、股票、时间、服务商等）"""
    try:
        # 记录管理员操作
        log_admin_action(
            admin_user=admin_user,
            action="VIEW_ANALYSIS_LOGS",
            details=f"Page: {page}, UserID: {user_id}, Symbol: {symbol}"
        )
        # 构建查询条件
        query = {}
        if user_id:
            query["user_id"] = user_id
        if symbol:
            query["symbol"] = symbol
        # 计算总数
        limit_up_analysis = get_limit_up_analysis_collection()
        total = limit_up_analysis.count_documents(query)
        # 分页查询
        skip = (page - 1) * page_size
        logs = list(limit_up_analysis.find(query).sort("timestamp", -1).skip(skip).limit(page_size))
        for log in logs:
            log["_id"] = str(log["_id"])
        return {
            "success": True,
            "data": {
                "logs": logs,
                "pagination": {
                    "page": page,
                    "page_size": page_size,
                    "total": total
                }
            }
        }
    except Exception as e:
        return {"success": False, "message": f"获取分析日志失败: {str(e)}"}


# ==================== 任务管理 API ====================

@router.post("/tasks/create")
def create_task(
    symbols: List[str] = Body(...),
    user_id: str = Body(...),
    task_type: str = Body("realtime"),  # realtime / analysis / backtest
    mode: str = Body("live"),
    config: dict = Body(None),
    admin_user: dict = Depends(get_current_admin_user)
):
    """管理员创建任务，自动分配到合适的 Worker
    
    任务类型：
    - realtime: 实时交易（只能在私有 Worker 上运行）
    - analysis: 分析任务（优先公有 Worker）
    - backtest: 回测任务（优先公有 Worker）
    """
    try:
        # 记录管理员操作
        log_admin_action(
            admin_user=admin_user,
            action="CREATE_TASK",
            details=f"Task type: {task_type}, Symbols: {symbols}, User: {user_id}"
        )
        
        # 使用 TaskRouter 创建任务
        group = TaskRouter.create_worker_group(
            symbols=symbols,
            user_id=user_id,
            task_type=task_type,
            mode=mode,
            config=config
        )
        
        return {
            "success": True,
            "data": {
                "group": group,
                "message": f"任务已分配到节点: {group['node_id']}"
            }
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"创建任务失败: {str(e)}")


@router.get("/tasks/available-nodes")
def get_available_nodes_for_task(
    task_type: str = Query("realtime"),
    admin_user: dict = Depends(get_current_admin_user)
):
    """查询可用于指定任务类型的节点列表"""
    try:
        nodes = TaskRouter.get_available_nodes(task_type)
        
        return {
            "success": True,
            "data": {
                "nodes": nodes,
                "count": len(nodes),
                "task_type": task_type
            }
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询节点失败: {str(e)}")


@router.post("/tasks/assign")
def assign_task_to_specific_node(
    node_id: str = Body(...),
    symbols: List[str] = Body(...),
    user_id: str = Body(...),
    task_type: str = Body("realtime"),
    mode: str = Body("live"),
    config: dict = Body(None),
    admin_user: dict = Depends(get_current_admin_user)
):
    """管理员手动指定节点分配任务"""
    try:
        log_admin_action(
            admin_user=admin_user,
            action="ASSIGN_TASK",
            details=f"Node: {node_id}, Task: {task_type}, Symbols: {symbols}"
        )
        
        group = TaskRouter.assign_task_to_node(
            node_id=node_id,
            symbols=symbols,
            user_id=user_id,
            task_type=task_type,
            mode=mode,
            config=config
        )
        
        return {
            "success": True,
            "data": {
                "group": group,
                "message": f"任务已分配到节点: {node_id}"
            }
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"分配任务失败: {str(e)}")