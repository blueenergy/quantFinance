# 组合生命周期与状态来源（Portfolio Lifecycle & State Sources）

> 目的：用一套清晰的概念和命名，统一“组合总览 / 组合交易计划”相关的
> 后端字段、API 契约和前端展示，减少“想到哪做到哪”导致的歧义与 bug。
> 适用范围：`quantFinance/routers/portfolio_plans.py` 及
> `quantFinance-dashboard` 中的组合相关页面。

---

## 1. 一等概念（First-class concepts）

| 概念 | 含义 | 数据来源 | 关键标识 |
|---|---|---|---|
| **Portfolio / Lineage** | 一套策略参数下的长期组合；其 `execution_venue` 表示走实盘还是纸面通道 | 按 `(user_id, strategy_template_id, params_hash)` 聚合 | `params_hash` |
| **Plan（期）** | 某个 base_date 上对组合的一次具体计划（调仓 / 监控 / 手工补录） | `portfolio_trade_plans` | `plan_id` |
| **Signal（信号/订单）** | plan 派生出的可执行委托；纸面或实盘 | `trade_signals`（`mode=live/paper`） | `order_id` |
| **Execution（成交）** | 实盘订单的成交回报 | `trade_executions` | `exec_id` |
| **Holdings（持仓）** | 组合当前真实持仓；实盘按成交流水重建，纸面按最新快照 | 成交流水 / paper snapshot | symbol → shares |
| **券商持仓（broker）** | 券商账户实时同步的仓位（miniQMT 等） | `trader_positions` | account_id |

“足球队”心智模型：**首发 = 当前真实持仓**（场上 11 人），**替补席 = 评分靠前但
未持仓的候选**（板凳 ~1.5×），教练（人）可单点换人，算法负责持续推荐。

---

## 2. 关键枚举口径

- **execution_venue**（原 `funding_mode`，已重命名）：`live` / `paper`
  - 含义：**成交场所（execution venue）**——`live` = 向券商发单成交，
    `paper` = 纸面账本模拟。
  - **不是**资金属性；旧名 `funding_mode` 易误解为“资金模式”，故重命名。
  - **权威字段**物化于 `portfolio_lineage_controls.execution_venue`（见
    [portfolio_domain_model.md](./portfolio_domain_model.md)）；读路径在
    未回填时 fallback 到 `trade_signals.mode=live` 推断。

- **plan_type**：`rebalance` / `monitor` / `manual`
  - `rebalance`：到期调仓计划（参与调仓节奏 cadence）。
  - `monitor`：每日监控计划，内含“替补可上场”的可交易选项。
  - `manual`：手工补录（miniQMT 操作回填），**不影响** cadence。

- **origin**：`strategy` / `manual`
  - 仅 `origin=strategy` 的计划参与 base_date 唯一性约束。

- **is_rebalance_day**：该 plan 是否为调仓日（手工补录恒为 `False`）。

---

## 3. 状态来源规则（避免歧义的硬规定）

1. **`execution_venue` 判定**：lineage 只要存在 `trade_signals(mode=live)`
   即为 `live`；`trade_executions` 仅用于“持仓重建”，**不作为是否实盘的判据**。
   （否则“已发实盘单但无成交回报”的组合会被判成 paper，甚至从
   `/live-portfolios` 消失——这是历史 bug 的根因。）

2. **首发（持仓）来源**：首发 = 当前真实持仓，由 `_current_lineage_holdings`
   统一给出（实盘走成交流水重建，纸面走最新 snapshot）。**不再**用 plan items
   当首发，保证“今日阵容”与“最新持仓”始终一致。

3. **替补席来源**：从 `stock_scores` 现算评分排序，**剔除**所有当前持仓
   （按 suffix 不敏感的 symbol key 比较），bench 容量 = `top_n × 1.5`（可调）。

4. **券商对账**：实盘组合需对比“系统账本持仓”与 `trader_positions`，
   差异第一时间在总览页提示（`/plans/{plan_id}/holdings-reconcile`）。

5. **cadence（调仓节奏）**：只数 `plan_type=rebalance` 的计划；`manual`/`monitor`
   不推进 / 不取消既定调仓日。

---

## 4. 主要 API（组合相关）

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/portfolio/plans` (list) | 列出所有 lineage（含 `execution_venue`） |
| GET | `/portfolio-plans/portfolios` | 轻量 lineage 列表（身份 + venue；无 paper 摘要） |
| GET | `/portfolio-plans/plans/{plan_id}/portfolio-summary` | 延迟加载纸面快照、持仓数、权益、审批策略 |
| GET | `/portfolio/live-portfolios` | 仅 `execution_venue == 'live'` 的组合 |
| GET | `/plans/{plan_id}/bench` | 今日阵容（持仓首发）+ 替补席 |
| GET | `/plans/{plan_id}/holdings-reconcile` | 账本 vs 券商持仓对账 |
| POST | `/plans/{plan_id}/force-rebalance` | 慢思考：入队重算 / 强制建仓 |
| POST | `live/paper-rebalance`（含 `dry_run`） | 快思考：单点换人 / 减仓清仓预览+执行 |

---

## 5. 命名清理清单

| 现状（易混） | 建议命名 | 理由 |
|---|---|---|
| `funding_mode`（同时表达“资金”和“是否实盘”） | **`execution_venue`** | 实质是“成交场所：券商/纸面”，与“资金”无关；并明确判据以 signal 为准 |
| “首发 = plan items” | “首发 = 当前持仓” | 与“最新持仓”天然一致 |

---

## 6. 落地顺序

1. 写本文档，统一概念与口径（本文件）。
2. 按 §5 做纯重命名（含 `funding_mode → execution_venue`，全仓 3 文件）
   + §3.1 的 live 判据修复。
3. 按 §3.2/§3.3 让首发取持仓、替补剔除持仓。
4. 按 §3.4 接入券商对账提示。
5. 回归测试 + 前端构建。
