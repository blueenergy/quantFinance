from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr
from typing import Optional, List
from datetime import datetime, timedelta
import jwt
import os
from auth import get_current_active_user, users_collection, SECRET_KEY, ALGORITHM
from models.all_models import UpdateLLMConfigRequest
from bson.objectid import ObjectId
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

router = APIRouter()

# 邮箱验证相关
EMAIL_VERIFICATION_SECRET = os.getenv("EMAIL_VERIFICATION_SECRET", "email-verification-secret-change-in-production")

class UpdateProfileRequest(BaseModel):
    full_name: Optional[str] = None

class UpdateEmailRequest(BaseModel):
    email: EmailStr

class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str

class TwoFactorRequest(BaseModel):
    enabled: bool

class TestLLMRequest(BaseModel):
    provider: str
    api_key: str
    base_url: Optional[str] = None
    model: Optional[str] = None

class LLMConfigRequest(BaseModel):
    """创建或更新LLM配置"""
    name: str
    provider: str
    api_key: Optional[str] = None  # 更新时可选
    base_url: Optional[str] = None
    model: Optional[str] = None

class LLMConfigResponse(BaseModel):
    """LLM配置响应"""
    id: str
    name: str
    provider: str
    base_url: Optional[str] = None
    model: Optional[str] = None
    created_at: Optional[datetime] = None

security = HTTPBearer()

def send_verification_email(email: str, username: str):
    """发送邮箱验证邮件"""
    # 创建验证令牌
    verification_data = {
        "email": email,
        "username": username,
        "exp": datetime.utcnow() + timedelta(hours=24)  # 24小时有效
    }
    verification_token = jwt.encode(verification_data, EMAIL_VERIFICATION_SECRET, algorithm=ALGORITHM)
    
    # 构建验证链接 (这里使用占位符，实际使用时需要替换为真实URL)
    verification_link = f"http://yourdomain.com/api/user/verify-email?token={verification_token}"
    
    # 发送邮件的逻辑 (这里使用伪代码，实际需要配置邮件服务器)
    try:
        # 邮件内容
        msg = MIMEMultipart()
        msg['Subject'] = '邮箱验证 - 请验证您的邮箱地址'
        msg['From'] = os.getenv('SMTP_FROM_EMAIL', 'noreply@yourdomain.com')
        msg['To'] = email
        
        body = f"""
        您好 {username}，
        
        感谢您注册我们的服务。请点击以下链接验证您的邮箱地址：
        
        {verification_link}
        
        此链接将在24小时后过期。
        
        如果您没有注册我们的服务，请忽略此邮件。
        
        此致，
        服务团队
        """
        
        msg.attach(MIMEText(body, 'plain', 'utf-8'))
        
        # 发送邮件 (需要配置SMTP服务器)
        smtp_server = os.getenv('SMTP_SERVER', 'localhost')
        smtp_port = int(os.getenv('SMTP_PORT', '587'))
        smtp_username = os.getenv('SMTP_USERNAME')
        smtp_password = os.getenv('SMTP_PASSWORD')
        
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(smtp_username, smtp_password)
            server.send_message(msg)
            
        return True
    except Exception as e:
        print(f"发送验证邮件失败: {str(e)}")
        return False

@router.get("/profile")
def get_user_profile(current_user: dict = Depends(get_current_active_user)):
    """获取用户完整资料"""
    return {
        "success": True,
        "user": {
            "id": current_user.get("id"),
            "username": current_user.get("username"),
            "email": current_user.get("email"),
            "full_name": current_user.get("full_name"),
            "email_verified": current_user.get("email_verified", False),
            "is_active": current_user.get("is_active", True),
            "is_admin": current_user.get("is_admin", False),
            "created_at": current_user.get("created_at"),
            "last_login": current_user.get("last_login"),
            "llm_provider": current_user.get("llm_provider"),
            "llm_base_url": current_user.get("llm_base_url"),
            "llm_model": current_user.get("llm_model"),
            "enable_analytics": current_user.get("enable_analytics", True),
            "active_llm_config_id": current_user.get("active_llm_config_id"),
            "service_level": current_user.get("service_level", "free"),
        }
    }

@router.put("/profile")
def update_user_profile(
    request: UpdateProfileRequest, 
    current_user: dict = Depends(get_current_active_user)
):
    """更新用户资料"""
    update_data = {}
    
    if request.full_name is not None:
        update_data["full_name"] = request.full_name
    
    result = users_collection.update_one(
        {"_id": ObjectId(current_user["id"])},
        {"$set": update_data}
    )
    
    if result.modified_count > 0:
        return {"success": True, "message": "个人资料更新成功"}
    else:
        return {"success": True, "message": "个人资料可能未发生改变"}

@router.put("/email")
def update_user_email(
    request: UpdateEmailRequest,
    current_user: dict = Depends(get_current_active_user),
    background_tasks: BackgroundTasks = BackgroundTasks()
):
    """更新用户邮箱并发送验证邮件"""
    # 检查邮箱是否已被其他用户使用
    existing_user = users_collection.find_one({"email": request.email})
    if existing_user and str(existing_user["_id"]) != current_user["id"]:
        raise HTTPException(status_code=400, detail="该邮箱已被其他用户使用")
    
    # 更新用户邮箱，标记为未验证
    result = users_collection.update_one(
        {"_id": ObjectId(current_user["id"])},
        {"$set": {
            "email": request.email,
            "email_verified": False
        }}
    )
    
    if result.modified_count > 0:
        # 在后台发送验证邮件
        background_tasks.add_task(send_verification_email, request.email, current_user["username"])
        return {
            "success": True, 
            "message": "邮箱地址已更新，请查收验证邮件",
            "email_verified": False
        }
    else:
        raise HTTPException(status_code=400, detail="邮箱更新失败")

@router.post("/resend-verification")
def resend_verification_email(
    current_user: dict = Depends(get_current_active_user),
    background_tasks: BackgroundTasks = BackgroundTasks()
):
    """重新发送验证邮件"""
    background_tasks.add_task(send_verification_email, current_user["email"], current_user["username"])
    return {"success": True, "message": "验证邮件已重新发送"}

@router.get("/verify-email")
def verify_email(token: str):
    """验证邮箱地址"""
    try:
        # 解码验证令牌
        payload = jwt.decode(token, EMAIL_VERIFICATION_SECRET, algorithms=[ALGORITHM])
        email = payload.get("email")
        username = payload.get("username")
        
        if not email or not username:
            raise HTTPException(status_code=400, detail="验证令牌无效")
        
        # 更新用户邮箱验证状态
        result = users_collection.update_one(
            {"username": username, "email": email},
            {"$set": {"email_verified": True}}
        )
        
        if result.modified_count > 0:
            return {"success": True, "message": "邮箱验证成功"}
        else:
            raise HTTPException(status_code=400, detail="用户信息不匹配，验证失败")
            
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=400, detail="验证链接已过期")
    except jwt.JWTError:
        raise HTTPException(status_code=400, detail="验证令牌无效")

@router.post("/change-password")
def change_user_password(
    request: ChangePasswordRequest,
    current_user: dict = Depends(get_current_active_user)
):
    """修改用户密码"""
    from auth import verify_password, get_password_hash
    
    # 验证旧密码
    if not verify_password(request.old_password, current_user["hashed_password"]):
        raise HTTPException(status_code=400, detail="当前密码不正确")
    
    # 检查新密码强度
    if len(request.new_password) < 8:
        raise HTTPException(status_code=400, detail="新密码长度不能少于8位")
    
    # 更新密码
    hashed_new_password = get_password_hash(request.new_password)
    result = users_collection.update_one(
        {"_id": ObjectId(current_user["id"])},
        {"$set": {"hashed_password": hashed_new_password}}
    )
    
    if result.modified_count > 0:
        return {"success": True, "message": "密码修改成功"}
    else:
        raise HTTPException(status_code=400, detail="密码修改失败")

@router.put("/two-factor")
def update_two_factor(
    request: TwoFactorRequest,
    current_user: dict = Depends(get_current_active_user)
):
    """更新双因素认证设置"""
    result = users_collection.update_one(
        {"_id": ObjectId(current_user["id"])},
        {"$set": {"two_factor_enabled": request.enabled}}
    )
    
    status = "启用" if request.enabled else "禁用"
    if result.modified_count > 0:
        return {"success": True, "message": f"双因素认证{status}成功"}
    else:
        return {"success": True, "message": f"双因素认证{status}状态"}

@router.get("/security-settings")
def get_security_settings(current_user: dict = Depends(get_current_active_user)):
    """获取安全设置"""
    return {
        "success": True,
        "two_factor_enabled": current_user.get("two_factor_enabled", False)
    }

@router.put("/llm-config")
def update_llm_config(
    request: UpdateLLMConfigRequest,
    current_user: dict = Depends(get_current_active_user)
):
    """更新LLM配置"""
    update_data = {}
    
    if request.llm_provider is not None:
        update_data["llm_provider"] = request.llm_provider
    if request.llm_api_key is not None:
        update_data["llm_api_key"] = request.llm_api_key
    if request.llm_base_url is not None:
        update_data["llm_base_url"] = request.llm_base_url
    if request.llm_model is not None:
        update_data["llm_model"] = request.llm_model
    
    result = users_collection.update_one(
        {"_id": ObjectId(current_user["id"])},
        {"$set": update_data}
    )
    
    if result.modified_count > 0:
        return {"success": True, "message": "LLM配置更新成功"}
    else:
        return {"success": True, "message": "LLM配置可能未发生改变"}

# 同步版本的LLM配置测试
@router.post("/llm/test")
def test_llm_config_sync(request: TestLLMRequest):
    """测试LLM配置 - 同步版本"""
    try:
        # 验证基本参数
        if not request.provider or not request.api_key:
            return {"success": False, "message": "提供商和API密钥不能为空"}
        
        # 根据提供商设置API基础URL
        base_url = request.base_url
        if not base_url:
            if request.provider == "openai":
                base_url = "https://api.openai.com/v1"
            elif request.provider == "deepseek":
                base_url = "https://api.deepseek.com/v1"
            else:
                base_url = "https://api.openai.com/v1"  # 默认
        
        # 选择模型
        model = request.model
        if not model:
            if request.provider == "openai":
                model = "gpt-3.5-turbo"
            elif request.provider == "deepseek":
                model = "deepseek-chat"
            else:
                model = "gpt-3.5-turbo"
        
        # 发送测试请求
        import requests
        headers = {
            "Authorization": f"Bearer {request.api_key}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": model,
            "messages": [
                {"role": "user", "content": "Hello, this is a test."}
            ],
            "max_tokens": 5
        }
        
        response = requests.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=10
        )
        
        if response.status_code == 200:
            return {"success": True, "message": "LLM配置测试成功"}
        else:
            return {"success": False, "message": f"API调用失败: {response.status_code}"}
                
    except Exception as e:
        return {"success": False, "message": f"测试失败: {str(e)}"}


# LLM 配置管理端点
@router.get("/llm-configs")
def get_llm_configs(current_user: dict = Depends(get_current_active_user)):
    """获取用户的所有LLM配置"""
    try:
        # current_user["id"] 是字符串，需要转换为 ObjectId
        user = users_collection.find_one({"_id": ObjectId(current_user["id"])})
        if not user:
            raise HTTPException(status_code=404, detail="用户不存在")
        
        configs = user.get("llm_configs", [])
        if not isinstance(configs, list):
            configs = []
        
        result = []
        for idx, config in enumerate(configs):
            try:
                print(f"处理配置 {idx}: {config}")  # 调试日志
                
                config_id = config.get("_id")
                # 安全串行化ObjectId
                if config_id:
                    config_id_str = str(config_id) if not isinstance(config_id, str) else config_id
                    print(f"配置ID: {config_id_str}, 类型: {type(config_id)}")  # 调试日志
                else:
                    config_id_str = ""
                    print(f"警告: 配置 {idx} 没有_id字段")
                
                item = {
                    "id": config_id_str,
                    "name": config.get("name", ""),
                    "provider": config.get("provider", ""),
                    "base_url": config.get("base_url"),
                    "model": config.get("model"),
                }
                
                # 只在created_at是日期时添加
                created_at = config.get("created_at")
                if created_at:
                    try:
                        item["created_at"] = created_at.isoformat() if hasattr(created_at, 'isoformat') else str(created_at)
                        print(f"created_at处理成功: {item['created_at']}")
                    except Exception as dt_err:
                        print(f"created_at处理失败: {dt_err}, 值: {created_at}, 类型: {type(created_at)}")
                        item["created_at"] = str(created_at)
                
                result.append(item)
                print(f"成功添加配置 {idx}")
            except Exception as e:
                print(f"处理配置 {idx} 出错: {e}, config: {config}")
                import traceback
                traceback.print_exc()
                continue
        
        print(f"成功获取{len(result)}个配置")
        return result
    except HTTPException:
        raise
    except Exception as e:
        print(f"获取LLM配置失败: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"获取配置失败: {str(e)}")


@router.post("/llm-configs", response_model=LLMConfigResponse)
def create_llm_config(
    request: LLMConfigRequest,
    current_user: dict = Depends(get_current_active_user)
):
    """创建新的LLM配置"""
    try:
        if not request.name or not request.provider or not request.api_key:
            raise HTTPException(status_code=400, detail="配置名称、提供商和API密钥不能为空")
        
        new_config = {
            "_id": ObjectId(),
            "name": request.name,
            "provider": request.provider,
            "api_key": request.api_key,
            "base_url": request.base_url,
            "model": request.model,
            "created_at": datetime.utcnow()
        }
        
        result = users_collection.update_one(
            {"_id": ObjectId(current_user["id"])},
            {"$push": {"llm_configs": new_config}}
        )
        
        if result.modified_count > 0:
            return {
                "id": str(new_config["_id"]),
                "name": new_config["name"],
                "provider": new_config["provider"],
                "base_url": new_config["base_url"],
                "model": new_config["model"],
                "created_at": new_config["created_at"]
            }
        else:
            raise HTTPException(status_code=400, detail="创建配置失败")
    except HTTPException:
        raise
    except Exception as e:
        print(f"创建配置失败: {e}")
        raise HTTPException(status_code=500, detail=f"创建配置失败: {str(e)}")


@router.put("/llm-configs/{config_id}", response_model=LLMConfigResponse)
def update_llm_config(
    config_id: str,
    request: LLMConfigRequest,
    current_user: dict = Depends(get_current_active_user)
):
    """更新指定的LLM配置"""
    try:
        config_oid = ObjectId(config_id)
    except:
        raise HTTPException(status_code=400, detail="无效的配置ID")
    
    # 获取用户和配置
    user = users_collection.find_one({"_id": ObjectId(current_user["id"])})
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    
    # 查找配置
    config_index = None
    for i, cfg in enumerate(user.get("llm_configs", [])):
        if cfg.get("_id") == config_oid:
            config_index = i
            break
    
    if config_index is None:
        raise HTTPException(status_code=404, detail="配置不存在")
    
    # 准备更新数据
    update_data = {
        "name": request.name,
        "provider": request.provider,
    }
    
    if request.api_key:
        update_data["api_key"] = request.api_key
    
    if request.base_url is not None:
        update_data["base_url"] = request.base_url
    
    if request.model is not None:
        update_data["model"] = request.model
    
    # 更新配置
    result = users_collection.update_one(
        {"_id": ObjectId(current_user["id"]), "llm_configs._id": config_oid},
        {"$set": {f"llm_configs.$.{k}": v for k, v in update_data.items()}}
    )
    
    if result.modified_count > 0:
        # 获取更新后的配置
        user = users_collection.find_one({"_id": ObjectId(current_user["id"])})
        updated_config = None
        for cfg in user.get("llm_configs", []):
            if cfg.get("_id") == config_oid:
                updated_config = cfg
                break
        
        return {
            "id": str(updated_config["_id"]),
            "name": updated_config["name"],
            "provider": updated_config["provider"],
            "base_url": updated_config.get("base_url"),
            "model": updated_config.get("model"),
            "created_at": updated_config.get("created_at")
        }
    else:
        raise HTTPException(status_code=400, detail="更新配置失败")


@router.delete("/llm-configs/{config_id}")
def delete_llm_config(
    config_id: str,
    current_user: dict = Depends(get_current_active_user)
):
    """删除指定的LLM配置"""
    try:
        config_oid = ObjectId(config_id)
    except:
        raise HTTPException(status_code=400, detail="无效的配置ID")
    
    # 如果删除的是激活的配置，先取消激活
    user = users_collection.find_one({"_id": ObjectId(current_user["id"])})
    if user and user.get("active_llm_config_id") == config_id:
        users_collection.update_one(
            {"_id": ObjectId(current_user["id"])},
            {"$set": {"active_llm_config_id": None}}
        )
    
    # 删除配置
    result = users_collection.update_one(
        {"_id": ObjectId(current_user["id"])},
        {"$pull": {"llm_configs": {"_id": config_oid}}}
    )
    
    if result.modified_count > 0:
        return {"success": True, "message": "配置删除成功"}
    else:
        raise HTTPException(status_code=404, detail="配置不存在")


@router.post("/llm-configs/{config_id}/activate")
def activate_llm_config(
    config_id: str,
    current_user: dict = Depends(get_current_active_user)
):
    """激活指定的LLM配置"""
    try:
        config_oid = ObjectId(config_id)
    except:
        raise HTTPException(status_code=400, detail="无效的配置ID")
    
    # 验证配置存在
    user = users_collection.find_one({"_id": ObjectId(current_user["id"])})
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    
    config_exists = any(cfg.get("_id") == config_oid for cfg in user.get("llm_configs", []))
    if not config_exists:
        raise HTTPException(status_code=404, detail="配置不存在")
    
    # 检查是否已经激活
    if user.get("active_llm_config_id") == config_id:
        return {"success": True, "message": "该配置已经是激活状态"}
    
    # 激活配置
    result = users_collection.update_one(
        {"_id": ObjectId(current_user["id"])},
        {"$set": {"active_llm_config_id": config_id}}
    )
    
    if result.modified_count > 0:
        return {"success": True, "message": "配置激活成功"}
    else:
        raise HTTPException(status_code=400, detail="激活配置失败")