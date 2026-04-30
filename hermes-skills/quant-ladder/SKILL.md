---
name: quant-ladder
description: >-
  Fetch A-share limit-up ladder (连板天梯) from the quantFinance API.
  Use when the user asks about 连板天梯, 涨停梯队, 连板股, or today's limit-up tiers.
version: 1.0.0
---

# Quant Ladder — Limit-Up (连板天梯)

## When to use

- Questions about **连板天梯**, **涨停梯队**, **几连板**, **今日连板**, or sector strength from ladder data.

## How to retrieve data

The gateway container has `QUANT_API_URL` and `HERMES_QUANT_INTERNAL_KEY` (same value as on `quant-api`).

Run:

```bash
curl -sS -H "X-Hermes-Quant-Key: ${HERMES_QUANT_INTERNAL_KEY}" \
  "${QUANT_API_URL}/api/hermes-tools/ladder/daily"
```

Optional query: `?date=YYYYMMDD` (e.g. `20260130`). Omit for “today”.

## Answering

1. Parse the JSON. If `"success": false`, quote `error` / `suggestion` — do not invent ladder rows.
2. If `"success": true`, summarize using **`display_date`**, **`tier_counts`**, **`tiers`** (symbols/names/streaks), and **`indicators`** when helpful.
3. Do **not** say you lack market data if this request returns valid JSON.

## Pitfalls

- If `HERMES_QUANT_INTERNAL_KEY` is empty or wrong, the API returns 401/503 — tell the user the internal key is not configured, do not fabricate stock lists.
- This endpoint is **read-only** and does not replace real-time tick data from exchanges.

## Install

Copy this folder into the Hermes data volume, e.g. `hermes-data/skills/quant-ladder/`, then restart or refresh skills per Hermes docs.
