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

router = APIRouter()
logger = logging.getLogger("strategy_workers")

# 全局 orchestrator 实例（延迟加载）
_orchestrator = None

# 获取远程 Worker API 配置（如果有）
REMOTE_WORKER_API = os.getenv("REMOTE_WORKER_API", "")
"""
远程 quant-strategy-manager 的 Worker API 地址
如果设置，将直接查询远程 API 而不是本地 orchestrator
示例: http://localhost:5000/api/workers  (本地)
     http://192.168.1.100:5000/api/workers (远程服务器)
"""

USE_REMOTE_ONLY = os.getenv("USE_REMOTE_ONLY", "false").lower() == "true"
"""
是否只使用远程 Worker API，忽略本地 orchestrator
"""


def get_orchestrator():
    """获取全局 orchestrator 实例
    
    注意：推荐使用远程 API 模式（通过 api_server.py）
    本地 orchestrator 模式已废弃，因为需要 vnpy 依赖。
    
    使用方法：
        1. 启动 api_server.py (在 vnpy 环境中)
        2. 设置环境变量：
           export REMOTE_WORKER_API="http://localhost:5000/api/workers"
           export USE_REMOTE_ONLY="true"
    """
    global _orchestrator
    
    # 只支持远程 API 模式
    if REMOTE_WORKER_API:
        logger.info(f"Using remote Worker API: {REMOTE_WORKER_API}")
        return None
    
    # 如果没有配置远程 API，返回 None 并提示
    logger.warning(
        "⚠️  Local orchestrator mode is deprecated. Please use api_server.py\n"
        "   Set REMOTE_WORKER_API environment variable to enable Worker API access.\n"
        "   Example: export REMOTE_WORKER_API='http://localhost:5000/api/workers'"
    )
    return None


async def get_remote_workers() -> Optional[Dict[str, Any]]:
    """从远程 Worker API 获取 Workers 列表"""
    if not REMOTE_WORKER_API:
        return None
    
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(REMOTE_WORKER_API)
            response.raise_for_status()
            return response.json()
    except Exception as e:
        logger.error(f"✗ Error fetching remote workers: {e}")
        return None


@router.get("/workers")
async def list_workers() -> Dict[str, Any]:
    """获取所有 Workers 及其日志流地址
    
    支持三种工作模式：
    1. 本地 quant-strategy-manager（默认）
    2. 远程 Worker API（REMOTE_WORKER_API 环境变量）
    3. 本地优先，远程备用
    
    Returns:
        {
            "total_workers": 3,
            "active_configs": 2,
            "deployment_mode": "local|remote",
            "workers": {
                "user123_002050.SZ_hidden_dragon": {
                    "alive": True,
                    "stats": {
                        "symbol": "002050.SZ",
                        "strategy": "hidden_dragon",
                        "state": "running",
                        "position": 500,
                        "bars_processed": 1234
                    },
                    "log_stream_url": "ws://192.168.1.100:54321"
                },
                ...
            }
        }
    
    Example:
        # 本地模式
        curl http://localhost:3001/api/strategy/workers
        
        # 远程模式
        REMOTE_WORKER_API=http://192.168.1.100:3001/api/strategy/workers \\
        uvicorn main:app --port 3001
    """
    
    # 尝试远程 API（如果配置）
    if REMOTE_WORKER_API:
        remote_data = await get_remote_workers()
        if remote_data:
            return {**remote_data, "deployment_mode": "remote"}
    
    # 回退到本地 orchestrator
    orchestrator = get_orchestrator()
    
    if not orchestrator:
        return {
            "total_workers": 0,
            "active_configs": 0,
            "workers": {},
            "deployment_mode": "unavailable",
            "note": "Strategy Manager not available. Either:\n"
                   "1. Run locally: python -m strategy_manager.cli start\n"
                   "2. Set remote API: REMOTE_WORKER_API=http://host:port/api/strategy/workers"
        }
    
    try:
        status = orchestrator.get_status()
        
        # 转换为前端期望的格式
        workers = {}
        for key, worker_info in status.get('workers', {}).items():
            log_stream_url = worker_info.get('log_stream_url', '')
            
            workers[key] = {
                'alive': worker_info.get('alive', False),
                'stats': worker_info.get('stats', {}),
                'log_stream_url': log_stream_url
            }
        
        return {
            'total_workers': status.get('total_workers', 0),
            'active_configs': status.get('active_configs', 0),
            'workers': workers,
            'deployment_mode': 'local'
        }
    except Exception as e:
        logger.error(f"Error getting workers status: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Error fetching workers: {str(e)}"
        )


@router.get("/workers/{worker_key}")
async def get_worker(worker_key: str) -> Dict[str, Any]:
    """获取单个 Worker 的详细信息
    
    Args:
        worker_key: Worker 标识 (e.g., "user123_002050.SZ_hidden_dragon")
    
    Returns:
        {
            "symbol": "002050.SZ",
            "strategy": "hidden_dragon",
            "state": "running",
            "log_stream_url": "ws://192.168.1.100:54321",
            "stats": {...}
        }
    
    Example:
        curl http://localhost:8000/api/strategy/workers/user123_002050.SZ_hidden_dragon
    """
    orchestrator = get_orchestrator()
    
    if not orchestrator:
        raise HTTPException(
            status_code=503,
            detail="Strategy Manager not available"
        )
    
    try:
        status = orchestrator.get_status()
        worker_info = status.get('workers', {}).get(worker_key)
        
        if not worker_info:
            raise HTTPException(
                status_code=404,
                detail=f"Worker '{worker_key}' not found"
            )
        
        stats = worker_info.get('stats', {})
        return {
            'symbol': stats.get('symbol'),
            'strategy': stats.get('strategy'),
            'state': stats.get('state'),
            'log_stream_url': worker_info.get('log_stream_url'),
            'alive': worker_info.get('alive'),
            'stats': stats
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting worker {worker_key}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Error fetching worker: {str(e)}"
        )


@router.get("/workers/{worker_key}/console")
async def get_worker_console_url(worker_key: str) -> Dict[str, str]:
    """获取 Worker 的日志流 WebSocket URL（用于打开日志查看器）
    
    Args:
        worker_key: Worker 标识
    
    Returns:
        {
            "worker_key": "user123_002050.SZ_hidden_dragon",
            "log_stream_url": "ws://192.168.1.100:54321"
        }
    
    Example:
        curl http://localhost:8000/api/strategy/workers/user123_002050.SZ_hidden_dragon/console
    """
    # 远程模式
    if REMOTE_WORKER_API:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                # 移除路径中多余的部分，确保正确构建 URL
                base_url = REMOTE_WORKER_API.rstrip('/workers')
                response = await client.get(f"{base_url}/workers/{worker_key}/console")
                response.raise_for_status()
                data = response.json()
                
                # Fix WebSocket URL (0.0.0.0 → actual host)
                if 'log_stream_url' in data:
                    ws_url = data['log_stream_url']
                    if '0.0.0.0' in ws_url:
                        # 从 REMOTE_WORKER_API 提取主机名
                        from urllib.parse import urlparse
                        parsed = urlparse(REMOTE_WORKER_API)
                        actual_host = parsed.hostname or 'localhost'
                        data['log_stream_url'] = ws_url.replace('0.0.0.0', actual_host)
                
                return data
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=e.response.status_code, detail=str(e))
        except Exception as e:
            logger.error(f"Error fetching console URL from remote API: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    # 本地模式（已弃用）
    orchestrator = get_orchestrator()
    
    if not orchestrator:
        raise HTTPException(
            status_code=503,
            detail="Strategy Manager not available"
        )
    
    try:
        status = orchestrator.get_status()
        worker_info = status.get('workers', {}).get(worker_key)
        
        if not worker_info:
            raise HTTPException(
                status_code=404,
                detail=f"Worker '{worker_key}' not found"
            )
        
        log_stream_url = worker_info.get('log_stream_url')
        if not log_stream_url:
            raise HTTPException(
                status_code=404,
                detail=f"Worker '{worker_key}' does not have log streaming enabled"
            )
        
        return {
            'worker_key': worker_key,
            'log_stream_url': log_stream_url
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting console URL for {worker_key}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Error fetching console URL: {str(e)}"
        )


@router.get("/workers/{worker_key}/logs")
@router.head("/workers/{worker_key}/logs")
async def get_worker_logs(worker_key: str, tail: int = None):
    """获取 Worker 的历史日志文件
    
    Args:
        worker_key: Worker 标识
        tail: 只返回最后 N 行（可选）
    
    Returns:
        日志文件内容（text/plain）
    
    Example:
        curl http://localhost:3001/api/strategy/workers/user123_002050.SZ_hidden_dragon/logs
        curl http://localhost:3001/api/strategy/workers/user123_002050.SZ_hidden_dragon/logs?tail=100
    """
    # 远程模式
    if REMOTE_WORKER_API:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                base_url = REMOTE_WORKER_API.rstrip('/workers')
                url = f"{base_url}/workers/{worker_key}/logs"
                if tail:
                    url += f"?tail={tail}"
                
                response = await client.get(url)
                response.raise_for_status()
                
                # 返回原始文本内容
                from fastapi.responses import PlainTextResponse
                return PlainTextResponse(response.text)
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
        except Exception as e:
            logger.error(f"Error fetching logs from remote API: {e}")
            raise HTTPException(
                status_code=500,
                detail=f"无法获取历史日志: {str(e)}"
            )
    
    # 本地模式不支持
    raise HTTPException(
        status_code=503,
        detail="Log file access only available in remote mode"
    )


@router.get("/status")
async def get_orchestrator_status() -> Dict[str, Any]:
    """获取 Strategy Manager 的整体状态
    
    Returns:
        {
            "healthy": True,
            "total_workers": 3,
            "active_workers": 2,
            "active_configs": 2,
            "last_update": "2024-01-15T10:30:45Z"
        }
    
    Example:
        curl http://localhost:3001/api/strategy/status
    """
    
    # 优先使用远程 API
    if REMOTE_WORKER_API:
        try:
            # 构造远程状态 API 的 URL
            # 从 /api/workers 改为 /api/status
            status_url = REMOTE_WORKER_API.replace('/api/workers', '/api/status')
            
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(status_url)
                response.raise_for_status()
                return response.json()
        except Exception as e:
            logger.error(f"Error fetching remote status: {e}")
            # 如果远程失败，尝试从 workers 数据推断
            try:
                workers_data = await get_remote_workers()
                if workers_data:
                    workers = workers_data.get('workers', {})
                    alive_workers = sum(1 for w in workers.values() if w.get('alive'))
                    return {
                        'healthy': True,
                        'total_workers': workers_data.get('total_workers', 0),
                        'active_workers': alive_workers,
                        'active_configs': workers_data.get('active_configs', 0),
                        'deployment_mode': 'remote'
                    }
            except Exception as e2:
                logger.error(f"Error inferring status from workers: {e2}")
    
    # 本地 orchestrator 模式已废弃
    orchestrator = get_orchestrator()
    
    if not orchestrator:
        return {
            'healthy': False,
            'message': 'Strategy Manager not available. Please configure REMOTE_WORKER_API or start api_server.py',
            'total_workers': 0,
            'active_workers': 0,
            'active_configs': 0,
            'deployment_mode': 'unavailable'
        }
    
    try:
        status = orchestrator.get_status()
        workers = status.get('workers', {})
        alive_workers = sum(1 for w in workers.values() if w.get('alive'))
        
        return {
            'healthy': True,
            'total_workers': status.get('total_workers', 0),
            'active_workers': alive_workers,
            'active_configs': status.get('active_configs', 0),
            'timestamp': status.get('timestamp'),
            'deployment_mode': 'local'
        }
    except Exception as e:
        logger.error(f"Error getting orchestrator status: {e}")
        return {
            'healthy': False,
            'message': str(e),
            'total_workers': 0,
            'active_workers': 0,
            'active_configs': 0,
            'deployment_mode': 'error'
        }


# 初始化时尝试加载 orchestrator
@router.on_event("startup")
async def startup():
    """应用启动时初始化 orchestrator"""
    orchestrator = get_orchestrator()
    if orchestrator:
        logger.info("✓ Strategy Manager Workers API ready")
    else:
        logger.warning("⚠ Strategy Manager Workers API in standby mode (quant-strategy-manager not available)")


@router.on_event("shutdown")
async def shutdown():
    """应用关闭时清理资源"""
    global _orchestrator
    if _orchestrator:
        try:
            _orchestrator = None
            logger.info("Strategy Manager resources cleaned up")
        except Exception as e:
            logger.error(f"Error during shutdown: {e}")
