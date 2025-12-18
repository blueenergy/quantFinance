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
import os
from pymongo import MongoClient
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

# JWT配置
SECRET_KEY = os.getenv("JWT_SECRET_KEY", "your-secret-key-change-in-production")
ALGORITHM = "HS256"
# 默认 8 小时，可通过环境变量 JWT_ACCESS_EXPIRE_MINUTES 调整
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("JWT_ACCESS_EXPIRE_MINUTES", "480"))

# 密码加密
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# JWT Bearer token
security = HTTPBearer()

# MongoDB连接
mongo_url = os.getenv("MONGO_URL", "mongodb://localhost:27017")
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

class UserResponse(UserBase):
    id: str
    is_active: bool
    is_admin: bool = False
    created_at: datetime
    last_login: Optional[datetime] = None

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

def create_user(user_data: UserCreate):
    """创建新用户"""
    # 检查用户名是否已存在
    if get_user(user_data.username):
        raise HTTPException(
            status_code=400,
            detail="Username already registered"
        )
    
    # 检查邮箱是否已存在
    if users_collection.find_one({"email": user_data.email}):
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
        "is_active": True,
        "is_admin": False,  # 默认不是管理员
        "created_at": datetime.utcnow(),
        "last_login": None
    }
    
    result = users_collection.insert_one(user_doc)
    user_doc["id"] = str(result.inserted_id)
    del user_doc["_id"]
    del user_doc["hashed_password"]  # 不返回密码哈希
    
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
