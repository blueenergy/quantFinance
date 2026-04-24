"""
认证相关路由
处理用户注册、登录、令牌刷新等认证功能
"""

import jwt
import os
from datetime import datetime, timedelta
from typing import Any, Dict

from bson import ObjectId
from fastapi import APIRouter, Depends, Request, HTTPException
from pydantic import BaseModel, EmailStr

from models.all_models import UserCreate, UserLogin, UserResponse, Token
from auth import (
    APPROVED_BY_EMAIL_SELF_SERVICE,
    ALGORITHM,
    EMAIL_VERIFICATION_SECRET,
    create_user,
    authenticate_user,
    create_access_token,
    get_current_active_user,
    log_login_attempt,
    ACCESS_TOKEN_EXPIRE_MINUTES,
    issue_account_activation_jwt,
    users_collection,
)

from services.email_service import email_service

router = APIRouter()


class ActivateAccountRequest(BaseModel):
    token: str


class ResendActivationRequest(BaseModel):
    email: EmailStr


@router.post("/register", response_model=UserResponse)
def register(user: UserCreate):
    """用户注册"""
    return create_user(user)


def _activate_account_with_token(token: str) -> Dict[str, Any]:
    """根据激活 JWT 执行业务，返回给前端的统一结构（始终 200 + success 或 code）。"""
    try:
        payload = jwt.decode(
            token, EMAIL_VERIFICATION_SECRET, algorithms=[ALGORITHM]
        )
    except jwt.ExpiredSignatureError:
        return {
            "success": False,
            "code": "expired",
            "message": "激活链接已过期，请使用「重新发送」或联系管理员。",
        }
    except jwt.PyJWTError:
        return {
            "success": False,
            "code": "invalid_token",
            "message": "激活链接无效，请从最新邮件中打开，或联系管理员。",
        }

    if payload.get("purpose") != "account_activation":
        return {
            "success": False,
            "code": "invalid_token",
            "message": "此链接不是有效的账户激活链接。",
        }

    user_id = payload.get("user_id")
    p_email = (payload.get("email") or "").strip()
    p_username = (payload.get("username") or "").strip()
    if not user_id or not p_email or not p_username:
        return {
            "success": False,
            "code": "invalid_token",
            "message": "激活信息不完整，请重新注册或联系管理员。",
        }

    try:
        oid = ObjectId(user_id)
    except Exception:
        return {
            "success": False,
            "code": "invalid_token",
            "message": "无效的用户信息。",
        }

    user = users_collection.find_one({"_id": oid})
    if not user:
        return {
            "success": False,
            "code": "not_found",
            "message": "未找到该用户，可能已被删除。",
        }

    if (user.get("email") or "") != p_email or (user.get("username") or "") != p_username:
        return {
            "success": False,
            "code": "stale_token",
            "message": "该链接已失效（例如邮箱或用户名已变更），请使用「重新发送」或联系管理员。",
        }

    if user.get("approval_status") == "rejected":
        return {
            "success": False,
            "code": "rejected",
            "message": "该注册申请已被拒绝，无法通过此链接开通。",
        }

    is_active = bool(user.get("is_active", False))
    approved = user.get("approval_status") == "approved"
    if is_active:
        if approved:
            by = user.get("approved_by")
            if by == APPROVED_BY_EMAIL_SELF_SERVICE:
                return {
                    "success": True,
                    "code": "already_activated",
                    "message": "您的账户已经通过邮件开通，请直接登录。",
                    "activation_path": "self",
                }
            if by:
                return {
                    "success": True,
                    "code": "already_activated",
                    "message": "该账户已由管理员为您开通，请直接登录。",
                    "activation_path": "admin",
                }
            return {
                "success": True,
                "code": "already_activated",
                "message": "您的账户已可登录。",
                "activation_path": "unknown",
            }
        return {
            "success": True,
            "code": "already_activated",
            "message": "该账户已激活，请直接登录。",
            "activation_path": "unknown",
        }

    now = datetime.utcnow()
    ap = {
        "is_active": True,
        "approval_status": "approved",
        "email_verified": True,
        "approved_by": APPROVED_BY_EMAIL_SELF_SERVICE,
        "approved_at": now,
    }
    users_collection.update_one({"_id": oid}, {"$set": ap})
    return {
        "success": True,
        "code": "activated",
        "message": "激活成功，请使用您的用户名与密码登录。",
        "activation_path": "self",
    }


@router.post("/activate", response_model=None)
def activate_account(body: ActivateAccountRequest) -> Any:
    """通过注册邮件中的链接完成开通（与管理员审批二选一，幂等）。"""
    return _activate_account_with_token(body.token)


@router.post("/resend-activation", response_model=None)
def resend_activation_email(body: ResendActivationRequest) -> Any:
    """向待审批用户重新发送开通邮件（不暴露邮箱是否已注册）。"""
    user = users_collection.find_one(
        {
            "email": str(body.email),
            "approval_status": "pending",
            "is_active": False,
        }
    )
    if not user:
        return {
            "success": True,
            "message": "如果该邮箱有待开通的注册申请，您将很快收到开通邮件。",
        }

    uid = str(user["_id"])
    act_tok = issue_account_activation_jwt(
        uid, user.get("username", ""), user.get("email", "")
    )
    fe = os.getenv("FRONTEND_URL", "http://localhost:5173").rstrip("/")
    activation_url = f"{fe}/activate?token={act_tok}"
    email_service.send_self_activation_email(
        user.get("email", ""), user.get("username", ""), activation_url
    )
    return {
        "success": True,
        "message": "如果该邮箱有待开通的注册申请，您将很快收到开通邮件。",
    }


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
            detail_msg = (
                "账户尚未开通，请查收注册邮件并点击链接激活，或等待管理员审批后再登录"
            )
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
