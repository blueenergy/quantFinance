import os
from pymongo import MongoClient
from pymongo.collection import Collection
from typing import Dict
from dotenv import load_dotenv
load_dotenv()

# MongoDB connection settings
mongo_url = os.getenv("MONGO_URI", os.getenv("MONGO_URL", "mongodb://localhost:27017"))
DB_NAME = os.getenv("MONGO_DB", "finance")
ANALYZER_DB_NAME = os.getenv("ANALYZER_DB_NAME", "quant_analyzer")
data_db_name = os.getenv("DATA_DB_NAME", "quant_data")

# Singleton client (reuse across app)
client = MongoClient(mongo_url)
db = client[DB_NAME]
# Separate DB for quantAnalyzer outputs (read-only in quantFinance)
analyzer_db = client[ANALYZER_DB_NAME]
# Separate DB for raw external data (hot stocks, news, etc.)
data_db = client[data_db_name]

import logging as _logging
_logging.getLogger(__name__).info(
    "MongoDB connected: main=%s, analyzer=%s, data=%s",
    DB_NAME, ANALYZER_DB_NAME, data_db_name
)

def get_db_collections() -> Dict[str, Collection]:
    """
    Return a dict of commonly used collections on the main finance DB.
    Analyzer-written collections live in quant_analyzer; use get_analyzer_db().
    Usage:
        collections = get_db_collections()
        user_col = collections['users']
    """
    return {
        # User and auth
        "users": db["users"],
        "login_logs": db["login_logs"],
        "admin_logs": db["admin_logs"],
        "user_watchlists": db["user_watchlists"],
        "watchlist_strategies": db["watchlist_strategies"],
        "securities_accounts": db["securities_accounts"],
        # Trading
        "trade_executions": db["trade_executions"],  # 交易执行记录
        "trade_signals": db["trade_signals"],  # 交易信号
        # Position and account management (from quantTrader)
        "trader_positions": db["trader_positions"],  # 实时持仓快照
        "trader_accounts": db["trader_accounts"],  # 账户资金信息
        "position_snapshots": db["position_snapshots"],  # 历史持仓快照（每日）
        # Strategy
        "strategy_template": db["strategy_template"],  # 策略参数模板
        "strategy_metadata": db["strategy_metadata"],  # 策略元数据（key和name）
        "strategy_stock_pool": db["strategy_stock_pool"],  # 策略选股池结果
        "strategy_trade_history": db["strategy_trade_history"],  # 策略选股池结果
        # Worker monitoring
        "worker_status": db["worker_status"],  # Worker 状态和心跳
        "worker_activity": db["worker_activity"],  # Worker 活动日志
        # Distributed execution control plane
        "worker_nodes": db["worker_nodes"],  # 执行节点（机器）注册和状态
        "worker_groups": db["worker_groups"],  # Worker 组（任务单元）配置和状态
        # Notification system
        "notifications": db["notifications"],  # 通知记录
        "notification_settings": db["notification_settings"],  # 通知设置
        # Backtesting system
        "backtest_tasks": db["backtest_tasks"],  # 回测任务
        "backtest_results": db["backtest_results"],  # 回测结果
        # Permission and service upgrade
        "upgrade_requests": db["upgrade_requests"],  # 服务升级申请
        # Phase 10: Async Task Queue Architecture
        "analysis_tasks": db["analysis_tasks"],  # 深度分析任务队列
        "quota_usage": db["quota_usage"],        # 用户配额使用情况
        "schedule_config": db["schedule_config"], # 定时任务调度配置
        "app_settings": db["app_settings"],  # 全局可配置项（如主导航按等级可见性）
        # 评分等仍落在主库；AI/分析结果请用 get_analyzer_db()
        "stock_scores": db["stock_scores"],
        # Stock reference data lives in quant_data
        "stock_info": data_db["stock_info"],
    }

def get_collection(name: str):
    """Get a single collection by name."""
    return db[name]

def get_analyzer_db():
    """
    Return a dict of collections from the quantAnalyzer output DB (quant_analyzer).
    quantFinance uses these for read-only access to AI analysis results.
    Write access belongs exclusively to quantAnalyzer.
    """
    return {
        "analysis_history": analyzer_db["analysis_history"],
        "limit_up_analysis": analyzer_db["limit_up_analysis"],
        "analysis_tags": analyzer_db["analysis_tags"],
        "etf_analysis": analyzer_db["etf_analysis"],
        "global_analysis": analyzer_db["global_analysis"],
        "hot_stock_analysis": analyzer_db["hot_stock_analysis"],
        "sector_concept_analysis": analyzer_db["sector_concept_analysis"],
        "portfolio_analysis": analyzer_db["portfolio_analysis"],
        "market_analysis_cache": analyzer_db["market_analysis_cache"],
    }

def get_data_db():
    """Get quant_data database collections (raw external data)."""
    return {
        "hot_stock_raw": data_db["hot_stock_raw"],
        # Market data
        "volume_price": data_db["volume_price"],
        "money_flow": data_db["money_flow"],
        "minute_bars": data_db["minute_bars"],
        # Limit-up/down ladder
        "daily_limit_up": data_db["daily_limit_up"],
        "daily_limit_down": data_db["daily_limit_down"],
        "limit_up_ladder": data_db["limit_up_ladder"],
        "limit_down_ladder": data_db["limit_down_ladder"],
        "sector_flow": data_db["sector_flow"],
        # Market sentiment
        "market_spectrum": data_db["market_spectrum"],
        "market_spectrum_minute": data_db["market_spectrum_minute"],
        # ETF data
        "etf_prices": data_db["etf_prices"],
        "etf_basic": data_db["etf_basic"],
        "etf_daily": data_db["etf_daily"],
        "etf_adj_factor": data_db["etf_adj_factor"],
        "etf_shares": data_db["etf_shares"],
        "etf_share_size": data_db["etf_share_size"],
        # Index reference/meta data
        "index_meta": data_db["index_meta"],
        "index_constituents": data_db["index_constituents"],
        # Fund holdings snapshots (if sync task enabled)
        "fund_holdings": data_db["fund_holdings"],
        # Stock reference data
        "stock_info": data_db["stock_info"],
        "stock_basic": data_db["stock_basic"],
        # Financial statements
        "financial_daily_basic": data_db["financial_daily_basic"],
        "financial_income": data_db["financial_income"],
        "financial_balance": data_db["financial_balance"],
        "financial_cashflow": data_db["financial_cashflow"],
        "financial_indicator": data_db["financial_indicator"],
        # Earnings data
        "earnings_forecast": data_db["earnings_forecast"],
        "earnings_express": data_db["earnings_express"],
        "earnings_disclosure": data_db["earnings_disclosure"],
        "earnings_report_rc": data_db["earnings_report_rc"],
        # Industry data
        "market_industry_quotes_daily": data_db["market_industry_quotes_daily"],
        "market_etf_quotes_daily": data_db["market_etf_quotes_daily"],
        "industry_limitup_stats": data_db["industry_limitup_stats"],
        # Cached international headlines for global analysis/data pulse
        "international_news_cache": data_db["international_news_cache"],
        # Global quotes (yfinance sync from quant-data-engine)
        "global_market_quotes": data_db["global_market_quotes"],
    }

def close_db():
    """Close the MongoDB connection (if needed)."""
    client.close()