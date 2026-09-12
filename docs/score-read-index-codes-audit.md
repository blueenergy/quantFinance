# stock_scores 读路径：`index_codes` 过滤缺失审计

> **最后更新**：2026-09-13
> **基线**：`origin/main` @ `a532139`（行号以此为准）
> **范围**：quantFinance 仓库。`quant-api` / `quant-mcp-read` / `quant-mcp-actions` 三个容器由本仓库同一镜像构建，因此 MCP 读服务也在本文范围内。
> **性质**：仅审计与清单，不含代码改动。

---

## 1. 背景：为什么必须过滤 `index_codes`

`finance.stock_scores` 的主键是 **`(symbol, score_date)` 且唯一**（`stock-scoring-system/data_manager.py` 唯一索引）。一个 `(symbol, date)` 只有一行文档，`index_codes` 是这行在**当日 point-in-time** 所属的宽基标签：

```
SCORE_INDEX_LABEL_CODES = ("hs300", "a500", "csi500", "csi1000", "csi2000", "star50")
```

关键事实：**同一天的文档分两类**

| 类别 | `index_codes` | 含义 |
|---|---|---|
| 当日成分股 | 非空，如 `["hs300","star50"]` | 该票当日确实属于这些宽基 |
| 非成分股 | `[]` 或 `null` | 票在**回填分片的窗口并集**里，但当日不是任何宽基成分 |

回填的票池是 `load_historical_universe_symbols()` 取的**「分片窗口内所有快照的并集」**（325 天窗口实测 1984–2139 只），而 `index_codes` 严格按**当日** PIT 打标。两者口径不同，所以每个 `score_date` 上都会存在一批 `index_codes: []` 的行。

> **因此 `{"score_date": D}` 会读到整批当日非成分股。** 要拿「某指数在某日的成分」，查询必须带 `index_codes`。

实测分布（2026-09-13）：

| `index_codes` | 行数 |
|---|---|
| `[]` | 2,082,285 |
| `["csi1000"]` | 1,545,317 |
| `["csi500"]` | 334,989 |
| `["hs300"]` | 186,253 |
| `null`（字段缺失） | 64,714 |

⚠️ **`null` 与 `[]` 查询语义不同**：`{"index_codes": {"$ne": "csi1000"}}` **不会**匹配 `null` 文档，而 `{"index_codes": {"$nin": [...]}}` 的行为也不一致。所有过滤写法需显式覆盖 `null`。

### 已知边界（不是缺陷，不要"修"）

- **`score_date < 20200123`**：`quant_data.index_constituents` 最早快照就是 `20200123`，且 `_resolve_marker` 用 `weight_trade_date: {"$lte": as_of}` 严格 PIT、无向前回退 → 这 15 个交易日（`20200102`–`20200122`）**打不上任何标签**，恒为 `[]`。
- **指数存在性**：`star50` 起 `20200731`、`a500` 起 `20240930`。早于这些日期无对应标签是正确行为。

---

## 2. 缺陷清单

### A 类：查询完全不带 `index_codes`

| # | 文件:行 | 函数 / 路由 | 现状 | 影响 |
|---|---|---|---|---|
| A1 | `routers/stock_ranking.py:208` | `_latest_score_date` ← `GET /stock-rankings` | `find_one({})` 全库取最大 `score_date` | 见下 #3 |
| A2 | `routers/stock_ranking.py:1632,1638` | `GET /stock-rankings` | 仅 `score_date`（可选 `composite_score ≥ min_score`） | 全市场榜混入非成分股 |
| A3 | `routers/stock_ranking.py:2008,2010,2012` | `GET /statistics` | **不传 date 时 `match_query = {}`，聚合全集合全历史** | 见下 #2 |
| A4 | `routers/stock_ranking.py:1754,1756` | `GET /stock-score-detail` | 仅 `symbol`（可选 `score_date`） | 单股详情可能取到别批次 |
| A5 | `routers/stock_ranking.py:1917` | `GET /score-history/{symbol}` | 仅 `symbol`（可选 `score_date ≤`） | 历史曲线跨批次拼接 |
| A6 | `routers/stock_ranking.py:2058` | `GET /stock-dates` | 仅 `symbol` | 可选日期列表含别批次日期 |
| A7 | `routers/stock_ranking.py:2294` | `POST /stock-rankings/selected`（传 date 分支） | `symbol $in` + `score_date` | 指定股票榜混入非成分股 |
| A8 | `routers/stock_ranking.py:409,426` | `_aggregate_latest_score_doc_per_symbol` / `_find_latest_per_symbol_fallback` | `symbol $in` 取每票最新一条 | 同上（不传 date 分支） |
| A9 | `routers/stock_ranking.py:458` | `_find_one_stock_score` | `symbol` 变体 + `score_date` 逐对 `find_one` | `per_date_scores` 混批次 |
| A10 | `routers/stock_workbench.py:235` | `_latest_score` ← `GET /{symbol}/workbench`、`/workbench/scores`、`POST /workbench/signals/refresh-internal` | `symbol $in` 取 `score_date` 最新 | **researcher panel 主读点**，见下 #4 |
| A11 | `routers/stock_workbench.py:246` | `_score_history` ← `GET /{symbol}/workbench/scores` | `symbol $in` | panel 60 条历史跨批次 |
| A12 | `services/valuation.py:104` | `_latest_score` ← `GET /{symbol}/workbench/valuation` | `symbol $in`，投影 `value_score` | 估值 payload 的 `value_score_link` 可能来自别批次 |
| A13 | `services/fund_inventory_context.py:192` → `:28 _latest_scores` | `GET /fund-inventory/snapshots` | `symbol $in` 取每票最新一条 | 公募库存候选分类的评分上下文混批次 |
| A14 | `routers/hermes_quant_tools.py:2948` | MCP `GET /stock/score-detail` | `symbol` 取 `score_date` 最新 | MCP 消费方拿到别批次分 |
| A15 | `routers/hermes_quant_tools.py:3571` | MCP `GET /user/watchlist` | `symbol $in` 自选列表取最新 | 自选股评分混批次 |

### B 类：主查询有过滤，**兜底分支没有**

这类不能改成"完全不过滤"，只需给兜底分支补上同样的条件（或改为显式报错而不是静默降级）。

| # | 文件:行 | 函数 | 现状 |
|---|---|---|---|
| B1 | `routers/portfolio_research.py:362` | `_latest_stock_score_date` ← `GET /jobs`、`/jobs/{job_id}` | job 参数带 `universe_index` → 有过滤；不带 → `{"score_date": {"$lte": end_date}}` 全库 |
| B2 | `routers/portfolio_plans/__init__.py:1195,1199,1202` | `_latest_available_score_watermark` | `:1195` 主查询带 `index_codes`；`:1198-1199` 查不到则 `query = {}` 全库，并标 `scope: "global"` |
| B3 | `routers/portfolio_plans/__init__.py:1511,1527` | `_enrich_plan_items_with_score_snapshot` | `:1511-1513` 主查询带 `index_codes`；`:1527-1528` 只对**未命中**的 symbol 兜底，不带过滤 |
| B4 | `routers/portfolio_plans/__init__.py:1396` | `_score_symbol_meta` ← bench payload | `score_date` + `symbol $in`，无 `index_codes` |

### C 类：跨库 `$lookup` 恒空（**独立缺陷，比过滤问题更严重**）

| # | 文件:行 | 现状 |
|---|---|---|
| C1 | `routers/records.py:257` | `$lookup: {"from": "stock_scores", ...}` |

**已核实的根因**：该聚合的执行目标是 `collection = get_data_db()["volume_price"]`（`records.py:12`），即 **`quant_data` 库**。而 `stock_scores` 在 **`finance` 库**（`db.py:206` `get_data_db()` 只返回 `quant_data` 的集合）。MongoDB 的 `$lookup` **只能同库 join**。

已实测确认：`quant_data.stock_scores` **不存在**（该库 94 个集合中无此名），`finance.stock_scores` 存在（49 个集合）。

**后果**：`GET /records/` 的 `score_docs` **恒为空数组**，`composite_score` / `cycle_score` / `growth_score` / `fundamental_score` / `value_score` / `technical_score` / `money_flow_score` 在响应体里**永远是 `null`**。`RecordResponseModel` 里的全部评分字段对外表现为空 —— 这不是"过滤缺失"，是整段功能没生效。

### 已经正确的读点（不要动，可作为改法参照）

| 文件:行 | 路由 | 说明 |
|---|---|---|
| `routers/stock_ranking.py:222,252` | `GET /stock-rankings/by-index/{index_code}` | 全仓**唯一**按 `index_codes` 过滤的榜；命中 `(score_date -1, index_codes 1)` 复合索引 |
| `routers/portfolio_plans/__init__.py:1443` | bench / live overlay | `score_date` + `index_codes`，进程内缓存 600s |
| `routers/hermes_quant_tools.py:660,677` | MCP `GET /stock/ranking` | 先 `distinct("symbol", {"index_codes": ic})` 再按 `symbol $in` 取分 |

---

## 3. 最严重的后果（按危害排序）

**#1 `GET /records/` 评分字段恒空（C1）**
输入：任意 `GET /records/`。输出：所有评分字段 `null`。这是跨库 join 失效，不是数据问题 —— 修好 `$lookup`（或改为应用层二次查询）后才谈得上过滤。

**#2 `GET /statistics` 不传 date 时统计全历史（A3）**
`match_query = {}` 时，`count_documents` 与两个聚合把每个 symbol 的**每一天**评分都算进去。表现：`total_stocks` 是「历史所有 `(symbol, score_date)` 文档数」而非当日股票数；分数段分布被多日重复计数污染；`avg_score` 变成跨历史均值。文档写着"默认为昨天"，但代码并不默认昨天。

**#3 全市场榜被部分评分 run 静默截断（A1 + A2）**
`_latest_score_date` 用 `find_one({})` 取全局最大 `score_date` 作截面日。语义是"只在该日期有评分的股票才上榜"。一旦某次只写了某个指数/某批股票的评分（更晚的 `score_date`），全市场榜会**静默坍缩成那批股票的子集**，其余几千只从榜上消失，无任何告警。该榜还带进程内 TTL 缓存，错误结果会持续到缓存过期。

**#4 researcher panel 与指数榜可能显示互相矛盾的分数（A10 + A11）**
panel 的 `_latest_score` / `_score_history` 均不按 `index_codes` 过滤。研究员在某个指数语境下打开工作台时，看到的是该 symbol **全局最新**（可能是另一指数批次或全市场批次）的分数与历史，与 `/stock-rankings/by-index` 页面（严格 `score_date` + `index_codes` 截面）对不上。前端组件 `quantFinance-dashboard/src/components/stock/WorkbenchScoresPanel.vue` 副标题写着"来自最新 `stock_scores` 文档"，措辞本身也印证了这个语义。

**#5 portfolio_plans 的 watermark / score_date 全局兜底会张冠李戴（B2 + B3）**
按 `universe_index` 查不到时回退全集合查询，返回**其它指数**的最新评分日并标 `scope: "global"`。若某指数的 `index_codes` 尚未回填或当天缺数据，计划生成 / bench / overlay 会把别的指数的评分快照日当作本指数的"最新可用数据日"展示（例如 csi1000 计划显示 hs300 的水位线），用户据此判断数据新鲜度会误判。

**#6 `portfolio_research` `/jobs` 列表的 N+1 读（B1）**
`_job_summary` 对每个 job 调一次 `stock_scores.find_one`；且老式 job 若无 `universe_index` 参数就完全不按指数过滤、全库取最新。列表页放大后对 MongoDB 造成逐条往返，且 watermark 语义因有无 `universe_index` 而不一致（指数级 vs 全库级）。

---

## 4. 改法

### 4.1 统一封装（推荐先做这一条）

在各处散写 `index_codes` 条件正是缺陷的成因。建议在 `db.py` 或新建 `utils/score_query.py` 收敛为单一入口：

```python
def score_scope_filter(index_code: str | None) -> dict:
    """按宽基限定 stock_scores 读范围。

    index_code 为 None 时显式返回全量（调用方必须是有意为之），
    避免"忘了传"与"故意要全量"在代码上长得一样。
    """
```

要点：
- **显式区分「要全量」与「忘了传」** —— 建议 `index_code` 为必填位置参数，全量需显式传 `None` 并写明理由。
- 统一处理 `null`：`{"index_codes": index_code}` 不匹配 `null` 文档。若要含 `null`，须写 `{"index_codes": {"$in": [index_code]}}` 并确认语义；若不含，需在注释里写明。

### 4.2 逐条改法

| # | 改法 |
|---|---|
| A1 | 不要再用全局最大 `score_date`。改为「榜单所服务的指数」的 `_latest_index_score_date`（`:222` 已有实现），或要求调用方显式传 `score_date`。**这是 A2 的前置修复。** |
| A2 | `base_query` 增加 `index_codes`；`min_score` 逻辑不变。 |
| A3 | 不传 date 时**必须**回退到该 scope 的最新评分日，不能落成 `{}`；同时给聚合加 `index_codes`。 |
| A4–A9 | 全部补 `index_codes`；A8/A9 的"取最新一条"要改成"在指定 scope 内取最新一条"。 |
| A10–A14 | 同上。A10 额外需要：`POST /workbench/signals/refresh-internal` 用它取出的 `score_date` 会写进任务参数，**必须**是同一 scope 的日期，否则内部 S/W 信号的时间点锚会漂。 |
| A15 | 自选股列表本身跨指数，建议**显式声明**为全量读并加注释，而不是默认不过滤。 |
| B1–B4 | 兜底分支要么补同样的 `index_codes`，要么**显式失败**（返回 404/空并记日志），不要静默降级成全库查询。B2 的 `scope: "global"` 建议改为显式告警字段而不是正常返回值。 |
| C1 | 改为应用层二次查询（先取 `volume_price` 页，再用 `finance` 库按 `(symbol, score_date)` 批量 `$in` 取分），或迁移到同库物化视图。**同时补 `index_codes`。** |

### 4.3 若无法一次性全改

按危害排序，先修 #1–#3：`C1` → `A3` → `A1+A2`。这三条是"对外结果明显错误"级别；其余是"批次混读"级别，可分批。

---

## 5. 验收标准

写完后**逐条**用下列方式验证，不要只看代码。

**通用（每条都适用）**
1. 取一个**同一 `score_date` 上既有成分股行、又有 `index_codes: []` 行**的日期。构造方式：任选一个交易日 `D`，`db.stock_scores.countDocuments({score_date: D, index_codes: []})` 应 > 0（实测每个回填日期都有数百到上千行）。
2. 修改前记录该接口在 `D` 的返回集合 `S_before`；修改后记录 `S_after`。
3. 断言 **`S_after ⊆ S_before`**，且 `S_after` 中不存在 `index_codes` 不含目标指数的 symbol。

**C1（`GET /records/`）**
- 修复前：任取一条 `volume_price` 记录，其 `symbol` 在 `finance.stock_scores` 有对应 `score_date == trade_date` 的行 → 响应里 `composite_score` 仍为 `null`。
- 修复后：同一请求 `composite_score` 非 `null`，且数值等于 `db.stock_scores.findOne({symbol, score_date})` 的 `composite_score`。

**A3（`GET /statistics`）**
- 修复前：不传 date 的 `total_stocks` ≈ 4,055,320（全集合行数）。
- 修复后：不传 date 的 `total_stocks` 应等于**某一个 `score_date`** 的行数（数千量级），且响应里能看出实际用的日期。

**A1 + A2（`GET /stock-rankings`）**
- 构造：写入一行 `score_date` 比当前最大日更晚、但只覆盖少数 symbol 的记录（或直接用现有数据找一个只有单指数批次的最新日）。
- 修复前：榜单条目数骤降到那一批的规模。
- 修复后：榜单条目数稳定在该 scope 的正常规模，且 `effective_date` 是该 scope 的最新日而非全局最大日。

**A10 + A11（researcher panel）**
- 同一 symbol 在 `GET /stock/{symbol}/workbench/scores` 与 `GET /stock-rankings/by-index/csi1000` 中的分数**必须一致**（当该 symbol 当日属于 csi1000 时）。当前会不一致，这是最容易复现的一条。

**B2 + B3（portfolio_plans）**
- 构造：使目标指数的 `index_codes` 行在 `base_date` 之前不存在、而其它指数存在。
- 修复前：`latest_available_score_scope == "global"`，返回的是别的指数的日期。
- 修复后：不再返回别的指数的日期（显式失败或返回空），且日志有可观测的告警。

---

## 6. 相关

- `quantFinance/docs/MONGODB_COLLECTIONS.md` —— 多库架构与集合归属（本文 C1 的依据）
- `stock-scoring-system/docs/data-contract/stock_scores.md` —— `stock_scores` 集合契约与写入侧缺陷
- `stock-scoring-system/docs/data-contract/index_prices.md` —— data-contract 文档体例
