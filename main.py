from dotenv import load_dotenv

# 加载 .env 文件中的环境变量
load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from routers.auth import router as auth_router
from routers.user_watchlist import router as user_watchlist_router
from routers.user_config import router as user_config_router
from routers.stock import router as stock_router
from routers.market import router as market_router
from routers.admin import router as admin_router
from routers.analysis import router as analysis_router
from routers.records import router as records_router
from routers.stock_ranking import router as stock_ranking_router
from routers import index_data
from routers.securities_account import router as sec_account_router
from routers.strategy import router as strategy_router
from routers.trade_execution import router as trade_execution_router
from routers.worker_monitor import router as worker_monitor_router
from routers.notification import router as notification_router
from routers.trader import router as trader_router
from routers.worker_data import router as worker_data_router
from routers.analysis_details import router as analysis_details_router
from routers.trading_manual import router as trading_manual_router
from routers.backtest import router as backtest_router
from routers.permissions import router as permissions_router
from routers.strategy_pool import router as strategy_pool_router
from routers.strategy_workers import router as strategy_workers_router
from routers.limit_up_ladder import router as ladder_router
from routers.scheduler_admin import router as scheduler_admin_router
from routers.sector_concept import router as sector_concept_router
from routers.hot_stock import router as hot_stock_router
from routers.etf import router as etf_router
from routers.data_pulse import router as data_pulse_router
from routers.earnings import router as earnings_router
from routers.navigation_policy import user_router as navigation_user_router
from routers.entitlements_matrix import router as entitlements_matrix_router
from production_config import configure_production_app
import logging
from contextlib import asynccontextmanager
from scheduler import start_scheduler, stop_scheduler


class _NoiseFilter(logging.Filter):
    """Filter out high-frequency, low-value access log entries."""
    _SKIP = ("/health", "/api/backtest/tasks/pending/poll")

    def filter(self, record):
        msg = record.getMessage()
        return not any(p in msg for p in self._SKIP)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Apply after uvicorn has configured its own logging
    logging.getLogger("uvicorn.access").addFilter(_NoiseFilter())
    logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
    logging.getLogger("apscheduler.scheduler").setLevel(logging.WARNING)
    # Startup
    start_scheduler()
    yield
    # Shutdown
    stop_scheduler()

app = FastAPI(lifespan=lifespan)

# 应用生产环境配置
configure_production_app(app)

# 注册各功能模块路由
# app.include_router(user_router, prefix="/api/user", tags=["用户"])  # 已迁移到 auth_router 和 user_watchlist
app.include_router(auth_router, prefix="/api/auth", tags=["认证"])
app.include_router(user_watchlist_router, prefix="/api/user", tags=["自选股"])
app.include_router(user_config_router, prefix="/api/user", tags=["用户配置"])
app.include_router(navigation_user_router, prefix="/api/user", tags=["主导航"])
app.include_router(stock_router, prefix="/api/stock", tags=["股票"])
app.include_router(market_router, prefix="/api", tags=["市场分析"])
app.include_router(admin_router, prefix="/api/admin", tags=["管理员"])
# Admin 权限相关：权益矩阵
app.include_router(entitlements_matrix_router, prefix="/api/admin", tags=["权限与导航配置"])
app.include_router(analysis_router, prefix="/api", tags=["AI分析"])
app.include_router(records_router, prefix="/api", tags=["记录查询"])
app.include_router(stock_ranking_router, prefix="/api", tags=["股票排名"])
app.include_router(index_data.router, prefix="/api/index", tags=["指数数据"])
app.include_router(sec_account_router, prefix="/api/user", tags=["账户"])
app.include_router(strategy_router,prefix="/api", tags=["策略"])
app.include_router(trade_execution_router, prefix="/api", tags=["交易执行"])
app.include_router(worker_monitor_router, prefix="/api", tags=["Worker监控"])
app.include_router(notification_router, prefix="/api", tags=["通知提醒"])
app.include_router(trader_router, prefix="/api", tags=["交易管理"])
app.include_router(worker_data_router, prefix="/api", tags=["Worker数据"])
app.include_router(analysis_details_router, prefix="/api/analysis", tags=["全量分析数据"])
app.include_router(trading_manual_router, prefix="/api", tags=["交易操作手册"])
app.include_router(backtest_router, prefix="/api", tags=["回测系统"])
app.include_router(permissions_router, prefix="/api/admin", tags=["权限管理"])
app.include_router(strategy_pool_router, prefix="/api/strategy-pool", tags=["策略池"])
app.include_router(strategy_workers_router, prefix="/api/workers", tags=["策略集群管理"])
app.include_router(ladder_router, prefix="/api", tags=["连板天梯"])
app.include_router(scheduler_admin_router, prefix="/api/admin/scheduler", tags=["定时任务管理"])
app.include_router(sector_concept_router, prefix="/api", tags=["板块与概念解析"])
app.include_router(hot_stock_router, prefix="/api", tags=["盘中热股捕捉"])
app.include_router(etf_router, prefix="/api", tags=["ETF智能分析"])
app.include_router(data_pulse_router, prefix="/api", tags=["数据脉搏健康度"])
app.include_router(earnings_router, prefix="/api", tags=["财报猎手"])