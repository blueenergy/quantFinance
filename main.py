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


app = FastAPI()

# CORS 配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 生产环境应该指定具体域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册各功能模块路由
# app.include_router(user_router, prefix="/api/user", tags=["用户"])  # 已迁移到 auth_router 和 user_watchlist
app.include_router(auth_router, prefix="/api/auth", tags=["认证"])
app.include_router(user_watchlist_router, prefix="/api/user", tags=["自选股"])
app.include_router(user_config_router, prefix="/api/user", tags=["用户配置"])
app.include_router(stock_router, prefix="/api/stock", tags=["股票"])
app.include_router(market_router, prefix="/api", tags=["市场分析"])
app.include_router(admin_router, prefix="/api/admin", tags=["管理员"])
app.include_router(analysis_router, prefix="/api", tags=["AI分析"])
app.include_router(records_router, prefix="/api", tags=["记录查询"])
app.include_router(stock_ranking_router, prefix="/api", tags=["股票排名"])
app.include_router(index_data.router, prefix="/api/index", tags=["指数数据"])
app.include_router(sec_account_router, prefix="/api/user", tags=["账户"])
app.include_router(strategy_router,prefix="/api", tags=["策略"])
app.include_router(trade_execution_router, prefix="/api", tags=["交易执行"])
app.include_router(worker_monitor_router, prefix="/api", tags=["Worker监控"])
app.include_router(notification_router, prefix="/api", tags=["通知提醒"])
app.include_router(trader_router, prefix="/api", tags=["交易客户端"])
app.include_router(worker_data_router, prefix="/api/worker-data", tags=["Worker数据访问"])
app.include_router(analysis_details_router, prefix="/api", tags=["分析详情"])
app.include_router(trading_manual_router, prefix="/api", tags=["手动交易"])