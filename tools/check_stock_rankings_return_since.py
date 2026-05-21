#!/usr/bin/env python3
"""
诊断「评分日以来涨跌」：API 是否返回字段，以及 Mongo 侧 symbol / trade_date 是否可对齐。

用法（在项目 quantFinance 目录下，已配置 .env 中的 MONGO_URI）:

  python tools/check_stock_rankings_return_since.py
  python tools/check_stock_rankings_return_since.py --http http://127.0.0.1:8000
  python tools/check_stock_rankings_return_since.py --http http://127.0.0.1:8000 --path /api/stock-rankings --limit 5

不依赖本机已启动 API 时，仅跑 Mongo 段即可核对 volume_price.trade_date 类型与样例涨跌是否可算。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Any, Dict, Optional

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()


def _mongo_client() -> MongoClient:
    uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    return MongoClient(uri)


def _vp_trade_date_type_counts(vp) -> Dict[str, int]:
    out = {}
    for t in ("string", "date", "int", "long", "double"):
        try:
            out[t] = int(vp.count_documents({"trade_date": {"$type": t}}))
        except Exception:
            out[t] = -1
    return out


def _close(doc: Optional[dict]) -> Optional[float]:
    if not doc:
        return None
    try:
        c = float(doc.get("close"))
        return c if c > 0 else None
    except (TypeError, ValueError):
        return None


def _find_base_bar(vp, symbol: str, score_date: str) -> tuple[Optional[dict], str]:
    """与 stock_ranking.compute_return_since_score 一致的基准 K 查找（含 date/int 回退）。"""
    sd = score_date.strip()[:8]
    reason = "string_lte"
    doc = vp.find_one(
        {"symbol": symbol, "trade_date": {"$lte": sd}},
        sort=[("trade_date", -1)],
    )
    if doc:
        return doc, reason
    reason = "datetime_lte"
    try:
        y, mo, day = int(sd[:4]), int(sd[4:6]), int(sd[6:8])
        dt_end = datetime(y, mo, day)
        doc = vp.find_one(
            {"symbol": symbol, "trade_date": {"$lte": dt_end}},
            sort=[("trade_date", -1)],
        )
    except (ValueError, IndexError):
        doc = None
    if doc:
        return doc, reason
    reason = "int_lte"
    try:
        as_int = int(sd)
        doc = vp.find_one(
            {"symbol": symbol, "trade_date": {"$lte": as_int}},
            sort=[("trade_date", -1)],
        )
    except ValueError:
        doc = None
    return doc, reason


def mongo_diagnose(limit_samples: int) -> int:
    finance_db = os.getenv("MONGO_DB", "finance")
    data_db_name = os.getenv("DATA_DB_NAME", "quant_data")
    client = _mongo_client()
    scores = client[finance_db]["stock_scores"]
    vp = client[data_db_name]["volume_price"]

    print(f"DB: stock_scores={finance_db}, volume_price={data_db_name}")
    print("volume_price.trade_date 类型计数:", _vp_trade_date_type_counts(vp))

    sample = list(
        scores.aggregate(
            [
                {"$sort": {"score_date": -1}},
                {"$group": {"_id": "$symbol", "first": {"$first": "$$ROOT"}}},
                {"$replaceRoot": {"newRoot": "$first"}},
                {"$limit": max(1, limit_samples)},
            ]
        )
    )
    if not sample:
        print("stock_scores 无样例，退出。")
        return 1

    print(f"\n取 {len(sample)} 条最新评分样例:\n")
    for row in sample:
        sym = row.get("symbol") or ""
        sd = str(row.get("score_date") or "")
        latest = vp.find_one({"symbol": sym}, sort=[("trade_date", -1)])
        base, how = _find_base_bar(vp, sym, sd)
        lc, bc = _close(latest), _close(base)
        pct = None if lc is None or bc is None else round((lc / bc - 1.0) * 100.0, 2)
        print(f"  symbol={sym!r} score_date={sd!r}")
        print(f"    latest_vp: trade_date={latest.get('trade_date') if latest else None} close={lc}")
        print(f"    base_vp:   via={how} trade_date={base.get('trade_date') if base else None} close={bc}")
        print(f"    -> return_since_score_pct = {pct}")
        if latest is None:
            print("    !! volume_price 无该 symbol，请核对是否与 stock_scores 代码格式一致（如 600000.SH）")
        elif pct is None and base is None:
            print("    !! 基准 K 未命中：多为 trade_date 类型与查询不一致，或 score_date 早于首条日线")
        print()

    return 0


def http_check(base_url: str, path: str, limit: int) -> int:
    try:
        import urllib.request
    except ImportError:
        print("urllib 不可用", file=sys.stderr)
        return 1
    url = base_url.rstrip("/") + path
    if "limit=" not in path:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}limit={limit}"
    print("GET", url)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    data = json.loads(raw)
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        print("响应非 { data: [] }，前 500 字符:\n", raw[:500])
        return 1
    print(f"共 {len(rows)} 条。前 3 条涨跌字段:")
    for i, row in enumerate(rows[:3]):
        print(
            f"  [{i}] symbol={row.get('symbol')} score_date={row.get('score_date')} "
            f"return_since_score_pct={row.get('return_since_score_pct')} "
            f"base={row.get('price_base_trade_date')} latest={row.get('price_latest_trade_date')}"
        )
    missing = sum(1 for r in rows if r.get("return_since_score_pct") is None)
    print(f"\n其中 return_since_score_pct 为 null 的条数: {missing}/{len(rows)}")
    if missing == len(rows) and rows:
        print("若全为 null：多为 Mongo 日线未对齐（symbol / trade_date 类型），请运行不带 --http 的 Mongo 诊断。")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--http", metavar="BASE", help="例如 http://127.0.0.1:8000，请求金榜 API")
    p.add_argument("--path", default="/api/stock-rankings", help="API 路径")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--mongo-samples", type=int, default=5, help="Mongo 样例行数")
    args = p.parse_args()

    rc = mongo_diagnose(args.mongo_samples)
    if args.http:
        print("\n--- HTTP ---\n")
        rc = http_check(args.http, args.path, args.limit) or rc
    return rc


if __name__ == "__main__":
    sys.exit(main())
