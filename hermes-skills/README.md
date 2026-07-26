# Hermes skills (quantFinance) — deprecated

连板天梯等量化能力已合并到 monorepo 内的 **`wukong-skills/wukong-quant`**（MCP：`wukong-quant-read` / `wukong-quant-actions`）。

请将该 skill 目录复制到 Hermes 数据卷，例如：

- `wukong-skills/wukong-quant/` → `hermes-data/skills/wukong-quant/`

旧版 **`quant-ladder`**（直调 `GET /api/hermes-tools/ladder/daily`）已移除，勿再安装。

`HERMES_QUANT_INTERNAL_KEY` 仍须在 **quant-api** 与 MCP 客户端（`X-Hermes-Quant-Key`）上保持一致；详见 `wukong-skills/wukong-quant/SKILL.md` 中的 MCP 配置说明。
