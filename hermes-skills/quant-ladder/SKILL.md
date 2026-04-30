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

**Prefer Python** — the official `nousresearch/hermes-agent` image usually has `python3` but may **not** ship `curl` (same as the compose healthcheck).

```bash
python3 -c "
import os, urllib.request
base = os.environ.get('QUANT_API_URL', '').rstrip('/')
key = os.environ.get('HERMES_QUANT_INTERNAL_KEY', '')
url = f'{base}/api/hermes-tools/ladder/daily'
req = urllib.request.Request(url, headers={'X-Hermes-Quant-Key': key})
with urllib.request.urlopen(req, timeout=30) as r:
    print(r.read().decode())
"
```

Optional query: append `?date=YYYYMMDD` to the URL string (e.g. `20260130`). Omit for “today”.

If `curl` exists on the host, you may use:

```bash
curl -sS -H "X-Hermes-Quant-Key: ${HERMES_QUANT_INTERNAL_KEY}" \
  "${QUANT_API_URL}/api/hermes-tools/ladder/daily"
```

## Answering

1. Parse the JSON. If `"success": false`, quote `error` / `suggestion` — do not invent ladder rows.
2. If `"success": true`, summarize using **`display_date`**, **`tier_counts`**, **`tiers`** (symbols/names/streaks), and **`indicators`** when helpful.
3. Do **not** say you lack market data if this request returns valid JSON.

## Pitfalls

- If `HERMES_QUANT_INTERNAL_KEY` is empty or wrong, the API returns 401/503 — tell the user the internal key is not configured, do not fabricate stock lists.
- This endpoint is **read-only** and does not replace real-time tick data from exchanges.

## Install

Copy this folder into the Hermes data volume, e.g. `hermes-data/skills/quant-ladder/`, then restart or refresh skills per Hermes docs.
