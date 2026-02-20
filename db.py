import os
from pymongo import MongoClient
from pymongo.collection import Collection
from typing import Dict
from dotenv import load_dotenv
load_dotenv()

# MongoDB connection settings
mongo_url = os.getenv("DB_CONNECTION_URL", os.getenv("MONGO_URL", "mongodb://localhost:27017"))
DB_NAME = "finance"

# Singleton client (reuse across app)
client = MongoClient(mongo_url)
db = client[DB_NAME]

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
        "portfolio_analysis": db["portfolio_analysis"],  # 组合机会分析历史
        "analysis_tags": db["analysis_tags"],  # AI 分析标签
        "analysis_logs": db["analysis_logs"],  # AI 分析详单
        # Phase 10: Async Task Queue Architecture
        "analysis_tasks": db["analysis_tasks"],  # 深度分析任务队列
        "quota_usage": db["quota_usage"],        # 用户配额使用情况
        "global_analysis": db["global_analysis"], # 全球市场分析结果
        "market_analysis": db["market_analysis"], # 盘中/盘后大盘分析结果
        "schedule_config": db["schedule_config"], # 定时任务调度配置
        # Add more collections as needed
    }

def get_collection(name: str):
    """Get a single collection by name."""
    return db[name]

def close_db():
    """Close the MongoDB connection (if needed)."""
    client.close()