# MongoDB Collections 数据字典

> **数据库名**：`finance`  
> **最后更新**：2026-02-26

---

## 用户与权限

| 集合 | 说明 | 文档数 |
|------|------|--------|
| `users` | 用户账号（用户名、密码哈希、邮箱、服务等级等） | 8 |
| `login_logs` | 登录记录（IP、User-Agent、成功/失败） | 445 |
| `admin_logs` | 管理员操作日志（审批、修改用户等） | 193 |
| `notifications` | 站内通知（新用户注册待审批等） | 6 |
| `upgrade_requests` | 用户服务等级升级申请 | 1 |
| `quota_usage` | 用户 API/功能配额使用量 | 1 |

---

## 持仓与账户

| 集合 | 说明 | 文档数 |
|------|------|--------|
| `positions` | 用户当前持仓（实时） | 0 |
| `position_snapshots` | 持仓每日快照（历史） | 9 |
| `trader_accounts` | 交易账户信息（资金、可用现金） | 3 |
| `trader_positions` | 交易员层持仓（对接券商接口） | 16 |
| `securities_accounts` | 已绑定的券商账户 | 3 |
| `user_watchlists` | 用户自选股列表 | 10 |
| `watchlist_strategies` | 自选股绑定的策略配置 | 36 |

---

## 交易信号与执行

| 集合 | 说明 | 文档数 |
|------|------|--------|
| `trade_signals` | 策略产生的交易信号（含下单参数） | 41 |
| `trade_executions` | 信号对应的实际成交记录 | 103 |
| `strategy_states` | 策略运行状态（仓位、累计盈亏等） | 47 |
| `runtime_status` | 策略实时运行状态（是否活跃、异常原因） | 48 |
| `worker_status` | Worker（交易机器人）在线状态 | 0 |
| `worker_activity` | Worker 活动日志（轮询、分析、买卖等操作） | 933 |
| `worker_checkpoints` | Worker 数据同步断点（记录最后处理到哪天） | 22 |

---

## 策略配置

| 集合 | 说明 | 文档数 |
|------|------|--------|
| `strategy_metadata` | 策略元信息（名称、可回测/实盘、风险提示） | 11 |
| `strategy_template` | 策略参数 Preset 模板（如 `dragon_conservative`） | 28 |
| `strategy_execution_logs` | 策略回调执行日志 | 1 |

---

## 回测

| 集合 | 说明 | 文档数 |
|------|------|--------|
| `backtest_tasks` | 回测任务队列（前端提交、worker 认领） | 32 |
| `backtest_results` | 回测结果（收益曲线、胜率、最大回撤等） | 33 |
| `strategy_stock_pool` | 每日选股结果（screening 产生的买入信号） | 0 |
| `strategy_trade_history` | Screening 回测的历史交易记录（买卖信号） | 0 |

---

## AI 分析

| 集合 | 说明 | 文档数 |
|------|------|--------|
| `analysis_tasks` | AI 分析任务队列（个股标签、投资组合、市场分析） | 57 |
| `analysis_history` | 个股 AI 分析结果历史（含 LLM 模型信息） | 63 |
| `analysis_logs` | 个股分析详细日志（含推理过程、新闻上下文） | 299 |
| `portfolio_analysis` | 持仓/自选股 AI 分析结果（按用户存储） | 15 |
| `market_analysis_cache` | A 股市场整体分析缓存（每日） | 88 |
| `global_analysis` | 全球市场联动 AI 分析结果 | 15 |

---

## 市场行情数据

| 集合 | 说明 | 文档数 |
|------|------|--------|
| `volume_price` | **A 股日线行情**（主表，量价数据，约 1600 万条） | 16,308,566 |
| `minute_bars` | **A 股分钟 K 线**（28 万条） | 287,123 |
| `stock_info` | 股票基本信息（代码、名称、行业、PE、PB 等） | 5,435 |
| `financial_daily_basic` | 每日财务基础数据（市值、换手率、股息率等） | 1,212,984 |
| `money_flow` | 资金流向（大中小单买卖量） | 1,171,452 |
| `market_spectrum` | 市场强弱分布（单阳/单阴/均线上下个股数） | 5,123 |
| `stock_scores` | 个股综合评分（技术/资金/基本面/周期） | 293,987 |

---

## 财务报告数据

| 集合 | 说明 | 文档数 |
|------|------|--------|
| `financial_income` | 利润表（收入、费用、净利润等） | 102,119 |
| `financial_balance` | 资产负债表 | 90,116 |
| `financial_cashflow` | 现金流量表 | 94,951 |
| `financial_indicator` | 财务指标（ROE、资产周转率等） | 72,255 |

---

## 指数与 ETF

| 集合 | 说明 | 文档数 |
|------|------|--------|
| `index_prices` | 指数日线行情（沪深 300、创业板等） | 1,220 |
| `index_meta` | 指数元信息 | 12 |
| `etf_prices` | ETF 日线行情 | 1,708 |

---

## 全球市场

| 集合 | 说明 | 文档数 |
|------|------|--------|
| `global_market_quotes` | 全球行情快照（美股指数、商品、加密、外汇，实时更新） | 35 |

---

## 连板天梯

| 集合 | 说明 | 文档数 |
|------|------|--------|
| `daily_limit_up` | 每日涨停股票明细（板数、行业、是否跟风） | 667 |
| `limit_up_ladder` | 连板天梯汇总（分层数据 + 情绪指标 + 板块排行） | 9 |

---

## 系统运维

| 集合 | 说明 | 文档数 |
|------|------|--------|
| `collect_state` | 数据采集状态（记录最后同步日期） | 1 |
| `job_status` | 数据采集任务运行记录（`job_type`+`date` 唯一）；权威管线列表见仓库 `quantFinance/config/data_pipelines_catalog.yaml`，写入见 `quant_data_engine.utils.ingestion_job.record_ingestion_run` | — |
