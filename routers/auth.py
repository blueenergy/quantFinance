"""
认证相关路由
处理用户注册、登录、令牌刷新等认证功能
"""

from fastapi import APIRouter, Depends, Request, HTTPException
from models.all_models import UserCreate, UserLogin, UserResponse, Token
from auth import (
    create_user, authenticate_user, create_access_token, 
    get_current_active_user, log_login_attempt, ACCESS_TOKEN_EXPIRE_MINUTES
)
from datetime import timedelta

router = APIRouter()


@router.post("/register", response_model=UserResponse)
def register(user: UserCreate):
    """用户注册"""
    return create_user(user)


@router.post("/login", response_model=Token)
def login(user_data: UserLogin, request: Request):
    """用户登录"""
    # 获取客户端信息
    ip_address = request.client.host if request.client else "unknown"
    user_agent = request.headers.get("user-agent", "unknown")
    
    user = authenticate_user(user_data.username, user_data.password)
    if not user:
        # 记录失败的登录尝试
        log_login_attempt(
            username=user_data.username,
            ip_address=ip_address,
            user_agent=user_agent,
            success=False,
            failure_reason="Incorrect username or password"
        )
        raise HTTPException(
            status_code=401,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # 检查用户是否已激活（管理员审批通过）
    if not user.get("is_active", False):
        # 检查审批状态
        approval_status = user.get("approval_status", "pending")
        if approval_status == "pending":
            detail_msg = "账户待审批，请等待管理员审批后再登录"
        elif approval_status == "rejected":
            detail_msg = "账户已被管理员拒绝，请联系管理员"
        else:
            detail_msg = "账户未激活，请联系管理员"
        
        log_login_attempt(
            username=user_data.username,
            ip_address=ip_address,
            user_agent=user_agent,
            success=False,
            failure_reason=f"Account not activated: {approval_status}"
        )
        raise HTTPException(
            status_code=401,
            detail=detail_msg,
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # 记录成功的登录
    log_login_attempt(
        username=user_data.username,
        ip_address=ip_address,
        user_agent=user_agent,
        success=True
    )
    
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user["username"], "user_id": user["id"]}, 
        expires_delta=access_token_expires
    )
    
    # 准备用户响应数据
    user_response = UserResponse(
        id=user["id"],
        username=user["username"],
        email=user["email"],
        full_name=user.get("full_name"),
        is_active=user.get("is_active", True),
        is_admin=user.get("is_admin", False),
        service_level=user.get("service_level", "free"),
        created_at=user["created_at"],
        last_login=user.get("last_login")
    )
    
    return Token(
        access_token=access_token,
        token_type="bearer",
        user=user_response
    )


@router.post("/token/refresh", response_model=Token)
def refresh_token(current_user: dict = Depends(get_current_active_user)):
    """为已登录用户生成新的 Token（用于 quantTrader 配置）
    
    注意：这不会撤销旧 Token，旧 Token 仍然有效直到过期
    """
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": current_user["username"]}, 
        expires_delta=access_token_expires
    )
    
    user_response = UserResponse(
        id=current_user["id"],
        username=current_user["username"],
        email=current_user["email"],
        full_name=current_user.get("full_name"),
        is_active=current_user.get("is_active", True),
        is_admin=current_user.get("is_admin", False),
        service_level=current_user.get("service_level", "free"),
        created_at=current_user["created_at"],
        last_login=current_user.get("last_login"),
        llm_provider=current_user.get("llm_provider"),
        llm_api_key=None,  # Never return API key for security
        llm_base_url=current_user.get("llm_base_url"),
        llm_model=current_user.get("llm_model"),
        enable_analytics=current_user.get("enable_analytics", True),
        active_llm_config_id=current_user.get("active_llm_config_id")
    )
    
    return Token(
        access_token=access_token,
        token_type="bearer",
        user=user_response
    )
