# 架构重构讨论：quantAnalyzer 数据访问层解耦

> **状态**：待决策  
> **记录时间**：2026-02-27

---

## 背景

`quantAnalyzer/main.py` 目前**直连 MongoDB** 读写数据，与 `quantFinance`（API 服务）共用同一个数据库，但绕过了 API 层。

具体 quantAnalyzer 直接写入的集合：

| 集合 | 说明 | quantFinance 已有 API？ |
|------|------|------------------------|
| `analysis_logs` | 涨停/个股 AI 标签推理过程日志 | ❌ 无 |
| `analysis_history` | 用户触发的 AI 分析结果 | ✅ `POST /analyze/history` |
| `portfolio_analysis` | 持仓/自选股 AI 分析 | ✅ 有 |
| `daily_limit_up` | 每日涨停板数据 | ❌ 无 |
| `global_analysis` | 全球市场分析 | ❌ 无 |
| `limit_up_ladder` | 连板天梯数据 | ❌ 无 |

---

## 核心问题：集合命名不清晰

`analysis_logs` 名字过于宽泛，实际只存 quantAnalyzer 对**涨停/个股打 AI 标签**时的推理过程（驱动因子、逻辑链、置信度、新闻上下文等），与 `analysis_history`（用户主动触发的分析）容易混淆。

**建议改名**：`analysis_logs` → `stock_tag_analysis_logs`

---

## 推荐重构路径

直接改集合名需要同步修改 2 个仓库共 7 个文件，且日后每次集合结构调整都还会重复这个问题。建议按以下步骤彻底解耦：

```
Step 1: quantFinance 新增 internal write API
         └── POST /internal/analysis-logs
         └── POST /internal/daily-limit-up
         └── POST /internal/global-analysis
         └── POST /internal/limit-up-ladder

Step 2: quantAnalyzer 改为调用 API（不再直连 DB）

Step 3: 集合改名只需改 quantFinance 一处
```

---

## 潜在风险（已评估）

| 风险 | 说明 | 缓解方案 |
|------|------|---------|
| **性能下降** | HTTP 调用比直连 DB 多一次网络往返；批量写入（5000+ 股 screening）影响大 | 设计批量写入 API（一次传一组数据） |
| **原子性丢失** | 多个集合写入从单次 pymongo 操作变为多个 HTTP 请求，中间失败可能半写 | 每个 API 接口设计为幂等（upsert 语义） |
| **认证设计** | quantAnalyzer 调用 quantFinance API 需认证，不能裸调 | 使用固定 internal API key 或服务级 JWT |
| **循环依赖** | quantAnalyzer 依赖 quantFinance 启动；目前两者独立可运行 | 加重试机制；internal API 故障时降级为本地写入 |
| **数据迁移** | 改名时历史数据需迁移（analysis_logs 约 299 条） | 写一次性迁移脚本，执行后删旧集合 |

---

## 结论

当前直连方式功能正常，暂不重构。等以下任一条件满足时再推进：
- quantAnalyzer 需要部署到与 quantFinance 不同的机器/云服务
- 需要更严格的数据校验和权限控制
- 集合结构需要较大变动，希望统一管理迁移
