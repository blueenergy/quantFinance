# Agent 接入：通用选股 MCP

本能力通过 **同一套 ScreeningService** 同时暴露 REST 与 MCP，不绑定 Hermes。

## 工具契约（operation_id）

| operation_id | 方法 | 路径 |
|---|---|---|
| `stock_screen_presets` | GET | `/api/agent-tools/screening/presets` |
| `stock_screen_schema` | GET | `/api/agent-tools/screening/schema` |
| `run_stock_screen` | POST | `/api/agent-tools/screening/run` |

兼容财务筛选：`stock_screen`（`/api/hermes-tools/stock/screen`）已转调同一引擎。

## 鉴权

| Header | 说明 |
|---|---|
| `X-Quant-Client-Key` | 推荐：中性别名 |
| `X-Hermes-Quant-Key` | 兼容旧部署 |
| `X-User-Token` | 选股只读工具**不强制** |

环境变量：`HERMES_QUANT_INTERNAL_KEY`（服务端）

## Hermes

1. 配置 MCP server `wukong-quant-read` → `http://quant-mcp:3002/api/mcp`（或本地 `localhost:3002`）
2. 将 `wukong-skills/stock-screening/SKILL.md` 复制到 `hermes-data/skills/stock-screening/`
3. 在 Hermes UI 用自然语言描述条件；Agent 应先 `stock_screen_schema` 再 `run_stock_screen`

## Claude Code / Cursor

1. 在 MCP 配置中添加 `wukong-quant-read`（同上 URL 与 key）
2. 引用 skill：`wukong-skills/stock-screening/SKILL.md`（Cursor 可 symlink 到 `~/.cursor/skills/`）
3. 工具名前缀因宿主而异，例如 Cursor：`mcp_wukong_quant_read_run_stock_screen`

## REST（脚本 / 未来 Dashboard）

```bash
curl -s -H "X-Quant-Client-Key: $KEY" \
  https://example/api/screeners/schema

curl -s -H "X-Quant-Client-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"universe":{"type":"preset","value":"tech_hardware_v1"},"start":"20260401","end":"latest","sort":[{"field":"range_amplitude","order":"desc"}]}' \
  https://example/api/screeners/run
```

## 扩展新条件

- **已有指标的新组合**：只改 ScreenSpec，无需改代码
- **新指标**：在后端 `MetricRegistry` 增加 Provider 并发布 schema 版本
- **新概念成分**：恢复可靠概念库后再加 universe preset
