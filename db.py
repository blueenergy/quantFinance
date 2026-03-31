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
    Return a dict of commonly used collections.
    Usage:
        collections = get_db_collections()
        user_col = collections['users']
    """
    return {
        "users": db["users"],
        "stock_info": db["stock_info"],
        "stock_scores": db["stock_scores"],
        "volume_price": db["volume_price"],
        "analysis_history": db["analysis_history"],
        "login_logs": db["login_logs"],
        "admin_logs": db["admin_logs"],
        "market_analysis_cache": db["market_analysis_cache"],
        "user_watchlists": db["user_watchlists"],
        "watchlist_strategies": db["watchlist_strategies"],
        "securities_accounts": db["securities_accounts"],
        "money_flow": db["money_flow"],  # 资金流数据
        "market_spectrum": db["market_spectrum"],  # 阴阳谱数据
        "market_spectrum_minute": db["market_spectrum_minute"],  # 分钟级阴阳谱数据
        "strategy_template": db["strategy_template"],  # 策略参数模板
        "strategy_metadata": db["strategy_metadata"],  # 策略元数据（key和name）
        "trade_executions": db["trade_executions"],  # 交易执行记录
        "trade_signals": db["trade_signals"],  # 交易信号
        "minute_bars": db["minute_bars"],  # 分钟K线数据
        # Worker monitoring
        "worker_status": db["worker_status"],  # Worker 状态和心跳
        "worker_activity": db["worker_activity"],  # Worker 活动日志
        # Distributed execution control plane
        "worker_nodes": db["worker_nodes"],  # 执行节点（机器）注册和状态
        "worker_groups": db["worker_groups"],  # Worker 组（任务单元）配置和状态
        # Notification system
        "notifications": db["notifications"],  # 通知记录
        "notification_settings": db["notification_settings"],  # 通知设置
        # Position and account management (from quantTrader)
        "trader_positions": db["trader_positions"],  # 实时持仓快照
        "trader_accounts": db["trader_accounts"],  # 账户资金信息
        "position_snapshots": db["position_snapshots"],  # 历史持仓快照（每日）
        # Backtesting system
        "backtest_tasks": db["backtest_tasks"],  # 回测任务
        "backtest_results": db["backtest_results"],  # 回测结果
        # Permission and service upgrade
        "upgrade_requests": db["upgrade_requests"],  # 服务升级申请
        # Strategy selection results
        "strategy_stock_pool": db["strategy_stock_pool"],  # 策略选股池结果
        "strategy_trade_history": db["strategy_trade_history"],  # 策略选股池结果"
        # Limit-up ladder system (连板天梯)
        "daily_limit_up": db["daily_limit_up"],  # 每日涨停股票
        "limit_up_ladder": db["limit_up_ladder"],  # 连板天梯聚合数据
        "sector_flow": db["sector_flow"],  # 板块资金流向
        # Portfolio analysis
        "portfolio_analysis": db["portfolio_analysis"],  # 组合机会分析历史
        # ETF data
        "etf_basic": db["etf_basic"],
        "etf_daily": db["etf_daily"],
        "analysis_tags": db["analysis_tags"],  # AI 分析标签
        "analysis_logs": db["analysis_logs"],  # AI 分析详单
        # Phase 10: Async Task Queue Architecture
        "analysis_tasks": db["analysis_tasks"],  # 深度分析任务队列
        "quota_usage": db["quota_usage"],        # 用户配额使用情况
        "global_analysis": db["global_analysis"], # 全球市场分析结果
        "market_analysis": db["market_analysis"], # 盘中/盘后大盘分析结果
        "schedule_config": db["schedule_config"], # 定时任务调度配置
        "daily_limit_down": db["daily_limit_down"], # 每日跌停数据
        "limit_down_ladder": db["limit_down_ladder"], # 跌停天梯数据
        # Sector concept analysis (DC / THS)
        "sector_concept_analysis": db["sector_concept_analysis"],  # 概念板块分析结果
        # Hot stock analysis (THS / DC)
        "hot_stock_analysis": db["hot_stock_analysis"],  # 热股分析结果（每次更新一条）
        # Add more collections as needed
        "stock_basic": db["stock_basic"]
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
    }

def close_db():
    """Close the MongoDB connection (if needed)."""
    client.close()