# Intraday T+0 Runbook — Phase 0 / Phase 1 实操验证

> 信号模块只产出 `BUY_WATCH` / `SELL_WATCH`，**不自动下单**。  
> Phase 0 = 链路通；Phase 1 = 信号有没有 edge（零资金或 mock）。

---

## Phase 0 — 基础设施验收

### 何时跑

| 时机 | 检查 |
|------|------|
| 每次 quant-api / data-engine 部署后 | `liveness` |
| **每个交易日 09:20 前** | `readiness` + 人工清单 |
| 盘中异常（Dashboard 无现价） | 重跑 `readiness`，查 bridge 日志 |

### Step 0-1：API 存活（liveness）

```bash
# 服务器或 CI
cd quantFinance
python scripts/check_intraday_t0_phase0.py

# 或 HTTP（无需登录）
curl -s http://<API_HOST>:3001/api/intraday-t0/health/liveness | jq .
```

**通过标准**：`overall` 为 `pass`（`warn` 可接受，如 internal key 占位）。

### Step 0-2：当日数据就绪（readiness）

```bash
python scripts/check_intraday_t0_phase0.py --mode readiness

curl -s -H "X-Hermes-Quant-Key: $HERMES_QUANT_INTERNAL_KEY" \
  "http://<API_HOST>:3001/api/intraday-t0/health/readiness" | jq .
```

**必须通过（required）**：

| 检查项 | 含义 |
|--------|------|
| `realtime_quotes` | 当日 `volume_price_realtime_latest` 有行 |
| `orderbook_fields` | 样本含 bid1/ask1 |
| `market_spectrum` | 当日阳谱有值 |

**可 warn（盘中再确认）**：`intraday_t0_signals`、`position_sources`。

### Step 0-3：data-engine 触发链

1. 确认 `INTRADAY_T0_TRIGGER_URL` 指向 quant-api `.../api/intraday-t0/signals/generate-all`
2. 确认 `INTRADAY_T0_TRIGGER_KEY` 与 quant-api `HERMES_QUANT_INTERNAL_KEY` 一致
3. miniQMT bridge 或东财 intraday 在 Windows/容器内运行
4. 查 postprocess 日志：`intraday T+0 trigger status=200`

### Step 0-4：盘中 30 分钟抽查（09:35–14:55 任选连续 30 分钟）

```javascript
// Mongo shell
use finance
db.intraday_t0_signals.find(
  { trade_date: "YYYYMMDD" },
  { snapshot_time: 1, symbol: 1, signal_type: 1 }
).sort({ snapshot_time: -1 }).limit(5)
```

**通过标准**：`snapshot_time` 每分钟递增（与 bridge 间隔一致，通常 1 分钟）。

### Step 0-5：Dashboard 人工确认

1. 登录 → **日内T+0**
2. source=`real`（或 mock）：持仓列表有现价、盘口非 `-`
3. 大盘卡片：阳谱、覆盖率、快照时间有值

### Phase 0 签收

- [ ] liveness PASS
- [ ] readiness PASS（交易日开盘前）
- [ ] 30 分钟内 signals 持续写入
- [ ] Dashboard 持仓现价与行情一致

---

## Phase 1 — 信号质量验证（零资金）

### 前置

- Phase 0 已签收
- 选 **2–3 只** 流动性好的标的；mock 或 real 均可，`enabled=true`

### 每日 SOP

| 时间 | 动作 |
|------|------|
| 09:20 | 跑 readiness（同 Phase 0） |
| 09:30–15:00 | 依赖自动 `generate-all`，无需手点生成 |
| 15:05 | Dashboard → **评估** |
| 15:10 | 查看表现统计（总体 / 按股票 / 按原因） |
| 15:15 | 可选：导出信号 CSV、评估 CSV 归档 |

### 手动补生成（仅调试）

```bash
# 登录后带 JWT
curl -X POST "http://<API>/api/intraday-t0/signals/generate" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"trade_date":"YYYYMMDD","source":"mock"}'
```

### 评估指标与达标（滚动 10 个交易日）

在 Dashboard **表现统计 → 总体**，看 `15m` / `30m`：

| 信号 | 期望 avg_return | win_rate 达标 |
|------|-----------------|---------------|
| `SELL_WATCH` | < 0（卖后跌） | ≥ 55% |
| `BUY_WATCH` | > 0（买后涨） | ≥ 55% |

**辅助分析**：

- **按原因**：剔除持续亏损的 `reason_code`
- **按股票**：流动性差的标的移出 mock 列表
- 单票同向 WATCH ≤ 6 次/日（cooldown 是否生效）

### 未达标调参（改 quant-api 后需重新部署）

1. 阈值 58 → 65（`classify_signal`）
2. `INTRADAY_T0_COOLDOWN_MINUTES` 15 → 20
3. 禁用 09:30–10:00 出 WATCH（`time_bucket` 逻辑）

### Phase 1 签收（进入 Phase 2 人工小仓前）

- [ ] ≥ 10 个交易日数据
- [ ] SELL / BUY 在 15m、30m **同时** win_rate ≥ 55%
- [ ] 团队书面确认可进入 Phase 2

---

## 常见问题

| 现象 | 排查 |
|------|------|
| readiness 实时行情 FAIL | bridge 未跑 / 非交易日 / Mongo 库名不对 |
| 信号全 HOLD | 正常；看 score 分布，或调低阈值 |
| 评估为 0 条 | 当日无信号或未点「评估」 |
| 触发 401 | trigger key 与 internal key 不一致 |
