# 股票研究工作台：数据地图 + BFF Section 接口契约（迭代计划）

> **状态**：计划草案（待评审 / 分批实施）
> **创建**：2026-06-12
> **owner**：（待指派）
> **关联代码**：
> - 后端 BFF：`quantFinance/routers/stock_workbench.py`（当前单体 `/{symbol}/workbench`）
> - 前端：`quantFinance-dashboard/src/views/StockWorkbench.vue`（当前 4 个 Tab）
> - 数据字典：`quantFinance/docs/MONGODB_COLLECTIONS.md`
> - 库映射：`quantFinance/db.py`（`get_db_collections()` / `get_data_db()`）

---

## 1. 背景与产品定位

工作台要回答的核心问题只有一句：**"我现在要不要关注/操作这只票？"**

它不是同花顺 F10 的复刻，差异化定位在三点：

1. **量化优先**：以系统自有的多维评分（technical / fundamental / value / growth / money_flow / cycle / sentiment）和评分历史为骨架，而非堆砌公开财务报表。
2. **AI 闭环**：把"深度分析历史 + 分析结果评估（事后对错）"作为一等公民，让用户看到 AI 过往判断的命中率，而不是只看一次性结论。
3. **研究闭环**：行情/资金/筹码 → 评分 → AI 解读 → 自选/交易，在一个页面内打通。

### 现状（baseline）

- **后端**：单体端点 `GET /api/stock/{symbol}/workbench` 一次性返回 `stock / quote / score / deep_analysis / data_status`。
- **前端**：4 个 Tab —— `总览`、`量化评分`、`财务快照`、`深度分析`。
- **痛点**：
  - 单体聚合，任何一个慢查询拖垮整页；新增模块只能继续往这个端点塞，迟早变巨石。
  - 大量已入库的、可归属到个股的数据没有被工作台消费（见第 3 节数据地图）。
  - `sentiment` 维度在前端 `SCORE_DEFS` 里占了一格，但后端综合分权重为 `0.0`（见 `stock-scoring-system/stock_analyzer.py`，`"sentiment": 0.0  # 情绪分析暂不计入综合分`），且 `batch_update_composite_score.py` 的 composite 计算根本不含 sentiment——**等于占位但无有效内容**。
  - ETF 标的进工作台时无分支处理（评分/财务报表对 ETF 不适用）。

---

## 2. 架构方向：Section 化按需加载

把单体 BFF 拆成**总览骨架 + 多个 section 子端点**，前端 Tab 切换时按需拉取，未访问的 section 不查询。

### 2.1 端点设计

| 端点 | 用途 | 说明 |
|---|---|---|
| `GET /api/stock/{symbol}/workbench` | 总览骨架 | 仅返回 identity、最新 quote、评分摘要、最近一条 AI 分析、以及各 section 的 `data_status`（是否有数据 + 新鲜度），**不**内联重数据。 |
| `GET /api/stock/{symbol}/workbench/quote` | 行情资金 section | K 线/分钟线指针、资金流、量价。 |
| `GET /api/stock/{symbol}/workbench/scores` | 量化评分 section | 当前分项 + 评分历史（趋势）。 |
| `GET /api/stock/{symbol}/workbench/financials` | 财务业绩 section | 财务三表摘要、daily_basic、业绩预告/快报。 |
| `GET /api/stock/{symbol}/workbench/holders` | 股东筹码 section | 主题/连板/资金集中度等可归属个股的筹码面。 |
| `GET /api/stock/{symbol}/workbench/ai` | AI 与交易 section | 分析历史、分析评估、自选/持仓/交易信号。 |

> 端点粒度可按实施进度合并/拆分，关键是**总览不内联重数据**，每个 section 独立可缓存、可降级。

### 2.2 统一 `data_status` 契约

每个 section 响应都带一个统一的状态块，前端据此渲染"有数据/无数据/数据过期"的徽标，避免空 Tab 让用户困惑：

```json
{
  "success": true,
  "data": { "...": "section payload" },
  "data_status": {
    "found": true,
    "as_of": "2026-06-11",          // 该 section 数据的业务日期
    "stale": false,                  // 是否超过预期刷新窗口
    "expected_freshness": "T+1 收盘后", // 人类可读的新鲜度预期
    "source": "quant_data.volume_price"
  }
}
```

> 新鲜度窗口的权威来源：共享库 catalog `data-access-lib/src/stock_data_access/catalogs/data_pipelines_catalog.yaml`（经 `stock_data_access.catalog_path()` 解析），可复用 `quantFinance/services/data_monitoring.py` 的新鲜度逻辑。

---

## 3. 数据地图（集合 → 工作台分区）

> 库归属以 `db.py` 实际映射为准：`finance`=主库，`quant_data`=行情/基础，`quant_realtime`=实时快照。

| 分区 | 集合 | 库 | 工作台用途 | 优先级 |
|---|---|---|---|---|
| 行情资金 | `volume_price` | quant_data | 日线 K 线、量价 | P0 |
| 行情资金 | `volume_price_realtime_latest` | quant_realtime | 最新实时价（已用于总览 quote） | P0 |
| 行情资金 | `minute_bars` | quant_data | 分钟 K 线（盘中视图） | P1 |
| 行情资金 | `money_flow` | quant_data | 主力/大中小单资金流 | P0 |
| 行情资金 | `financial_daily_basic` | quant_data | 市值、换手率、PE/PB/股息率 | P0 |
| 量化评分 | `stock_scores` | finance | 分项评分 + 评分历史趋势 | P0 |
| 财务业绩 | `financial_income` / `financial_balance` / `financial_cashflow` / `financial_indicator` | quant_data | 三表 + ROE 等指标 | P1 |
| 财务业绩 | `earnings_forecast` / `earnings_express` / `earnings_disclosure` / `earnings_report_rc` | quant_data | 业绩预告/快报/披露日历 | P1 |
| 股东筹码/主题 | `daily_limit_up` / `daily_limit_down` | quant_data | 个股涨跌停记录、连板属性 | P1 |
| 股东筹码/主题 | `limit_up_ladder` | quant_data | 连板天梯中的位置/情绪 | P2 |
| 股东筹码/主题 | `theme_lag_candidates` / `theme_lag_candidate_validation` | quant_data | 题材补涨候选标记 | P2 |
| 行业上下文 | `sw_index_member` | quant_data | 申万行业归属（总览已用） | P0 |
| 行业上下文 | `market_industry_quotes_daily` / `industry_limitup_stats` | quant_data | 所属行业强弱/涨停统计对比 | P2 |
| AI 与交易 | `analysis_history` / `analysis_logs` | finance | 个股 AI 深度分析历史 + 推理日志 | P0 |
| AI 与交易 | `analysis_evaluations`（待确认集合名/位置） | finance | **AI 历史判断的事后命中评估**（差异化核心） | P1 |
| AI 与交易 | `user_watchlists` / `watchlist_strategies` | finance | 是否已自选 + 绑定策略 | P1 |
| AI 与交易 | `positions` / `position_snapshots` | finance | 是否持仓 + 成本/盈亏 | P1 |
| AI 与交易 | `trade_signals` / `trade_executions` | finance | 该票相关信号与成交 | P2 |
| ETF 分支 | `etf_basic` / `etf_prices` / `etf_daily` / `etf_shares` | quant_data | ETF 标的专属视图（见第 6 节） | P2（later） |

> 注：`analysis_evaluations` 在数据字典中未单列，实施前需先确认实际集合名与写入方（疑似在 AI 分析评估流程中产生）。若暂不存在，则该能力降级为 P2 并先补齐数据生产。

---

## 4. 目标信息架构（Tab）

从当前 4 Tab 扩展为 6 Tab，对应 section 端点：

| Tab | 内容 | 数据 section | 优先级 |
|---|---|---|---|
| 总览 | identity、实时价、评分雷达摘要、最近 AI 摘要、各模块状态徽标 | 骨架端点 | P0（已有，需补状态徽标） |
| 行情资金 | K 线、量价、资金流、daily_basic 估值 | `/quote` | P0 |
| 财务业绩 | 三表摘要、ROE 等指标、业绩预告/快报 | `/financials` | P1 |
| 股东筹码 | 涨跌停/连板属性、题材标记、（后续股东数） | `/holders` | P1/P2 |
| 主题事件 | 行业强弱对比、连板天梯位置、题材补涨 | （可并入 holders 或独立） | P2 |
| AI 与交易 | 分析历史、分析评估命中率、自选/持仓/信号 | `/ai` | P0（历史）/P1（评估） |

> 当前的 `量化评分`、`深度分析` Tab 分别并入"总览/行情资金"与"AI 与交易"。`财务快照` 升级为"财务业绩"。

---

## 5. 实施优先级与里程碑

### P0 — 闭环骨架（先做，价值密度最高）
1. BFF 拆分：总览骨架瘦身 + `/quote`、`/scores`、`/ai`（历史）三个 section 端点，统一 `data_status` 契约。
2. 前端 Tab 重排为目标 IA，按需加载；总览补"模块状态徽标"。
3. 行情资金 Tab：日线 K 线 + 资金流 + daily_basic 估值。
4. 评分历史趋势（`stock_scores` 按 `score_date` 拉时间序列），不止显示当前快照。

### P1 — 差异化与深化
5. AI 分析评估命中率（`analysis_evaluations`，先确认数据源；缺失则先补生产）。
6. 财务业绩 Tab：三表摘要 + 指标 + 业绩预告/快报。
7. 自选/持仓/信号状态接入 AI 与交易 Tab。
8. **修复 `sentiment` 维度**：要么补齐 sentiment 数据生产并给定有效权重后展示，要么前端先隐藏该格（避免"占位无内容"），二选一并在 PR 中说明决策（详见 6.1）。

### P2 — 增量与 ETF
9. 分钟 K 线盘中视图、连板天梯位置、题材补涨、行业强弱对比。
10. **ETF 分支视图**（详见 6.2）。
11. 交易信号/成交明细。

---

## 6. 关键专项与注意事项

### 6.1 sentiment 维度（当前未有效展示）
- **现状**：前端 `SCORE_DEFS` 含 `sentiment`，但后端综合分权重 `0.0`，composite 计算不含它 → 实际是空占位。
- **处置**：P1 阶段二选一——
  - (A) 接入 `stock-scoring-system/scorers/sentiment_scorer.py` 的产出，确认其是否真正写入 `stock_scores.sentiment_score`，给定非零权重后展示；或
  - (B) 在产品决策明确前，前端先隐藏该维度，避免误导。
- **决策点**：sentiment 的"产品含义"需明确（是舆情/资金情绪/盘口情绪？口径不清不要硬展示）。

### 6.2 ETF 支持（later，P2）
- ETF 标的没有财务三表、个股评分多数维度不适用。
- 进入工作台时需按 `etf_basic` 判定标的类型并**走分支 IA**：用 `etf_prices`/`etf_daily`/`etf_shares`（规模/份额）替代财务与评分 Tab。
- 本轮先保证个股路径完善，ETF 分支排在个股闭环之后。

### 6.3 权限 / entitlements（暂不实现，仅记一笔）
- 当前**不做** section 级权限控制；所有 section 对登录用户开放。
- 留待后续：如需按服务等级限制（参考 `entitlements_matrix.py` 现有模式），再为高成本 section（如 AI 评估、分钟线）加 gating。**此处仅作备忘，本轮不投入。**

### 6.4 Symbol 一致性（实施前必查）
- 各集合的代码字段不统一：`symbol` / `ts_code` / `con_code` 混用（`stock_workbench.py` 已用 `_symbol_candidates` + `_find_first` 兜底）。
- 新增 section 查询时必须复用 `_symbol_candidates`，并对 `volume_price`(symbol) / `sw_index_member`(con_code) / `stock_scores`(symbol) 等分别验证字段。

### 6.5 文档滞后（本轮已"顺手"修复）
- ✅ `MONGODB_COLLECTIONS.md` 顶部"数据库名：finance"更正为**多库架构说明**（finance / quant_data / quant_realtime / quant_trading / quant_analyzer 及各自 env 覆盖）。
- ✅ `job_status` 行的"权威管线列表"指向更正为共享库 catalog `data-access-lib/.../data_pipelines_catalog.yaml`（`quantFinance/config/...` 降级为 fallback）。
- 待办：`analysis_evaluations` 等若确实在用但未入字典，实施时补录。

---

## 7. 验收口径（每个 section 通用）
- 有数据：正确渲染 + 显示 `as_of` 业务日期。
- 无数据：显示空态而非报错；总览徽标置灰。
- 过期：`stale=true` 时给出"数据可能滞后"提示及预期新鲜度。
- 性能：总览骨架 P95 < 现状；section 独立超时不影响其他 Tab。
- 测试：每个新 section 端点补 `quantFinance/tests/unit/` 单测（沿用 monkeypatch DB 的现有模式）。
