"""
用户认证模块
包含用户注册、登录、JWT令牌验证等功能
"""

from datetime import datetime, timedelta
from typing import Optional
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr
from passlib.context import CryptContext
import jwt
import logging
import os
from pymongo import MongoClient
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

_log = logging.getLogger(__name__)

# JWT配置
SECRET_KEY = os.getenv("JWT_SECRET_KEY", "your-secret-key-change-in-production")
ALGORITHM = "HS256"
# 默认 8 小时，可通过环境变量 JWT_ACCESS_EXPIRE_MINUTES 调整
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("JWT_ACCESS_EXPIRE_MINUTES", "480"))
# 邮箱验证 / 自助激活等邮件链接的 JWT 密钥（与 user_config 中邮件令牌共用此变量）
EMAIL_VERIFICATION_SECRET = os.getenv(
    "EMAIL_VERIFICATION_SECRET", "email-verification-secret-change-in-production"
)
ACCOUNT_ACTIVATION_TOKEN_DAYS = int(os.getenv("ACCOUNT_ACTIVATION_TOKEN_DAYS", "7"))
# 用户通过注册邮件自行激活时写入 approved_by
APPROVED_BY_EMAIL_SELF_SERVICE = "email_link"

# 密码加密
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# JWT Bearer token
security = HTTPBearer()

# MongoDB连接
mongo_url = os.getenv("MONGO_URI", os.getenv("MONGO_URL", "mongodb://localhost:27017"))
client = MongoClient(mongo_url)
db = client[os.getenv("MONGO_DB", "finance")]
users_collection = db.users
login_logs_collection = db.login_logs
admin_logs_collection = db.admin_logs

class UserBase(BaseModel):
    username: str
    email: EmailStr
    full_name: Optional[str] = None

class UserCreate(UserBase):
    password: str
    llm_provider: Optional[str] = None
    llm_api_key: Optional[str] = None
    llm_base_url: Optional[str] = None
    llm_model: Optional[str] = None

class UserResponse(UserBase):
    id: str
    is_active: bool
    is_admin: bool = False
    created_at: datetime
    last_login: Optional[datetime] = None
    llm_provider: Optional[str] = None
    llm_api_key: Optional[str] = None  # This will not be returned to client for security
    llm_base_url: Optional[str] = None
    llm_model: Optional[str] = None

class UserLogin(BaseModel):
    username: str
    password: str

class Token(BaseModel):
    access_token: str
    token_type: str
    user: UserResponse

class TokenData(BaseModel):
    username: Optional[str] = None

class LoginLog(BaseModel):
    user_id: str
    username: str
    ip_address: str
    user_agent: str
    login_time: datetime
    success: bool
    failure_reason: Optional[str] = None

class AdminAction(BaseModel):
    admin_id: str
    admin_username: str
    action: str
    target_user_id: Optional[str] = None
    target_username: Optional[str] = None
    details: str
    timestamp: datetime

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """验证密码"""
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    """生成密码哈希"""
    return pwd_context.hash(password)

def get_user(username: str):
    """获取用户信息"""
    user = users_collection.find_one({"username": username})
    if user:
        user["id"] = str(user["_id"])
        del user["_id"]
        # 确保返回用户的所有字段，包括LLM配置和服务等级
        user.setdefault("llm_provider", None)
        user.setdefault("llm_api_key", None)
        user.setdefault("llm_base_url", None)
        user.setdefault("llm_model", None)
        user.setdefault("service_level", "free")
        return user
    return None

def authenticate_user(username: str, password: str):
    """验证用户"""
    user = get_user(username)
    if not user:
        return False
    if not verify_password(password, user["hashed_password"]):
        return False
    return user

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    """创建访问令牌"""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """获取当前用户"""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
        token_data = TokenData(username=username)
    except jwt.PyJWTError:
        raise credentials_exception
    
    if token_data.username is None:
        raise credentials_exception
        
    user = get_user(username=token_data.username)
    if user is None:
        raise credentials_exception
    return user

async def get_current_active_user(current_user: dict = Depends(get_current_user)):
    """获取当前活跃用户"""
    if not current_user.get("is_active", True):
        raise HTTPException(status_code=400, detail="Inactive user")
    return current_user

def notify_admins_new_user(user_doc: dict):
    """给所有管理员发送新用户注册通知"""
    try:
        # 直接使用db连接
        notifications_coll = db["notifications"]
        
        # 查找所有管理员
        admins = list(users_collection.find({"is_admin": True, "is_active": True}))
        
        if not admins:
            print("WARNING: 没有找到活跃的管理员用户")
            return
        
        # 为每个管理员创建通知
        now = datetime.utcnow()
        for admin in admins:
            notification = {
                "user_id": str(admin["_id"]),
                "type": "user_registration",
                "title": "新用户注册待审批",
                "message": f"用户 {user_doc.get('username')} ({user_doc.get('email')}) 已注册，请审批。",
                "data": {
                    "user_id": user_doc.get("id"),
                    "username": user_doc.get("username"),
                    "email": user_doc.get("email"),
                    "full_name": user_doc.get("full_name"),
                    "created_at": user_doc.get("created_at").isoformat() if user_doc.get("created_at") else None
                },
                "is_read": False,
                "created_at": now,
                "read_at": None
            }
            notifications_coll.insert_one(notification)
            print(f"DEBUG: 已为管理员 {admin.get('username')} 创建通知")
            
    except Exception as e:
        print(f"ERROR: 创建管理员通知失败: {e}")
        import traceback
        traceback.print_exc()


def issue_account_activation_jwt(user_id: str, username: str, email: str) -> str:
    """自助开通账号用的邮件链接 JWT（与 /api/auth/activate 校验一致）。"""
    data = {
        "purpose": "account_activation",
        "user_id": user_id,
        "username": username,
        "email": email,
        "exp": datetime.utcnow() + timedelta(days=ACCOUNT_ACTIVATION_TOKEN_DAYS),
    }
    return jwt.encode(data, EMAIL_VERIFICATION_SECRET, algorithm=ALGORITHM)


def create_user(user_data: UserCreate):
    """创建新用户"""
    print(f"DEBUG: 尝试创建用户: username={user_data.username}, email={user_data.email}")
    
    # 检查用户名是否已存在
    existing_user = get_user(user_data.username)
    if existing_user:
        print(f"DEBUG: 用户名 {user_data.username} 已存在")
        raise HTTPException(
            status_code=400,
            detail="Username already registered"
        )
    
    # 检查邮箱是否已存在
    existing_email = users_collection.find_one({"email": user_data.email})
    if existing_email:
        print(f"DEBUG: 邮箱 {user_data.email} 已存在")
        raise HTTPException(
            status_code=400,
            detail="Email already registered"
        )
    
    # 创建用户文档
    user_doc = {
        "username": user_data.username,
        "email": user_data.email,
        "full_name": user_data.full_name,
        "hashed_password": get_password_hash(user_data.password),
        "is_active": False,  # 新注册用户默认不激活，需要管理员审批
        "is_admin": False,  # 默认不是管理员
        "created_at": datetime.utcnow(),
        "last_login": None,
        "email_verified": False,  # 邮箱未验证
        "approval_status": "pending",  # 审批状态：pending(待审批) / approved(已批准) / rejected(已拒绝)
        "approved_by": None,  # 审批人
        "approved_at": None,  # 审批时间
        "llm_provider": user_data.llm_provider,
        "llm_api_key": user_data.llm_api_key,
        "llm_base_url": user_data.llm_base_url,
        "llm_model": user_data.llm_model,
        "llm_configs": [],  # 初始化新的多LLM配置数组
        "active_llm_config_id": None  # 初始化激活的配置ID
    }
    
    result = users_collection.insert_one(user_doc)
    user_doc["id"] = str(result.inserted_id)
    del user_doc["_id"]
    del user_doc["hashed_password"]  # 不返回密码哈希
    
    print(
        f"DEBUG: 用户 {user_data.username} 创建成功，ID: {user_doc['id']}，"
        f"可自助邮件激活或等待管理员审批"
    )
    
    # 发送自助开通邮件（未配置 SMTP 时静默跳过，不阻断注册）
    if os.getenv("SEND_REGISTRATION_ACTIVATION_EMAIL", "true").lower() not in (
        "0",
        "false",
        "no",
    ):
        try:
            from services.email_service import email_service

            act_token = issue_account_activation_jwt(
                user_doc["id"], user_data.username, user_data.email
            )
            fe = os.getenv("FRONTEND_URL", "http://localhost:5173").rstrip("/")
            activation_url = f"{fe}/activate?token={act_token}"
            sent = email_service.send_self_activation_email(
                user_data.email, user_data.username, activation_url
            )
            if not sent:
                _log.warning(
                    "注册激活邮件未发出（未配置邮件或失败），用户仍可由管理员审批: %s",
                    user_data.email,
                )
        except Exception as e:
            _log.warning("发送注册激活邮件失败: %s", e, exc_info=True)
    
    # 给管理员发送通知
    try:
        notify_admins_new_user(user_doc)
        print(f"DEBUG: 已向管理员发送新用户注册通知")
    except Exception as e:
        print(f"WARNING: 发送管理员通知失败: {e}")
    
    return user_doc

def log_login_attempt(username: str, ip_address: str, user_agent: str, success: bool, failure_reason: Optional[str] = None):
    """记录登录尝试"""
    user = get_user(username)
    user_id = user["id"] if user else "unknown"
    
    login_log = {
        "user_id": user_id,
        "username": username,
        "ip_address": ip_address,
        "user_agent": user_agent,
        "login_time": datetime.utcnow(),
        "success": success,
        "failure_reason": failure_reason
    }
    
    login_logs_collection.insert_one(login_log)
    
    # 如果登录成功，更新用户的最后登录时间
    if success and user:
        users_collection.update_one(
            {"username": username},
            {"$set": {"last_login": datetime.utcnow()}}
        )

def log_admin_action(admin_user: dict, action: str, target_user_id: Optional[str] = None, target_username: Optional[str] = None, details: str = ""):
    """记录管理员操作"""
    admin_action = {
        "admin_id": admin_user["id"],
        "admin_username": admin_user["username"],
        "action": action,
        "target_user_id": target_user_id,
        "target_username": target_username,
        "details": details,
        "timestamp": datetime.utcnow()
    }
    
    admin_logs_collection.insert_one(admin_action)

async def get_current_admin_user(current_user: dict = Depends(get_current_active_user)):
    """获取当前管理员用户（验证管理员权限）"""
    if not current_user.get("is_admin", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required"
        )
    return current_user

def create_admin_user(username: str, password: str, email: str, full_name: Optional[str] = None):
    """创建管理员用户（仅用于初始化）"""
    admin_doc = {
        "username": username,
        "email": email,
        "full_name": full_name,
        "hashed_password": get_password_hash(password),
        "is_active": True,
        "is_admin": True,
        "created_at": datetime.utcnow(),
        "last_login": None
    }
    
    # 检查是否已存在同名用户
    if get_user(username):
        # 如果存在，更新为管理员
        users_collection.update_one(
            {"username": username},
            {"$set": {"is_admin": True}}
        )
        return {"message": f"User {username} promoted to admin"}
    else:
        # 创建新的管理员用户
        result = users_collection.insert_one(admin_doc)
        return {"message": f"Admin user {username} created", "id": str(result.inserted_id)}
