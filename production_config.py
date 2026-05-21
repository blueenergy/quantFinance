# 生产环境优化配置

import os
import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from prometheus_fastapi_instrumentator import Instrumentator
import time


def silence_pymongo_debug_logs():
    """Force pymongo debug noise down to WARNING."""
    for name in (
        "pymongo",
        "pymongo.topology",
        "pymongo.driver",
        "pymongo.command",
        "pymongo.connection",
        "pymongo.serverSelection",
    ):
        logger = logging.getLogger(name)
        logger.setLevel(logging.WARNING)


def silence_http_debug_logs():
    """Force httpx/httpcore debug noise down to WARNING."""
    for name in (
        "httpx",
        "httpcore",
        "httpcore.connection",
        "httpcore.http11",
        "httpcore.http2",
        "httpcore.proxy",
    ):
        logger = logging.getLogger(name)
        logger.setLevel(logging.WARNING)


def setup_production_middleware(app: FastAPI):
    """配置生产环境中间件"""
    
    # 0. CORS中间件 (必须最先添加)
    allowed_origins = os.getenv("ALLOWED_ORIGINS", "*").split(",")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins if allowed_origins != ["*"] else ["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"]
    )
    
    # 1. 信任的主机中间件 (仅在非测试环境启用)
    if os.getenv("TESTING") != "true" and os.getenv("DISABLE_TRUSTED_HOST") != "true":
        trusted_hosts = os.getenv("TRUSTED_HOSTS", "*").split(",")
        if trusted_hosts != ["*"]:
            app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts)
    
    # 2. HTTPS重定向中间件 (仅在生产环境)
    # 2. HTTPS重定向中间件 (仅在生产环境)
    #- if os.getenv("ENVIRONMENT") == "production":
    #-     from fastapi.middleware.httpsredirect import HTTPSRedirectMiddleware
    #-     app.add_middleware(HTTPSRedirectMiddleware)
    
    # 3. Gzip压缩中间件
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    
    # 4. 限流配置
    limiter = Limiter(key_func=get_remote_address)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    
    # 5. 安全头中间件
    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        return response
    
    # 6. 请求处理时间记录
    @app.middleware("http")
    async def add_process_time_header(request: Request, call_next):
        start_time = time.time()
        response = await call_next(request)
        process_time = time.time() - start_time
        response.headers["X-Process-Time"] = str(process_time)
        return response
    
    return limiter

def setup_logging():
    """配置生产环境日志"""
    
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    log_file = os.getenv("LOG_FILE", "./logs/app.log")
    
    try:
        # 创建日志目录
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
    except OSError:
        # 如果无法创建目录（如在只读文件系统中），则只使用控制台处理器
        print(f"无法创建日志目录 {os.path.dirname(log_file)}, 切换到控制台日志")
        # 配置根日志器 - 只使用控制台处理器
        logging.basicConfig(
            level=getattr(logging, log_level),
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            handlers=[
                logging.StreamHandler()  # 只使用控制台处理器
            ]
        )
        return
    
    # 配置日志格式
    log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    
    # 配置根日志器
    logging.basicConfig(
        level=getattr(logging, log_level),
        format=log_format,
        handlers=[
            # 文件处理器 (带轮转)
            RotatingFileHandler(
                log_file,
                maxBytes=10*1024*1024,  # 10MB
                backupCount=5
            ),
            # 控制台处理器
            logging.StreamHandler()
        ]
    )
    
    # 设置第三方库日志级别
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    silence_http_debug_logs()
    silence_pymongo_debug_logs()

def setup_monitoring(app: FastAPI):
    """配置监控和指标"""
    
    # Prometheus指标
    instrumentator = Instrumentator()
    instrumentator.instrument(app)
    instrumentator.expose(app)
    
    # 自定义健康检查端点
    @app.get("/health")
    async def health_check():
        """健康检查端点"""
        try:
            # 检查数据库连接
            from pymongo import MongoClient
            mongo_url = os.getenv("MONGO_URI") or "mongodb://localhost:27017"
            client = MongoClient(mongo_url)
            client.admin.command('ping')
            
            return {
                "status": "healthy",
                "timestamp": datetime.now().isoformat(),
                "version": os.getenv("APP_VERSION", "1.0.0"),
                "environment": os.getenv("ENVIRONMENT", "development"),
                "database": "connected"
            }
        except Exception as e:
            logging.error(f"Health check failed: {e}")
            return {
                "status": "unhealthy",
                "timestamp": datetime.now().isoformat(),
                "error": str(e)
            }
    
    # 指标端点
    @app.get("/metrics")
    async def get_metrics():
        """暴露应用指标"""
        return {"message": "Metrics available at /metrics endpoint"}

def setup_database_pool():
    """配置数据库连接池"""
    from motor.motor_asyncio import AsyncIOMotorClient
    
    mongodb_url = os.getenv("MONGO_URI") or "mongodb://localhost:27017"
    max_pool_size = int(os.getenv("DB_POOL_SIZE", "10"))
    min_pool_size = int(os.getenv("DB_POOL_MIN_SIZE", "5"))
    
    client = AsyncIOMotorClient(
        mongodb_url,
        maxPoolSize=max_pool_size,
        minPoolSize=min_pool_size,
        maxIdleTimeMS=45000,
        connectTimeoutMS=10000,
        serverSelectionTimeoutMS=10000,
    )
    
    return client

# 应用启动事件
async def startup_event():
    """应用启动时的初始化"""
    logging.info("🚀 悟空量化金融智能助手启动中...")
    
    # 初始化数据库连接
    # app.state.db_client = setup_database_pool()
    
    logging.info("✅ 应用启动完成")

# 应用关闭事件  
async def shutdown_event():
    """应用关闭时的清理"""
    logging.info("⏹️ 悟空量化金融智能助手关闭中...")
    
    # 关闭数据库连接
    # if hasattr(app.state, 'db_client'):
    #     app.state.db_client.close()
    
    logging.info("✅ 应用关闭完成")

# 生产环境配置函数
def configure_production_app(app: FastAPI):
    """配置生产环境应用"""
    
    # 设置日志
    setup_logging()
    
    # 设置中间件
    limiter = setup_production_middleware(app)
    
    # 设置监控
    setup_monitoring(app)
    
    # 设置生命周期事件
    app.add_event_handler("startup", startup_event)
    app.add_event_handler("shutdown", shutdown_event)
    
    return limiter

# 限流装饰器示例
def rate_limit(requests_per_minute: int = 60):
    """限流装饰器工厂"""
    def decorator(func):
        return limiter.limit(f"{requests_per_minute}/minute")(func)
    return decorator

# 使用示例:
# @app.get("/api/data")
# @rate_limit(100)  # 每分钟最多100次请求
# async def get_data(request: Request):
#     return {"data": "example"}
