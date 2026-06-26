# 组合计划生命周期 — 逻辑缺陷修复计划

> 来源：2026-06 对 `portfolio_plans.py`（router）、`portfolio_trading.py`（worker）、
> `PortfolioOverview.vue` / `PortfolioPlans.vue`（前端）的逻辑审查。
> 行号为审查时的快照，实施前请以当时代码为准。

## 背景

审查起因：计划标的列表 + AI 风控原本被 gating 在 `status === 'approved'` 之后才显示，
导致用户在 `needs_review` 阶段无法看到买卖标的与风险，审批沦为「盲签」。
该问题已在前端修复（新增 needs_review 复核卡片，提交 `quantFinance-dashboard@60db7ae`）。

排查后发现一批同类「逻辑不合理」问题，按严重程度分阶段修复。

图例：`[ ]` 待办 · `[~]` 进行中 · `[x]` 完成

---

## 阶段 1 — 资金安全 / 账本一致性（P0）

目标：任何让计划「退出活跃状态」的路径，都必须妥善处理已发布的实盘订单，
不得让券商挂单与系统状态脱节，也不得让 paper / live 双轨执行同一计划。

- [x] **1.1 抽取共享清理逻辑**
  - 把 `_cancel_plan`（router L3291+）中「取消未成交 live 信号 / 拦截已成交 fills」
    的逻辑抽成共享辅助函数，例如 `_settle_open_live_signals_or_block(plan)`。
- [x] **1.2 reject 复用清理逻辑**
  - `_review_plan`（router L3284）`approved → rejected` 前：
    有未成交信号则取消；已有成交（live fills）则拒绝转换并返回 409。
  - 注意：此时**不要**提示「改用 cancel」——`_cancel_plan` 在有 fills 时同样
    返回 409（已核验，router L3310-3311）。应提示走成交后处理 / 补单 / 人工对账流程。
  - 现状：reject 仅 `$set status`，完全不碰 `trade_signals`（已核验）。
- [x] **1.6 收紧 review 状态转换**
  - `_review_plan`（router L3270）当前允许 `needs_review / approved / rejected`
    三者任意互转，且不检查执行态（已核验）。需明确并实现：
    - `approved → rejected`：按 1.2 处理 live 信号/成交。
    - 已有 paper execution 或 live fills 的计划：禁止 re-review 翻转。
    - `rejected → approved` 是否允许？若允许，需限定「无执行态、无已发布信号」前提。
  - 已实现：review 状态真正变化时先检查执行痕迹；已有 paper execution 或 live fills
    的计划禁止翻转；`rejected → approved` 仅允许在没有 paper execution 且没有任何
    live signal 的情况下执行。
- [x] **1.3 supersede 加保护**
  - `supersede_stale_active_strategy_plans`（worker L104-146）仅改 status，
    不处理 live 信号（已核验）。被 supersede 的旧计划若已发布实盘信号：
    取消其未成交信号，或跳过该计划并告警。
  - 需确认 worker 侧能否访问 `trade_signals` 集合。
  - 已实现：`generate_trade_plan` 将 `app_db` 传入 supersede 逻辑；未成交 live signal
    会被标记 `cancelled` 后再 supersede；已有 live fills 的旧 plan 不会被 supersede，
    并写入 blocked operation log。
- [x] **1.4 cron 纸面执行加 live 检查**
  - `apply_paper_trade_plan` / `apply_approved_paper_plans`（worker）缺少
    `_plan_has_live_signals` 门禁，可能与实盘双轨执行。对齐 API 侧 `_apply_paper_execution`。
  - 阻断范围：只要该 plan 存在任意 live signal 即阻断（包括 `cancelled` /
    terminal），避免同一计划在 live / paper 双账本之间混用。
  - 已实现：`apply_paper_trade_plan` 在执行前阻断任意 live signals（`force=True`
    也不能绕过）；`apply_approved_paper_plans` 遇到 live signals 时跳过并记录
    `live_signals`。
- [x] **1.5 测试**
  - reject-with-open-signals、supersede-with-published-live、
    cron-paper-skips-live-plan 各至少一例单测。

风险：跨 `quantFinance`（router）与 `stock-scoring-system`（worker）两个仓库，
建议每条独立 commit + 单测。

---

## 阶段 2 — 状态机终态与死角（P1）

- [ ] **2.1 定义实盘终态**
  - `executed_live` 被列入 `TERMINAL_PLAN_STATUSES`（worker L92-100）但全仓库从不写入。
    实盘成交收口后应写 `executed_live`（在 reconcile / trader 回填处判断），
    否则计划永远停在 `approved`，无法区分「待发布」与「实盘已完成」。
  - 需先明确「收口」判定（验收标准）：
    - 何为「全部成交」：所有 live 信号都进入终态（filled / cancelled / rejected / expired），
      且没有 active 信号在途？还是要求全部 `filled`？
    - 部分成交 + 部分取消的混合终态如何归类（`executed_live` 还是新增
      `partially_executed_live` / 保持 `approved + live_closed` 标记）？
  - 实现前先定义清楚上述判定，避免把「信号都终态」误判为「实盘完成」。
- [ ] **2.2 `partially_executed` 给出路**
  - 它被 `_plan_action_required`（L4203）标为「需处理」，但 cancel / review /
    publish-live / replan-remainder 都不接受它 → 用户被困。
  - 方案：无实盘成交时允许 cancel；或加补单入口；或从 action_required 移除。
- [ ] **2.3 `filled` 纳入状态常量**
  - `record_external_manual_record`（L2264）写 `filled`，但它不在任何状态机常量里，
    `_plan_today_state_label` 无对应分支，会落入「未知状态」。补常量 + 标签分支。
- [ ] **2.4 `expired` 决断**
  - `_plan_today_state_label`（L4184-4185）有文案但代码从不写入。
    要么实现过期逻辑，要么删除 UI 文案。

---

## 阶段 3 — 校验口径对齐（P2）

- [ ] **3.1 开盘价策略统一**
  - 缺开盘价时：API（`_apply_paper_execution` L5209+）整单 409；
    cron（`apply_paper_trade_plan` L2674+）落 partial 仍写账本。两套口径需统一。
- [ ] **3.2 live/paper-rebalance 校验 anchor 状态**
  - `live_rebalance_portfolio`（L1833）/ `paper_rebalance_portfolio`（L2424）仅
    `_require_plan`，无 status 门禁，可从 cancelled/superseded 计划拉起新子计划。
    若属审计设计则文档化，否则加状态校验。

---

## 阶段 4 — 前端剩余项（P1/P2）

- [ ] **4.1 审批时展示执行就绪状态**
  - 复核卡片里展示 `execution_status`（开盘价是否就绪、execute_date、缺失标的数），
    让用户批准前知道「批了能不能执行」。数据 `refreshDetail` 已拉取，仅缺展示。
- [ ] **4.2 计划页实盘账户默认绑定账户**
  - `PortfolioPlans.vue` 默认选列表第一个账户，应对齐总览的 `syncSelectedLiveAccount`，
    默认用组合绑定账户，避免对错误账户预检/发布。
- [ ] **4.3 needs_review 时 gating「立即调仓 / 手动调仓」**
  - 存在待审计划时仍可 `forceRebalanceLineage` / 手动调仓，可能生成 competing plan；
    文案「不会执行当前已有 plan」对「待审 plan」语义不清。加 gating + 修文案。
- [ ] **4.4 publish 后刷新详情**
  - `publishLiveSignals` 成功后未 `selectPlan` / `refreshDetail`，UI 仍停在
    「实盘预检 / 确认发布」，看不到补缺口态。补刷新。

---

## 建议执行顺序

1. ~~前端提交~~（已完成 `quantFinance-dashboard@60db7ae`）
2. 阶段 1（最独立的 1.1 + 1.2 先做，必须配测试）
3. 阶段 2 → 阶段 4 → 阶段 3

## 已完成 / 设计合理（对照参考）

- 总览 needs_review 复核卡片（标的 + AI 风控 + 批准/拒绝/风控复检），
  提交 `quantFinance-dashboard@60db7ae`。
- 审批入口已统一到复核卡片（移除时间轴「一键审批」），approve/reject 共用
  `reviewPlanId` 兜底并加 disabled 保护。
- `_cancel_plan` 已正确取消未成交 live 信号、拦截已成交（阶段 1 以此为模板）。
