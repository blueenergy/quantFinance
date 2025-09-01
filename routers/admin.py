from bson import ObjectId
from fastapi import APIRouter, Depends, Query, HTTPException
from auth import get_current_admin_user, log_admin_action, create_admin_user
from db import get_db_collections
from datetime import timedelta, datetime



router = APIRouter()
collections = get_db_collections()
users_collection = collections["users"]
user_watchlists_collection = collections["user_watchlists"]
login_logs_collection = collections["login_logs"]
admin_logs_collection = collections["admin_logs"]
analysis_history_collection = collections["analysis_history"]

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
        users = list(users_collection.find(
            query,
            {"hashed_password": 0}  # 不返回密码哈希
        ).sort("created_at", -1).skip(skip).limit(page_size))
        
        # 处理用户数据
        for user in users:
            user["id"] = str(user["_id"])
            del user["_id"]
        
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
        
        user["id"] = str(user["_id"])
        del user["_id"]
        
        # 获取用户的自选股信息
        watchlist_doc = user_watchlists_collection.find_one({"user_id": user["id"]})
        watchlist_count = len(watchlist_doc.get("symbols", [])) if watchlist_doc else 0
        
        # 获取用户的最近登录记录
        recent_logins = list(login_logs_collection.find(
            {"user_id": user["id"]},
            sort=[("login_time", -1)]
        ).limit(5))
        
        for login in recent_logins:
            login["_id"] = str(login["_id"])
        
        return {
            "success": True,
            "data": {
                "user": user,
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
        logs = list(login_logs_collection.find(query).sort("login_time", -1).skip(skip).limit(page_size))
        
        # 处理日志数据
        for log in logs:
            log["_id"] = str(log["_id"])
        
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

@router.put("/users/{user_id}/status")
def update_user_status(
    user_id: str,
    is_active: bool,
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
        
        # 更新用户状态
        result = users_collection.update_one(
            {"_id": ObjectId(user_id)},
            {"$set": {"is_active": is_active}}
        )
        
        if result.modified_count == 0:
            raise HTTPException(status_code=400, detail="更新失败")
        
        # 记录管理员操作
        log_admin_action(
            admin_user=admin_user,
            action="UPDATE_USER_STATUS",
            target_user_id=user_id,
            target_username=target_user.get("username"),
            details=f"Set user active status to {is_active}"
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
        total = analysis_history_collection.count_documents(query)
        # 分页查询
        skip = (page - 1) * page_size
        logs = list(analysis_history_collection.find(query).sort("timestamp", -1).skip(skip).limit(page_size))
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