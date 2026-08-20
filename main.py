from dotenv import load_dotenv
import logging
import os
# 加载 .env 文件中的环境变量
load_dotenv()


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


silence_pymongo_debug_logs()


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


silence_http_debug_logs()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from routers.auth import router as auth_router
from routers.user_watchlist import router as user_watchlist_router
from routers.theme_lag_validation_subscription import router as theme_lag_validation_subscription_router
from routers.user_config import router as user_config_router
from routers.stock import router as stock_router
from routers.stock_workbench import router as stock_workbench_router
from routers.market import router as market_router
from routers.daily_gold import router as daily_gold_router
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
from routers.portfolio_plans import router as portfolio_plans_router
from routers.symbol_risk import router as symbol_risk_router
from routers.symbol_opportunity import router as symbol_opportunity_router
from routers.portfolio_research import router as portfolio_research_router
from routers.factor_backtest import router as factor_backtest_router
from routers.permissions import router as permissions_router
from routers.strategy_pool import router as strategy_pool_router
from routers.strategy_workers import router as strategy_workers_router
from routers.limit_up_ladder import router as ladder_router
from routers.risk_report import router as risk_report_router
from routers.market_regime import router as market_regime_router
from routers.market_stats import router as market_stats_router
from routers.x_influencer import router as x_influencer_router
from routers.scheduler_admin import router as scheduler_admin_router
from routers.sector_concept import router as sector_concept_router
from routers.hot_stock import router as hot_stock_router
from routers.etf import router as etf_router
from routers.data_pulse import router as data_pulse_router
from routers.market_events import router as market_events_router
from routers.earnings import router as earnings_router
from routers.analysis_tasks import router as analysis_tasks_router
from routers.shenwan_index import router as shenwan_index_router
from routers.assistant_bff import router as assistant_bff_router
from routers.hermes_quant_tools import (
    MCP_ACTION_TAG,
    MCP_READ_TAG,
    router as hermes_quant_tools_router,
)
from routers.macro import router as macro_router
from routers.kline_series import router as kline_series_router
from routers.intraday_t0 import router as intraday_t0_router
from routers.market_insights import router as market_insights_router
from routers.entropy_ranking import router as entropy_ranking_router
from routers.screeners import router as screeners_router
from routers.agent_screening_tools import router as agent_screening_tools_router
from routers.navigation_policy import user_router as navigation_user_router
from routers.entitlements_matrix import router as entitlements_matrix_router
from production_config import configure_production_app
from contextlib import asynccontextmanager
import asyncio
import os
from scheduler import start_scheduler, stop_scheduler, write_scheduler_status


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
    embedded_scheduler_enabled = os.getenv("ENABLE_EMBEDDED_SCHEDULER", "false").lower() in {"1", "true", "yes"}
    scheduler_heartbeat_task = None

    async def _embedded_scheduler_heartbeat():
        interval = int(os.getenv("SCHEDULER_HEARTBEAT_INTERVAL_SECONDS", "15"))
        while True:
            try:
                write_scheduler_status("running")
            except Exception as exc:
                logging.getLogger(__name__).warning("embedded scheduler heartbeat failed: %s", exc)
            await asyncio.sleep(interval)

    if embedded_scheduler_enabled:
        start_scheduler()
        scheduler_heartbeat_task = asyncio.create_task(_embedded_scheduler_heartbeat())
    try:
        yield
    finally:
        if scheduler_heartbeat_task:
            scheduler_heartbeat_task.cancel()
            try:
                await scheduler_heartbeat_task
            except asyncio.CancelledError:
                pass
        if embedded_scheduler_enabled:
            try:
                write_scheduler_status("stopped")
            except Exception:
                pass
            stop_scheduler()

app = FastAPI(lifespan=lifespan)
_MCP_SERVER_ENABLED = os.getenv("ENABLE_MCP_SERVER", "false").strip().lower() in ("1", "true", "yes", "on")
_MCP_SERVER_MODE = os.getenv("MCP_SERVER_MODE", "all").strip().lower()
_MCP_ONLY_PROCESS = _MCP_SERVER_ENABLED and _MCP_SERVER_MODE in {"read", "actions"}

# 应用生产环境配置
configure_production_app(app)

# 注册各功能模块路由
# app.include_router(user_router, prefix="/api/user", tags=["用户"])  # 已迁移到 auth_router 和 user_watchlist
app.include_router(auth_router, prefix="/api/auth", tags=["认证"])
app.include_router(user_watchlist_router, prefix="/api/user", tags=["自选股"])
app.include_router(theme_lag_validation_subscription_router, prefix="/api/user", tags=["主题补涨验证订阅"])
app.include_router(user_config_router, prefix="/api/user", tags=["用户配置"])
app.include_router(navigation_user_router, prefix="/api/user", tags=["主导航"])
app.include_router(stock_router, prefix="/api/stock", tags=["股票"])
app.include_router(stock_workbench_router, prefix="/api/stock", tags=["股票工作台"])
app.include_router(market_router, prefix="/api", tags=["市场分析"])
app.include_router(daily_gold_router, prefix="/api", tags=["每日金股"])
app.include_router(admin_router, prefix="/api/admin", tags=["管理员"])
# Admin 权限相关：权益矩阵
app.include_router(entitlements_matrix_router, prefix="/api/admin", tags=["权限与导航配置"])
app.include_router(analysis_router, prefix="/api", tags=["AI分析"])
app.include_router(records_router, prefix="/api", tags=["记录查询"])
app.include_router(stock_ranking_router, prefix="/api", tags=["股票排名"])
app.include_router(index_data.router, prefix="/api/index", tags=["指数数据"])
app.include_router(sec_account_router, prefix="/api/user", tags=["账户"])
app.include_router(strategy_router,prefix="/api", tags=["策略"])
app.include_router(trader_router, prefix="/api", tags=["交易管理"])
app.include_router(trade_execution_router, prefix="/api", tags=["交易执行"])
app.include_router(worker_monitor_router, prefix="/api", tags=["Worker监控"])
app.include_router(notification_router, prefix="/api", tags=["通知提醒"])
app.include_router(worker_data_router, prefix="/api", tags=["Worker数据"])
app.include_router(analysis_details_router, prefix="/api/analysis", tags=["全量分析数据"])
app.include_router(trading_manual_router, prefix="/api", tags=["交易操作手册"])
app.include_router(backtest_router, prefix="/api", tags=["回测系统"])
app.include_router(portfolio_plans_router, prefix="/api", tags=["组合交易计划"])
app.include_router(symbol_risk_router, prefix="/api", tags=["个股风险台账"])
app.include_router(symbol_opportunity_router, prefix="/api", tags=["个股机会台账"])
app.include_router(portfolio_research_router, prefix="/api", tags=["组合研究"])
app.include_router(factor_backtest_router, prefix="/api", tags=["因子回测"])
# 兼容前端与后台两套历史路径：
# - /api/permissions/...（前端在用）
# - /api/admin/permissions/...（后台旧路径）
app.include_router(permissions_router, prefix="/api", tags=["权限管理"])
app.include_router(permissions_router, prefix="/api/admin", tags=["权限管理"])
app.include_router(strategy_pool_router, prefix="/api/strategy-pool", tags=["策略池"])
if not _MCP_ONLY_PROCESS:
    # The strategy workers router probes REMOTE_WORKER_API during startup.
    # MCP-only read/actions processes do not serve strategy worker APIs, so
    # skip it to avoid unrelated local-dev DNS errors.
    app.include_router(strategy_workers_router, prefix="/api/strategy", tags=["策略集群管理"])
app.include_router(ladder_router, prefix="/api", tags=["连板天梯"])
app.include_router(risk_report_router, prefix="/api", tags=["市场风险预警"])
app.include_router(market_regime_router, prefix="/api", tags=["A股广度估值扫描"])
app.include_router(market_stats_router, prefix="/api", tags=["市场统计"])
app.include_router(x_influencer_router, prefix="/api", tags=["X大V情报"])
app.include_router(scheduler_admin_router, prefix="/api/admin/scheduler", tags=["定时任务管理"])
app.include_router(sector_concept_router, prefix="/api", tags=["板块与概念解析"])
app.include_router(hot_stock_router, prefix="/api", tags=["盘中热股捕捉"])
app.include_router(etf_router, prefix="/api", tags=["ETF智能分析"])
app.include_router(data_pulse_router, prefix="/api", tags=["数据脉搏健康度"])
app.include_router(market_events_router, prefix="/api", tags=["市场事件日历"])
app.include_router(earnings_router, prefix="/api", tags=["财报猎手"])
app.include_router(analysis_tasks_router, prefix="/api", tags=["分析任务中心"])
app.include_router(shenwan_index_router, prefix="/api/shenwan-index", tags=["申万行业指数"])
app.include_router(assistant_bff_router, prefix="/api/assistant", tags=["助手BFF"])
app.include_router(hermes_quant_tools_router, prefix="/api", tags=["Hermes quant tools"])
app.include_router(macro_router, prefix="/api", tags=["宏观分析"])
app.include_router(kline_series_router, prefix="/api", tags=["K线对比"])
app.include_router(intraday_t0_router, prefix="/api", tags=["日内T+0"])
app.include_router(market_insights_router, prefix="/api", tags=["火眼金睛"])
app.include_router(entropy_ranking_router, prefix="/api", tags=["熵值因子排名"])
app.include_router(screeners_router, prefix="/api", tags=["选股筛选"])
app.include_router(agent_screening_tools_router, prefix="/api", tags=["Agent screening tools"])

# ---------------------------------------------------------------------------
# MCP 服务器：暴露 quant-api 工具给 Hermes 等多智能体框架
# 端点：POST/GET/DELETE /mcp  (Streamable HTTP, MCP spec 2025-03-26)
# 鉴权：复用现有 HERMES_QUANT_INTERNAL_KEY (X-Hermes-Quant-Key header)
# 工具范围：按 MCP_SERVER_MODE 拆成 read/actions 两个工具面。
# 扩展：往 routers/hermes_quant_tools.py 加路由即可自动出现在 MCP 工具列表
# ---------------------------------------------------------------------------
if _MCP_SERVER_ENABLED:
    from fastapi import Depends
    from fastapi_mcp import FastApiMCP, AuthConfig
    from auth import require_hermes_quant_internal_key

    _mcp_instances = []

    def _mount_mcp_server(*, name: str, description: str, tags: list[str], mount_path: str) -> None:
        mcp = FastApiMCP(
            app,
            name=name,
            description=description,
            include_tags=tags,
            auth_config=AuthConfig(
                dependencies=[Depends(require_hermes_quant_internal_key)],
            ),
            # 将 MCP client headers 转发给底层路由的 Depends 校验。
            headers=["x-hermes-quant-key", "x-quant-client-key", "x-user-token"],
        )
        mcp.mount_http(mount_path=mount_path)
        _mcp_instances.append(mcp)

    _mcp_mode = _MCP_SERVER_MODE

    if _mcp_mode == "read":
        _mount_mcp_server(
            name="wukong-quant-read",
            description="Read-only A-share quantitative finance tools",
            tags=[MCP_READ_TAG],
            mount_path="/api/mcp",
        )
    elif _mcp_mode == "actions":
        _mount_mcp_server(
            name="wukong-quant-actions",
            description="Action tools for submitting analysis tasks and user mutations",
            tags=[MCP_ACTION_TAG],
            mount_path="/api/mcp",
        )
    else:
        _mount_mcp_server(
            name="wukong-quant-read",
            description="Read-only A-share quantitative finance tools",
            tags=[MCP_READ_TAG],
            mount_path="/api/mcp/read",
        )
        _mount_mcp_server(
            name="wukong-quant-actions",
            description="Action tools for submitting analysis tasks and user mutations",
            tags=[MCP_ACTION_TAG],
            mount_path="/api/mcp/actions",
        )
        # Backward-compatible combined endpoint for local/dev deployments.
        _mount_mcp_server(
            name="wukong-quant",
            description="A-share quantitative finance tools (read + actions)",
            tags=[MCP_READ_TAG, MCP_ACTION_TAG],
            mount_path="/api/mcp",
        )
