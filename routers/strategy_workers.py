"""Strategy Manager Workers API

暴露 quant-strategy-manager 的 Worker 信息和实时日志流地址。
集成 quant-strategy-manager 的 MultiStrategyOrchestrator，
不需要单独的 Flask 服务器。

Endpoints:
    GET /api/strategy/workers - 获取所有 Workers 及其日志流地址
    GET /api/strategy/workers/{worker_key} - 获取单个 Worker 详情
    GET /api/strategy/workers/{worker_key}/console - 获取 Worker 控制台 URL
    GET /api/strategy/status - 获取整体状态
"""

from fastapi import APIRouter, HTTPException, Depends
from typing import Dict, Any, Optional
import sys
import os
from pathlib import Path
import logging
import httpx
from auth import get_current_user  # 导入认证函数

router = APIRouter()
logger = logging.getLogger("strategy_workers")

# ==================== 配置 ====================

REMOTE_WORKER_API = os.getenv("REMOTE_WORKER_API", "")
"""
远程 quant-strategy-manager 的 Worker API 地址

⚠️ 必须配置: 此服务只支持远程 API 模式
   
配置方法:
    1. 复制 .env.template 为 .env
    2. 设置: REMOTE_WORKER_API=http://localhost:5000/api/workers
    3. 确保 api_server.py 已启动

示例:
    本地: http://localhost:5000/api/workers
    远程: http://192.168.1.100:5000/api/workers
"""

# ==================== 辅助函数 ====================

def _get_base_api_url() -> str:
    """获取远程 API 的基础 URL（去掉 /workers 后缀）"""
    if not REMOTE_WORKER_API:
        raise HTTPException(
            status_code=503,
            detail="Strategy Manager API not configured. Please set REMOTE_WORKER_API in .env file."
        )
    return REMOTE_WORKER_API.rstrip('/workers').rstrip('/')


async def _call_remote_api(endpoint: str, token: str, method: str = "GET") -> Dict[str, Any]:
    """调用远程 Worker API
    
    Args:
        endpoint: API 端点（如 "/workers", "/status"）
        token: JWT token 用于认证
        method: HTTP 方法
        
    Returns:
        API 响应的 JSON 数据
        
    Raises:
        HTTPException: 当 API 不可用或返回错误时
    """
    base_url = _get_base_api_url()
    url = f"{base_url}{endpoint}"
    
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            if method == "GET":
                response = await client.get(url, headers=headers)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")
            
            print(f"[_call_remote_api] Response status: {response.status_code}")
            response.raise_for_status()
            return response.json()
            
    except httpx.HTTPStatusError as e:
        # 提取远程 API 的错误信息
        try:
            error_detail = e.response.json().get('detail', str(e))
        except:
            error_detail = e.response.text or str(e)
        
        raise HTTPException(
            status_code=e.response.status_code,
            detail=error_detail
        )
    except httpx.RequestError as e:
        logger.error(f"Failed to connect to remote API {url}: {e}")
        raise HTTPException(
            status_code=503,
            detail=f"Cannot connect to Strategy Manager API at {REMOTE_WORKER_API}. "
                   f"Please ensure api_server.py is running."
        )
    except Exception as e:
        logger.error(f"Unexpected error calling remote API: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/workers")
async def list_workers(current_user: dict = Depends(get_current_user)) -> Dict[str, Any]:
    """获取当前用户的 Workers"""
    # 创建 token 传递给 quant-strategy-manager
    from auth import create_access_token
    user_id = current_user.get("id", current_user.get("username"))
    token = create_access_token(data={"sub": current_user["username"], "user_id": str(user_id)})
    
    data = await _call_remote_api("/workers", token=token)
    return {**data, "deployment_mode": "remote"}


@router.get("/workers/{worker_key}")
async def get_worker(worker_key: str, current_user: dict = Depends(get_current_user)) -> Dict[str, Any]:
    """获取单个 Worker 的详细信息
    
    Args:
        worker_key: Worker 标识 (e.g., "user123_002050.SZ_hidden_dragon")
    
    Returns:
        {
            "alive": True,
            "stats": {...},
            "log_stream_url": "ws://..."
        }
    """
    from auth import create_access_token
    token = create_access_token(data={"sub": current_user["username"], "user_id": str(current_user["id"])})
    
    data = await _call_remote_api(f"/workers/{worker_key}", token)
    return data


@router.get("/workers/{worker_key}/console")
async def get_worker_console_url(worker_key: str, current_user: dict = Depends(get_current_user)) -> Dict[str, str]:
    """获取 Worker 的日志流 WebSocket URL（用于打开日志查看器）
    
    Args:
        worker_key: Worker 标识
    
    Returns:
        {
            "worker_key": "user123_002050.SZ_hidden_dragon",
            "log_stream_url": "ws://192.168.1.100:54321"
        }
    """
    from auth import create_access_token
    token = create_access_token(data={"sub": current_user["username"], "user_id": str(current_user["id"])})
    
    data = await _call_remote_api(f"/workers/{worker_key}/console", token)
    
    # 修复 WebSocket URL (0.0.0.0 → 实际主机名)
    if 'log_stream_url' in data:
        ws_url = data['log_stream_url']
        if '0.0.0.0' in ws_url:
            from urllib.parse import urlparse
            parsed = urlparse(REMOTE_WORKER_API)
            actual_host = parsed.hostname or 'localhost'
            data['log_stream_url'] = ws_url.replace('0.0.0.0', actual_host)
    
    return data


@router.get("/workers/{worker_key}/logs")
@router.head("/workers/{worker_key}/logs")
async def get_worker_logs(
    worker_key: str, 
    tail: int = None,
    current_user: dict = Depends(get_current_user)  # 添加认证
):
    """获取 Worker 的历史日志文件
    
    Args:
        worker_key: Worker 标识
        tail: 只返回最后 N 行（可选）
        current_user: 当前已认证用户
    
    Returns:
        日志文件内容（text/plain）
    
    Example:
        curl http://localhost:3001/api/strategy/workers/{worker_key}/logs?tail=100
    """
    logger.info(f"📋 用户 {current_user.get('username')} 请求日志: worker_key={worker_key}, tail={tail}")
    
    # 创建 token 传递给 quant-strategy-manager
    from auth import create_access_token
    user_id = current_user.get("id", current_user.get("username"))
    token = create_access_token(data={"sub": current_user["username"], "user_id": str(user_id)})
    
    base_url = _get_base_api_url()
    url = f"{base_url}/workers/{worker_key}/logs"
    if tail:
        url += f"?tail={tail}"
    
    logger.info(f"🔗 转发请求到: {url} (带认证)")
    
    try:
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            
            logger.info(f"✅ 成功获取日志，长度: {len(response.text)} 字符")
            
            from fastapi.responses import PlainTextResponse
            return PlainTextResponse(response.text)
            
    except httpx.HTTPStatusError as e:
        try:
            error_detail = e.response.json().get('detail', str(e))
        except:
            error_detail = e.response.text or str(e)
        
        logger.error(f"❌ 远程 API 错误 ({e.response.status_code}): {error_detail}")
        
        raise HTTPException(
            status_code=e.response.status_code,
            detail=error_detail
        )
    except Exception as e:
        logger.error(f"❌ 获取日志异常: {type(e).__name__}: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"无法获取历史日志: {str(e)}"
        )


@router.get("/status")
async def get_status(current_user: dict = Depends(get_current_user)) -> Dict[str, Any]:
    """获取 orchestrator 整体状态
    
    Returns:
        {
            "healthy": True,
            "total_workers": 3,
            "active_workers": 2,
            "deployment_mode": "remote"
        }
    """
    from auth import create_access_token
    token = create_access_token(data={"sub": current_user["username"], "user_id": str(current_user["id"])})
    
    data = await _call_remote_api("/status", token)
    return {**data, "deployment_mode": "remote"}


# ==================== 生命周期事件 ====================

@router.on_event("startup")
async def startup():
    """应用启动时验证远程 API 可用性"""
    if not REMOTE_WORKER_API:
        logger.warning(
            "⚠️  REMOTE_WORKER_API not configured!\n"
            "   Strategy Manager Workers API will not be available.\n"
            "   Please set REMOTE_WORKER_API in .env file."
        )
        return
    
    try:
        # 使用公开的健康检查端点（不需要认证）
        base_url = _get_base_api_url()
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"{base_url.rstrip('/api/workers')}/health")
            response.raise_for_status()
        logger.info(f"✓ Strategy Manager Workers API ready: {REMOTE_WORKER_API}")
    except Exception as e:
        logger.error(f"✗ Strategy Manager API unavailable: {e}")


@router.on_event("shutdown")
async def shutdown():
    """应用关闭时清理资源"""
    logger.info("Strategy Manager router shutting down")
