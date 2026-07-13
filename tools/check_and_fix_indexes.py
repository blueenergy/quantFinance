"""
检查三个数据库（finance / quant_analyzer / quant_data）的索引健康度，
列出缺失或低效索引，用户确认后一键补建。

用法：
  python check_and_fix_indexes.py          # 扫描 + 交互修复
  python check_and_fix_indexes.py --dry-run # 仅扫描，不修改
  python check_and_fix_indexes.py --apply   # 扫描 + 直接应用，不询问
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.collection import Collection
from pymongo.database import Database

# ─── 连接配置（和 quantFinance/db.py 保持一致）─────────────────────────────
import os
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "quantFinance", ".env"),
            override=False)
load_dotenv(override=False)  # fallback to cwd .env

MONGO_URI   = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DB_FINANCE  = os.getenv("MONGO_DB", "finance")
DB_ANALYZER = os.getenv("ANALYZER_DB_NAME", "quant_analyzer")
DB_DATA     = os.getenv("DATA_DB_NAME", "quant_data")

# ─── 期望索引定义 ──────────────────────────────────────────────────────────
# 格式：(collection, [(field, direction), ...], unique, sparse, name_hint)
# direction: 1=ASCENDING, -1=DESCENDING
# 复合索引顺序遵循 ESR 原则（Equality-Sort-Range）

@dataclass
class IndexSpec:
    collection: str
    keys: List[Tuple[str, int]]           # [(field, direction)]
    unique: bool = False
    sparse: bool = False
    name: Optional[str] = None            # 如果 None，用 auto-name
    reason: str = ""                      # 说明这个索引解决什么查询

    @property
    def key_str(self) -> str:
        parts = []
        for k, d in self.keys:
            parts.append(f"{k}:{'ASC' if d == 1 else 'DESC'}")
        return "  +  ".join(parts)


EXPECTED: Dict[str, List[IndexSpec]] = {

    # ── finance DB ──────────────────────────────────────────────────────────
    DB_FINANCE: [
        IndexSpec("users", [("username", 1)], unique=True, reason="登录查询"),
        IndexSpec("users", [("email", 1)], sparse=True, reason="邮箱查找"),
        IndexSpec("analysis_tasks", [("status", 1), ("created_at", -1)],
                  reason="worker 拉取 pending 任务 + 按时间排列"),
        IndexSpec("analysis_tasks", [("type", 1), ("status", 1), ("created_at", -1)],
                  reason="按 type 过滤 pending，支持行业分析任务"),
        IndexSpec("analysis_tasks", [("user_id", 1), ("created_at", -1)],
                  reason="用户查询自己的任务列表"),
        IndexSpec("analysis_tasks", [("symbol", 1), ("status", 1)],
                  sparse=True, reason="按股票查最新分析任务"),
        IndexSpec("analysis_task_revisions", [("task_id", 1), ("version", -1)],
                  reason="按任务 ID 查历史版本"),
        IndexSpec("quota_usage", [("user_id", 1), ("period", 1)],
                  reason="查单用户当期用量（有重复数据，暂不加 UNIQUE）"),
        IndexSpec("trade_executions", [("symbol", 1), ("trade_date", -1)],
                  reason="按股票查交易记录"),
        IndexSpec("trade_executions", [("user_id", 1), ("trade_date", -1)],
                  reason="按用户查交易记录"),
        IndexSpec("trade_signals", [("symbol", 1), ("signal_time", -1)],
                  reason="按股票查信号"),
        IndexSpec("trade_signals", [("strategy_id", 1), ("signal_time", -1)],
                  reason="按策略查信号"),
        IndexSpec("notifications", [("user_id", 1), ("is_read", 1), ("created_at", -1)],
                  reason="用户未读通知列表"),
        IndexSpec("backtest_tasks", [("status", 1), ("created_at", -1)],
                  reason="回测任务队列"),
        IndexSpec("backtest_tasks", [("user_id", 1), ("created_at", -1)],
                  reason="按用户查回测任务"),
        IndexSpec("backtest_tasks", [("batch_id", 1), ("created_at", 1)],
                  sparse=True, reason="按批量实验查子任务"),
        IndexSpec("backtest_tasks", [("batch_id", 1), ("status", 1)],
                  sparse=True, reason="批量实验状态聚合"),
        IndexSpec("backtest_batches", [("batch_id", 1)], unique=True,
                  reason="按批量实验 ID 查详情"),
        IndexSpec("backtest_batches", [("user_id", 1), ("created_at", -1)],
                  reason="用户批量实验列表"),
        IndexSpec("strategy_lab_loops", [("loop_id", 1)], unique=True,
                  reason="Strategy Lab 自动迭代 ID"),
        IndexSpec("strategy_lab_loops", [("user_id", 1), ("created_at", -1)],
                  reason="用户自动迭代列表"),
        IndexSpec("strategy_lab_loops", [("status", 1), ("updated_at", 1)],
                  reason="调度器扫描运行中的 loop"),
        IndexSpec("backtest_results", [("task_id", 1)], unique=True,
                  reason="按任务 ID 取回测结果"),
        IndexSpec("stock_scores", [("ts_code", 1), ("score_date", -1)],
                  reason="按股票取最新评分"),
        IndexSpec("stock_scores", [("score_date", -1), ("score", -1)],
                  reason="排行榜查询"),
        IndexSpec("entropy_scores", [("score_date", 1), ("index_codes", 1), ("final_entropy_score", 1), ("symbol", 1)],
                  reason="熵值排名按日期与指数池排序"),
        IndexSpec("market_insight_events", [("trade_date", -1), ("snapshot_time", -1), ("confidence", -1)],
                  reason="火眼金睛事件列表排序"),
        IndexSpec("login_logs", [("user_id", 1), ("login_time", -1)],
                  reason="登录历史"),
        IndexSpec("strategy_template", [("strategy_id", 1)],
                  reason="策略模板查询（有 null 重复，暂不加 UNIQUE）"),
        IndexSpec("user_watchlists", [("user_id", 1), ("name", 1)],
                  reason="用户自选股列表"),
        IndexSpec("theme_lag_validation_subscriptions",
                  [("user_id", 1), ("theme_id", 1)],
                  reason="主题补涨订阅查询（有 null 重复，暂不加 UNIQUE）"),
    ],

    # ── quant_analyzer DB ───────────────────────────────────────────────────
    DB_ANALYZER: [
        IndexSpec("analysis_history", [("ts_code", 1), ("analysis_date", -1)],
                  reason="按股票取最新分析"),
        IndexSpec("analysis_history", [("analysis_date", -1)],
                  reason="全局最新分析列表"),
        IndexSpec("analysis_history", [("ts_code", 1), ("analysis_mode", 1), ("analysis_date", -1)],
                  reason="按模式取最新分析"),
        IndexSpec("analysis_history", [("user_id", 1), ("timestamp", -1)],
                  reason="用户分析历史分页列表"),
        IndexSpec("hot_stock_analysis", [("analysis_date", -1)],
                  reason="热股分析最新"),
        IndexSpec("limit_up_analysis", [("trade_date", -1)],
                  reason="涨停分析最新日期"),
        IndexSpec("limit_up_analysis", [("ts_code", 1), ("trade_date", -1)],
                  reason="按股票查涨停分析"),
        IndexSpec("limit_up_daily_narrative", [("trade_date", -1)], unique=True,
                  reason="每日叙事唯一"),
        IndexSpec("macro_analysis", [("analysis_date", -1)],
                  reason="宏观分析最新"),
        IndexSpec("market_analysis_cache", [("analysis_date", -1)],
                  reason="市场分析缓存最新"),
        IndexSpec("risk_report_analysis", [("report_date", -1)],
                  reason="风险报告最新"),
        IndexSpec("etf_analysis", [("ts_code", 1), ("analysis_date", -1)],
                  reason="ETF 分析最新"),
        IndexSpec("analysis_evaluations", [("task_id", 1)],
                  reason="按任务查评估"),
        IndexSpec("portfolio_analysis", [("user_id", 1), ("analysis_date", -1)],
                  reason="用户组合分析"),
        IndexSpec("backtest_batch_reviews", [("batch_id", 1), ("created_at", -1)],
                  reason="按批量回测实验查 AI 复盘"),
    ],

    # ── quant_data DB ───────────────────────────────────────────────────────
    DB_DATA: [
        # 日线行情类（高频查询，最重要）
        IndexSpec("financial_daily_basic", [("ts_code", 1), ("trade_date", -1)],
                  reason="按股票查最新估值"),
        IndexSpec("financial_daily_basic", [("trade_date", -1), ("ts_code", 1)],
                  reason="某交易日批量取市值/估值"),
        IndexSpec("volume_price", [("symbol", 1), ("trade_date", -1)],
                  reason="K 线数据查询"),
        IndexSpec("volume_price", [("ts_code", 1), ("trade_date", -1)],
                  reason="兼容按 ts_code 查最新日线/快照"),
        IndexSpec("volume_price", [("source", 1), ("symbol", 1), ("trade_date", -1)],
                  reason="自选股东财快照按 symbol 和日期查"),
        IndexSpec("volume_price", [("source", 1), ("ts_code", 1), ("trade_date", -1)],
                  reason="自选股东财快照按 ts_code 和日期查"),
        IndexSpec("minute_bars", [("symbol", 1), ("trade_date", -1)],
                  reason="分钟线按标的和日期聚合"),
        IndexSpec("sw_daily", [("ts_code", 1), ("trade_date", -1)],
                  reason="申万行业日线 PE/PB"),
        IndexSpec("sw_weekly", [("ts_code", 1), ("trade_date", -1)],
                  reason="申万行业周线"),
        IndexSpec("sw_monthly", [("ts_code", 1), ("trade_date", -1)],
                  reason="申万行业月线动量"),
        IndexSpec("stock_monthly", [("ts_code", 1), ("trade_date", -1)],
                  reason="个股月线"),
        IndexSpec("stock_weekly", [("ts_code", 1), ("trade_date", -1)],
                  reason="个股周线"),
        # 成分股 / 分类
        IndexSpec("sw_index_member", [("l1_code", 1), ("src", 1), ("is_new", 1)],
                  reason="申万 L1 成员查询"),
        IndexSpec("sw_index_member", [("l2_code", 1), ("src", 1), ("is_new", 1)],
                  reason="申万 L2 成员查询"),
        IndexSpec("sw_index_member", [("l3_code", 1), ("src", 1), ("is_new", 1)],
                  reason="申万 L3 成员查询"),
        IndexSpec("sw_index_member", [("con_code", 1), ("src", 1), ("is_new", 1)],
                  reason="自选股批量反查最新申万行业"),
        IndexSpec("sw_index_member", [("con_code", 1), ("src", 1)],
                  reason="按股票反查所属行业"),
        IndexSpec("sw_index_classify", [("index_code", 1), ("src", 1)],
                  reason="行业元数据查询"),
        IndexSpec("sw_index_classify", [("src", 1), ("level", 1)],
                  reason="按级别列举行业"),
        # 财务数据
        IndexSpec("financial_indicator", [("ts_code", 1), ("end_date", -1)],
                  reason="最新财务指标"),
        IndexSpec("financial_income", [("ts_code", 1), ("end_date", -1)],
                  reason="利润表最新"),
        IndexSpec("financial_balance", [("ts_code", 1), ("end_date", -1)],
                  reason="资产负债表最新"),
        IndexSpec("financial_cashflow", [("ts_code", 1), ("end_date", -1)],
                  reason="现金流表最新"),
        # 涨停梯队
        IndexSpec("limit_up_ladder", [("trade_date", -1)],
                  reason="涨停梯队最新交易日"),
        IndexSpec("limit_up_ladder", [("ts_code", 1), ("trade_date", -1)],
                  reason="个股涨停记录"),
        IndexSpec("limit_down_ladder", [("trade_date", -1)],
                  reason="跌停梯队最新"),
        IndexSpec("daily_limit_up", [("trade_date", -1)],
                  reason="每日涨停汇总"),
        IndexSpec("daily_limit_up", [("ts_code", 1), ("trade_date", -1)],
                  reason="个股涨停日历"),
        # 概念 / 主题
        IndexSpec("dc_concept_member", [("ts_code", 1)],
                  reason="个股所属概念"),
        IndexSpec("dc_concept_member", [("concept_code", 1)],
                  reason="概念成员列表"),
        IndexSpec("dc_concept_index", [("trade_date", -1)],
                  reason="概念指数最新"),
        # 资金流向
        IndexSpec("money_flow", [("ts_code", 1), ("trade_date", -1)],
                  reason="个股资金流向"),
        IndexSpec("sector_flow", [("trade_date", -1)],
                  reason="行业资金流"),
        # 基础信息
        IndexSpec("stock_info", [("ts_code", 1)], unique=True, reason="股票基础信息主键"),
        IndexSpec("stock_info", [("symbol", 1)], sparse=True, reason="按6位代码查"),
        IndexSpec("stock_info", [("pinyin", 1)], sparse=True, reason="缩写首字母前缀联想"),
        IndexSpec("stock_info", [("pinyin_full", 1)], sparse=True, reason="全拼前缀联想"),
        IndexSpec("stock_info", [("name", 1)], sparse=True, reason="中文名前缀联想"),
        IndexSpec("etf_basic", [("ts_code", 1)], unique=True, reason="ETF 基础信息"),
        IndexSpec("etf_daily", [("ts_code", 1), ("trade_date", -1)],
                  reason="ETF 日线"),
        IndexSpec("etf_share_size", [("trade_date", -1)],
                  reason="ETF 规模最新交易日"),
        IndexSpec("etf_share_size", [("trade_date", 1), ("total_share", -1)],
                  reason="ETF 规模按份额分页排序"),
        IndexSpec("etf_share_size", [("trade_date", 1), ("total_size", -1)],
                  reason="ETF 规模按总市值分页排序"),
        # 持仓
        IndexSpec("top10_holders", [("ts_code", 1), ("end_date", -1)],
                  reason="十大股东最新"),
        IndexSpec("top10_floatholders", [("ts_code", 1), ("end_date", -1)],
                  reason="十大流通股东最新"),
        IndexSpec("fund_holdings", [("ts_code", 1), ("end_date", -1)],
                  reason="基金持仓最新"),
        # 宏观 / 利率
        IndexSpec("us_rates_daily", [("trade_date", -1)], reason="美债利率最新"),
        IndexSpec("global_market_daily", [("symbol", 1), ("trade_date", -1)],
                  reason="全球行情最新"),
        # 机构快照
        IndexSpec("institutional_deep_snapshot", [("ts_code", 1), ("as_of", -1)],
                  reason="机构深度快照最新"),
        # 盈利预告
        IndexSpec("earnings_forecast", [("ts_code", 1), ("ann_date", -1)],
                  reason="盈利预告最新"),
        IndexSpec("earnings_disclosure", [("ts_code", 1), ("end_date", -1)],
                  reason="业绩披露日历"),
    ],
}


# ─── 索引比对逻辑 ─────────────────────────────────────────────────────────

def _existing_index_key_sets(col: Collection) -> Set[Tuple[Tuple[str, int], ...]]:
    """把现有索引的 key 部分转成可 hash 的 frozenset（用于比对）。"""
    result = set()
    for idx in col.index_information().values():
        key_tuple = tuple((k, v) for k, v in idx["key"])
        # 去掉 _id 索引
        if key_tuple == (("_id", 1),):
            continue
        result.add(key_tuple)
    return result


def scan_missing(client: MongoClient) -> Dict[str, List[IndexSpec]]:
    """返回每个 DB 中缺失的索引列表。"""
    missing: Dict[str, List[IndexSpec]] = {}
    for db_name, specs in EXPECTED.items():
        db: Database = client[db_name]
        existing_cols = set(db.list_collection_names())
        db_missing: List[IndexSpec] = []
        for spec in specs:
            if spec.collection not in existing_cols:
                continue  # collection 本身不存在，跳过
            col = db[spec.collection]
            existing = _existing_index_key_sets(col)
            want = tuple((k, v) for k, v in spec.keys)
            if want not in existing:
                db_missing.append(spec)
        if db_missing:
            missing[db_name] = db_missing
    return missing


def print_report(missing: Dict[str, List[IndexSpec]]) -> int:
    total = sum(len(v) for v in missing.values())
    if total == 0:
        print("\n✅  所有被检查的索引均已存在，无需修复。")
        return 0
    print(f"\n⚠️  共发现 {total} 个缺失索引：\n")
    for db_name, specs in missing.items():
        print(f"  📦  {db_name}  ({len(specs)} 个)")
        for s in specs:
            flag = ""
            if s.unique:
                flag += " [UNIQUE]"
            if s.sparse:
                flag += " [SPARSE]"
            print(f"      {s.collection:<40} {s.key_str}{flag}")
            print(f"      {'':40} → {s.reason}")
    return total


def apply_indexes(client: MongoClient, missing: Dict[str, List[IndexSpec]],
                  verbose: bool = True) -> int:
    """Create the missing indexes and return the number of failures so callers
    can surface a non-zero exit code. Per-index fault tolerant: one failure
    (e.g. a unique constraint that conflicts with existing data) never aborts
    the rest."""
    total = sum(len(v) for v in missing.values())
    done = 0
    failed = 0
    for db_name, specs in missing.items():
        db: Database = client[db_name]
        for spec in specs:
            col = db[spec.collection]
            keys = [(k, v) for k, v in spec.keys]
            kwargs: Dict[str, Any] = {}
            if spec.unique:
                kwargs["unique"] = True
            if spec.sparse:
                kwargs["sparse"] = True
            if spec.name:
                kwargs["name"] = spec.name
            try:
                idx_name = col.create_index(keys, **kwargs)
                done += 1
                if verbose:
                    print(f"  ✓  [{db_name}] {spec.collection}  →  {idx_name}")
            except Exception as e:
                failed += 1
                print(f"  ✗  [{db_name}] {spec.collection} {spec.key_str}: {e}",
                      file=sys.stderr)
    print(f"\n✅  完成：建立 {done}/{total} 个索引。")
    if failed:
        print(f"⚠️  {failed}/{total} 个索引创建失败，详见上方 stderr。", file=sys.stderr)
    return failed


# ─── 主程序 ───────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="检查并修复 MongoDB 索引")
    parser.add_argument("--dry-run", action="store_true",
                        help="仅扫描报告，不创建索引")
    parser.add_argument("--apply", action="store_true",
                        help="扫描后直接应用，不询问确认")
    args = parser.parse_args()

    print(f"连接 MongoDB: {MONGO_URI}")
    print(f"目标数据库: {DB_FINANCE}  |  {DB_ANALYZER}  |  {DB_DATA}")

    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    try:
        client.admin.command("ping")
    except Exception as e:
        print(f"❌  无法连接 MongoDB: {e}", file=sys.stderr)
        return 1

    failed = 0
    try:
        print("\n🔍  扫描索引中…")
        missing = scan_missing(client)
        total = print_report(missing)

        if total == 0 or args.dry_run:
            return 0

        if args.apply:
            print("\n⚙️  --apply 模式，直接创建索引…\n")
            failed = apply_indexes(client, missing)
        else:
            print("\n是否立即创建以上缺失索引？(y/N) ", end="", flush=True)
            ans = input().strip().lower()
            if ans in ("y", "yes"):
                print()
                failed = apply_indexes(client, missing)
            else:
                print("已取消，未做任何修改。")
    finally:
        client.close()

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
