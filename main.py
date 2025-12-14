from fastapi import FastAPI
from routers.user import router as user_router
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


app = FastAPI()

# 注册各功能模块路由
app.include_router(user_router, prefix="/api/user", tags=["用户"])
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