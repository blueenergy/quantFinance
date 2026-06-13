# 主力资金流入流出：个股工作台 + 申万行业研究 落地设计

> **状态**：设计草案（待评审 / 分批实施）
> **创建**：2026-06-14
> **owner**：（待指派）
> **关联代码**：
> - 个股 BFF：`quantFinance/routers/stock_workbench.py`（`_quote_section`，常量 `WORKBENCH_DAILY_KLINE_LIMIT = 360`）
> - 个股前端：`quantFinance-dashboard/src/views/StockWorkbench.vue`（行情资金 Tab，`moneyFlowMetrics`）
> - 申万 BFF：`quantFinance/routers/shenwan_index.py`（`sw_index_classify` / `sw_daily` / `sw_index_member`）
> - 申万前端：`quantFinance-dashboard/src/views/ShenwanIndustryIndex.vue`
> - 数据源：`quant_data.money_flow`（Tushare moneyflow，窗口 `20240102` 起）
> - 数据管线：`quant-data-engine` `run_moneyflow_sync` / `run_sw_industry_backfill`

---

## 1. 需求

1. **个股工作台 · 行情资金页**：展示某只股票每天的主力资金（超大单 + 大单）流入流出，含趋势与累计。
2. **申万行业研究**：展示某个申万行业的主力资金流入流出（由成分股聚合）。

核心问题：放进现有 K 线模块，还是单独模块？

**结论**：都做成**独立模块**，与 K 线并列；K 线组件保持单一职责，资金流副图能力后续用独立 ECharts option 实现，不改 `shenwanKlineOption`。

---

## 2. 数据基础与约束（务必先读）

### 2.1 `money_flow` 字段（Tushare moneyflow）

按四档大小单拆分的买卖额/量（金额单位 **万元**）：

| 档位 | 买入额 | 卖出额 |
|---|---|---|
| 小单 sm | `buy_sm_amount` | `sell_sm_amount` |
| 中单 md | `buy_md_amount` | `sell_md_amount` |
| 大单 lg | `buy_lg_amount` | `sell_lg_amount` |
| 超大单 elg | `buy_elg_amount` | `sell_elg_amount` |

净额字段：`net_mf_amount`（万元）、`net_mf_vol`（手）。

### 2.2 口径定义（统一,避免重复/矛盾）

```text
超大单净额 elg_net = buy_elg_amount - sell_elg_amount
大单净额   lg_net  = buy_lg_amount  - sell_lg_amount
主力净额   main_net = elg_net + lg_net
中单净额   md_net  = buy_md_amount - sell_md_amount
小单净额   sm_net  = buy_sm_amount - sell_sm_amount
```

- `net_mf_amount` 通常即“主力净额（大单+超大单）”。**UI 只展示一个主力净额**，以自算 `main_net` 为准，`net_mf_amount` 仅作为后端一致性校验，不在前端并列显示两个值。
- **金额单位是万元**。展示前必须统一换算（参考此前财务模块 `fmtStatementAmount` 踩过的“元/亿”换算 bug）。约定：后端原样返回万元数值 + 字段名标注单位；前端统一用一个 `fmtMoneyWan(v)`（万元 → 亿）格式化。

### 2.3 历史窗口约束

- `money_flow` 合约 window 自 `20240102` 起（Tushare 限制）。
- 允许：最近 N 日（建议 60/120）、5 日/20 日滚动累计、连续流入/流出天数。
- 禁止：跨 2024 之前的长期趋势、资金面同比。

---

## 3. 个股侧设计

### 3.1 后端：复用 `_quote_section`，不新开接口

`_quote_section` 已按 `WORKBENCH_DAILY_KLINE_LIMIT = 360` 拉日线 K。把 `money_flow` 从 `limit=1` 改为同一段历史，使资金流与 K 线**共用日期轴**（后续叠加副图零成本，且少一次请求，符合“加速 UI”方向）。

返回结构（在 `/{symbol}/workbench/quote` 的 payload 内新增）：

```json
{
  "money_flow": { "...latest one day (保留现状)..." },
  "money_flow_history": [
    {
      "trade_date": "20260613",
      "elg_net": 0.0,
      "lg_net": 0.0,
      "main_net": 0.0,
      "md_net": 0.0,
      "sm_net": 0.0,
      "amount": 0.0
    }
  ],
  "money_flow_summary": {
    "main_net_today": 0.0,
    "main_net_5d": 0.0,
    "main_net_20d": 0.0,
    "inflow_streak": 0,
    "unit": "wan_yuan"
  }
}
```

- 历史长度建议默认 120 天（轻量），上限对齐 K 线 360。
- `inflow_streak`：主力净额连续同号天数（正=连续流入，负=连续流出）。
- 所有金额字段单位标 `wan_yuan`。

### 3.2 前端：新增 `MoneyFlowPanel.vue`，挂在行情资金页

放在 K 线下方、独立卡片。内容：

1. **概览条**：今日主力净额、5 日累计、20 日累计、连续流入/流出天数（红涨绿跌沿用 A 股口径）。
2. **图表**：超大单净额 + 大单净额的正负柱状图（堆叠或分组），叠加 5 日累计主力净额折线。
3. **明细表**：最近 N 天 `trade_date / 超大单净额 / 大单净额 / 主力净额 / 中单净额`，金额用 `fmtMoneyWan`。

不改 `StockKLineChart.vue`。资金流图新建 `utils/echarts/moneyFlowOption.js`。

### 3.3 演进（可选，不在第一版）

1. 第一版：K 线下独立 `MoneyFlowPanel`。
2. 第二版：K 线副图开关，把 `main_net` 作为 K 线下方副图（用独立 option，不污染 `shenwanKlineOption`）。
3. 第三版：点击 K 线某日联动资金流明细高亮。

---

## 4. 申万行业侧设计（风险最高，分两阶段）

行业没有天然资金流表，需由成分股（`sw_index_member.con_code`）按日聚合 `money_flow`。L1 行业常 300+ 成分股，× 60 天 = 上万文档/次，**禁止前端每次请求实时全量聚合**。

### 4.1 阶段一（MVP）：选中行业 on-demand + 强缓存

仅对“当前选中行业 + 最近 N 天”聚合，结果缓存（如 6–12h，或按交易日缓存）。

新增 BFF（`shenwan_index.py`）：

```text
GET /api/shenwan-index/money-flow?index_code=801080.SI&src=SW2021&days=60
```

聚合逻辑：

```text
某行业某日:
  main_net   = Σ(成分股 elg_net + lg_net)
  amount_sum = Σ(成分股 amount)
  member_count  = 行业成分股数
  covered_count = 当日有 money_flow 数据的成分股数
  main_net_ratio = main_net / amount_sum
  top_inflow  = 当日主力净额最高的前 N 只
  top_outflow = 当日主力净额最低的前 N 只
```

### 4.2 阶段二：预聚合 collection + 每日 pipeline + 数据合约

落 `quant_data.sw_industry_money_flow`，由 quant-data-engine 每日 pipeline 写入，纳入数据合约（与 `moneyflow` / `sw_*` 同模式）。排行榜类功能必须基于此表，否则会慢。

建议文档结构：

```json
{
  "index_code": "801080.SI",
  "src": "SW2021",
  "level": "L1",
  "trade_date": "20260613",
  "member_count": 312,
  "covered_count": 305,
  "elg_net": 0.0,
  "lg_net": 0.0,
  "main_net": 0.0,
  "main_net_ratio": 0.0,
  "top_inflow": [{ "con_code": "...", "name": "...", "main_net": 0.0 }],
  "top_outflow": [{ "con_code": "...", "name": "...", "main_net": 0.0 }],
  "unit": "wan_yuan"
}
```

唯一键：`(src, index_code, trade_date)`。

### 4.3 覆盖率护栏（阶段一阶段二都要）

- 必带 `covered_count / member_count`。
- `money_flow` 2024 起 + 个别成分股某日缺数据，覆盖率低会让行业净额失真。
- UI 标注覆盖率；低于阈值（如 80%）时弱化或不给方向性结论。

### 4.4 前端：`ShenwanIndustryIndex.vue` 内新增“行业主力资金”卡片

位置：K 线下方、PE/PB 上方。内容：

1. 最近 20/60 日行业主力净流入趋势（正负柱 + 累计折线）。
2. 行业内 Top 流入 / Top 流出成分股，点击复用现有 `openDeepAnalysis` 跳个股。
3. 覆盖率徽标。

行业资金排行榜（多行业横向对比）作为后续独立功能，依赖 4.2 预聚合表。

---

## 5. 复用 vs 重写 总表

| 模块 | 决策 |
|---|---|
| 个股资金流面板 | 新组件 `MoneyFlowPanel.vue` |
| 个股资金流图 option | 新 `utils/echarts/moneyFlowOption.js` |
| 个股 BFF | 复用 `_quote_section`，加 `money_flow_history` / `money_flow_summary` |
| 申万资金流面板 | 新组件（卡片）挂进 `ShenwanIndustryIndex.vue` |
| 申万 BFF | 新端点 `/shenwan-index/money-flow`（阶段一 on-demand） |
| 申万预聚合 | 新 `sw_industry_money_flow` + 每日 pipeline + 合约（阶段二） |
| K 线组件 / `shenwanKlineOption` | 保持现状，不改 |

---

## 6. 实施顺序建议

1. 个股 BFF 加 `money_flow_history` / `money_flow_summary`（含单位标注与口径校验）。
2. 个股前端 `MoneyFlowPanel.vue` + `moneyFlowOption.js`。
3. 申万 BFF on-demand 端点（带覆盖率 + 缓存）。
4. 申万前端“行业主力资金”卡片。
5. 申万预聚合 pipeline + 合约 + 排行榜（独立后续迭代）。

## 7. 验收要点

- 金额单位换算正确（万元 → 亿），无量级错误。
- 主力净额只有一个口径，不与 `net_mf_amount` 并列冲突。
- 历史不越界到 2024 之前。
- 申万聚合带覆盖率，低覆盖率有降级提示。
- 个股资金流与 K 线日期轴对齐。
