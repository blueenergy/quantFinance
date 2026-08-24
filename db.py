import os
from pymongo import MongoClient
from pymongo.collection import Collection
from typing import Dict
from dotenv import load_dotenv
load_dotenv()

# MongoDB connection settings
mongo_url = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DB_NAME = os.getenv("MONGO_DB", "finance")
ANALYZER_DB_NAME = os.getenv("ANALYZER_DB_NAME", "quant_analyzer")
data_db_name = os.getenv("DATA_DB_NAME", "quant_data")
realtime_db_name = os.getenv("REALTIME_DB_NAME", "quant_realtime")
trading_db_name = os.getenv("TRADING_DB_NAME", "quant_trading")

# Singleton client (reuse across app)
client = MongoClient(mongo_url)
db = client[DB_NAME]
# Separate DB for quantAnalyzer outputs (read-only in quantFinance)
analyzer_db = client[ANALYZER_DB_NAME]
# Separate DB for raw external data (hot stocks, news, etc.)
data_db = client[data_db_name]
# Separate DB for high-frequency hot snapshots.
realtime_db = client[realtime_db_name]
# Separate DB for execution-domain strategy plans and paper trading.
trading_db = client[trading_db_name]

import logging as _logging
_logging.getLogger(__name__).info(
    "MongoDB connected: main=%s, analyzer=%s, data=%s, realtime=%s",
    DB_NAME, ANALYZER_DB_NAME, data_db_name, realtime_db_name
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
        "trader_account_equity": db["trader_account_equity"],  # 账户级每日资金曲线
        "position_snapshots": db["position_snapshots"],  # 历史持仓快照（每日）
        # Intraday T+0 opportunity discovery
        "intraday_t0_mock_positions": db["intraday_t0_mock_positions"],
        "intraday_t0_signals": db["intraday_t0_signals"],
        "intraday_t0_position_snapshots": db["intraday_t0_position_snapshots"],
        "intraday_t0_signal_evaluations": db["intraday_t0_signal_evaluations"],
        "intraday_t0_trades": db["intraday_t0_trades"],
        "intraday_t0_execution_settings": db["intraday_t0_execution_settings"],
        "intraday_t0_executions": db["intraday_t0_executions"],
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
        "backtest_batches": db["backtest_batches"],  # 批量回测实验
        "strategy_lab_loops": db["strategy_lab_loops"],  # Strategy Lab 自动迭代
        # Permission and service upgrade
        "upgrade_requests": db["upgrade_requests"],  # 服务升级申请
        # Phase 10: Async Task Queue Architecture
        "analysis_tasks": db["analysis_tasks"],  # 深度分析任务队列
        "analysis_task_revisions": db["analysis_task_revisions"],  # 分析任务版本历史
        "quota_usage": db["quota_usage"],        # 用户配额使用情况
        "schedule_config": db["schedule_config"], # 定时任务调度配置
        "scheduler_status": db["scheduler_status"], # 独立 scheduler runner 状态
        "pipeline_tasks": db["pipeline_tasks"],
        "job_status": db["job_status"],
        "market_insight_events": db["market_insight_events"],
        "market_insight_focus_feedback": db["market_insight_focus_feedback"],
        "market_insight_focus_reviews": db["market_insight_focus_reviews"],
        "market_insight_user_preferences": db["market_insight_user_preferences"],
        "market_insight_global_quality": db["market_insight_global_quality"],
        # 主题补涨验证订阅（物化/通知水位线；见 theme_lag_validation_subscription_task）
        "theme_lag_validation_subscriptions": db["theme_lag_validation_subscriptions"],
        "app_settings": db["app_settings"],  # 全局可配置项（如主导航按等级可见性）
        # 评分等仍落在主库；AI/分析结果请用 get_analyzer_db()
        "stock_scores": db["stock_scores"],
        # details/strategy_scores split out of stock_scores: 93% of the bytes,
        # read only by the per-stock detail page
        "stock_score_details": db["stock_score_details"],
        "entropy_scores": db["entropy_scores"],
        # Stock reference data lives in quant_data
        "stock_info": data_db["stock_info"],
        "etf_basic": data_db["etf_basic"],
        "index_constituents": data_db["index_constituents"],
    }

def get_collection(name: str):
    """Get a single collection by name."""
    return db[name]


def get_trading_db_collections() -> Dict[str, Collection]:
    """Return collections from the quant_trading execution-domain database."""
    return {
        "portfolio_strategy_configs": trading_db["portfolio_strategy_configs"],
        "portfolio_trade_plans": trading_db["portfolio_trade_plans"],
        "portfolio_trade_plan_items": trading_db["portfolio_trade_plan_items"],
        "portfolio_plan_reviews": trading_db["portfolio_plan_reviews"],
        "portfolio_plan_risk_snapshots": trading_db["portfolio_plan_risk_snapshots"],
        "portfolio_paper_positions": trading_db["portfolio_paper_positions"],
        "portfolio_paper_executions": trading_db["portfolio_paper_executions"],
        "portfolio_paper_equity": trading_db["portfolio_paper_equity"],
        "portfolio_live_equity": trading_db["portfolio_live_equity"],
        "portfolio_plan_generation_tasks": trading_db["portfolio_plan_generation_tasks"],
        "portfolio_plan_operation_logs": trading_db["portfolio_plan_operation_logs"],
        "symbol_risk_operation_logs": trading_db["symbol_risk_operation_logs"],
        "symbol_opportunity_operation_logs": trading_db["symbol_opportunity_operation_logs"],
        "portfolio_worker_status": trading_db["portfolio_worker_status"],
        "portfolio_strategy_param_presets": trading_db["portfolio_strategy_param_presets"],
        "portfolio_research_jobs": trading_db["portfolio_research_jobs"],
        "portfolio_research_results": trading_db["portfolio_research_results"],
        "portfolio_research_reviews": trading_db["portfolio_research_reviews"],
        # Factor backtest queue: enqueued here, drained by the
        # quant-factor-researcher worker, which also publishes factor_catalog.
        "factor_backtest_jobs": trading_db["factor_backtest_jobs"],
        "factor_backtest_results": trading_db["factor_backtest_results"],
        "factor_catalog": trading_db["factor_catalog"],
        "portfolio_lineage_controls": trading_db["portfolio_lineage_controls"],
        "portfolio_trailing_stop_runs": trading_db["portfolio_trailing_stop_runs"],
        "portfolio_live_snapshots": trading_db["portfolio_live_snapshots"],
    }

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
        "legacy_portfolio_risk_llm": analyzer_db["legacy_portfolio_risk_llm"],
        "symbol_risk_findings": analyzer_db["symbol_risk_findings"],
        "industry_risk_findings": analyzer_db["industry_risk_findings"],
        "symbol_opportunity_findings": analyzer_db["symbol_opportunity_findings"],
        "symbol_strength_findings": analyzer_db["symbol_strength_findings"],
        "symbol_weakness_findings": analyzer_db["symbol_weakness_findings"],
        "industry_opportunity_findings": analyzer_db["industry_opportunity_findings"],
        "symbol_llm_signal_reviews": analyzer_db["symbol_llm_signal_reviews"],
        "market_analysis_cache": analyzer_db["market_analysis_cache"],
        # LLM interpretations of risk scan reports (written by quantAnalyzer)
        "risk_report_analysis": analyzer_db["risk_report_analysis"],
        # X (Twitter) influencer post LLM analysis
        "x_influencer_post_analysis": analyzer_db["x_influencer_post_analysis"],
        # Macro deep-dive analysis (china_macro / us_macro)
        "macro_analysis": analyzer_db["macro_analysis"],
        # A-share market breadth / valuation LLM interpretation
        "market_regime_analysis": analyzer_db["market_regime_analysis"],
        # Retrospective analysis quality evaluations
        "analysis_evaluations": analyzer_db["analysis_evaluations"],
        # Daily limit-up panorama narrative (written by quantAnalyzer)
        "limit_up_daily_narrative": analyzer_db["limit_up_daily_narrative"],
        # A-share multi-index IV snapshot + LLM interpretation (written by quantAnalyzer)
        "iv_analysis": analyzer_db["iv_analysis"],
        # Strategy Lab batch review output (written by quantAnalyzer)
        "backtest_batch_reviews": analyzer_db["backtest_batch_reviews"],
        "strategy_lab_loop_reports": analyzer_db["strategy_lab_loop_reports"],
    }

def get_data_db():
    """Get quant_data database collections (raw external data)."""
    return {
        "hot_stock_raw": data_db["hot_stock_raw"],
        # Market data
        "volume_price": data_db["volume_price"],
        "trade_calendar": data_db["trade_calendar"],
        "stock_adj_factor": data_db["stock_adj_factor"],
        "volume_price_realtime_latest": realtime_db["volume_price_realtime_latest"],
        "stock_weekly": data_db["stock_weekly"],
        "stock_monthly": data_db["stock_monthly"],
        "money_flow": data_db["money_flow"],
        "stk_nineturn": data_db["stk_nineturn"],
        "minute_bars": data_db["minute_bars"],
        # Limit-up/down ladder
        "daily_limit_up": data_db["daily_limit_up"],
        "daily_limit_down": data_db["daily_limit_down"],
        "limit_up_ladder": data_db["limit_up_ladder"],
        "limit_down_ladder": data_db["limit_down_ladder"],
        "sector_flow": data_db["sector_flow"],
        "theme_lag_candidates": data_db["theme_lag_candidates"],
        "theme_lag_candidate_validation": data_db["theme_lag_candidate_validation"],
        "daily_gold_snapshots": data_db["daily_gold_snapshots"],
        "daily_gold_lots": data_db["daily_gold_lots"],
        "daily_gold_twr": data_db["daily_gold_twr"],
        # Market sentiment
        "market_spectrum": data_db["market_spectrum"],
        "market_spectrum_minute": data_db["market_spectrum_minute"],
        "market_spectrum_realtime_daily": realtime_db["market_spectrum_realtime_daily"],
        "realtime_sector_strength": realtime_db["realtime_sector_strength"],
        # ETF data
        "etf_prices": data_db["etf_prices"],
        "etf_basic": data_db["etf_basic"],
        "etf_daily": data_db["etf_daily"],
        "etf_adj_factor": data_db["etf_adj_factor"],
        "etf_shares": data_db["etf_shares"],
        "etf_share_size": data_db["etf_share_size"],
        # Index reference/meta data (index_constituents: only quant_data; never finance)
        "index_meta": data_db["index_meta"],
        "index_constituents": data_db["index_constituents"],
        # Fund holdings snapshots (if sync task enabled)
        "fund_holdings": data_db["fund_holdings"],
        # Stock reference data
        "stock_info": data_db["stock_info"],
        "stock_basic": data_db["stock_basic"],
        "sw_index_member": data_db["sw_index_member"],
        "sw_daily": data_db["sw_daily"],
        "sw_weekly": data_db["sw_weekly"],
        "sw_monthly": data_db["sw_monthly"],
        # Financial statements
        "financial_daily_basic": data_db["financial_daily_basic"],
        "financial_income": data_db["financial_income"],
        "financial_balance": data_db["financial_balance"],
        "financial_cashflow": data_db["financial_cashflow"],
        "financial_indicator": data_db["financial_indicator"],
        "financial_dividend": data_db["financial_dividend"],
        # Shareholder / chips data
        "stk_holdernumber": data_db["stk_holdernumber"],
        "hk_hold": data_db["hk_hold"],
        "top10_floatholders": data_db["top10_floatholders"],
        "stk_holdertrade": data_db["stk_holdertrade"],
        "share_float": data_db["share_float"],
        "repurchase": data_db["repurchase"],
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
        "market_events": data_db["market_events"],
        # Global quotes (yfinance sync from quant-data-engine)
        "global_market_quotes": data_db["global_market_quotes"],
        # Global market daily OHLCV history (synced by run_global_market_daily_sync)
        "global_market_daily": data_db["global_market_daily"],
        # Market risk scan results (written by quant-data-engine risk_scanner task)
        "risk_reports": data_db["risk_reports"],
        "market_breadth_daily": data_db["market_breadth_daily"],
        "index_cape_daily": data_db["index_cape_daily"],
        "market_regime_reports": data_db["market_regime_reports"],
        "index_daily_basic": data_db["index_daily_basic"],
        "market_pe_daily": data_db["market_pe_daily"],
        "margin_market_daily": data_db["margin_market_daily"],
        # X (Twitter) influencer tweets (written by quant-data-engine run_x_influencer_sync)
        "x_influencer_posts": data_db["x_influencer_posts"],
        # China macro rates/monthly/quarterly (written by run_cn_macro_sync)
        "cn_macro_rates": data_db["cn_macro_rates"],
        "cn_macro_monthly": data_db["cn_macro_monthly"],
        "cn_macro_quarterly": data_db["cn_macro_quarterly"],
        # US Treasury rates (written by run_us_rates_sync)
        "us_rates_daily": data_db["us_rates_daily"],
    }

def close_db():
    """Close the MongoDB connection (if needed)."""
    client.close()