#!/usr/bin/env python3
"""
验证 quant_data.money_flow 与 finance 中 moneyflow 任务记录是否一致、数据是否过旧。

与数据脉搏使用相同约定：
  - 优先读 finance.pipeline_tasks（domain=data_collection, task_type=moneyflow）
  - 否则回退 job_status
  - 新鲜度：quant_data.money_flow 中 trade_date 降序取一条（与 data_monitoring._max_freshness_value 一致）

用法（在已配置 .env 的目录，或先 export MONGO_URI 等）:

  cd /path/to/quantFinance
  python tools/check_moneyflow_health.py
  python tools/check_moneyflow_health.py --json
  python tools/check_moneyflow_health.py --strict   # 有问题时 exit 1
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# quantFinance 根目录（便于 import db、services.data_monitoring）
_QF = Path(__file__).resolve().parent.parent
if str(_QF) not in sys.path:
    sys.path.insert(0, str(_QF))

try:
    from dotenv import load_dotenv

    _ENV = _QF / ".env"
    load_dotenv(_ENV, override=False)
    load_dotenv(override=False)
except Exception:
    pass

KEY = "moneyflow"


def _norm_ymd(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "strftime"):
        try:
            return value.strftime("%Y%m%d")
        except Exception:
            return None
    s = str(value).replace("-", "").strip()
    if len(s) >= 8 and s[:8].isdigit():
        return s[:8]
    return None


def _fmt_dt(v: Any) -> Optional[str]:
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        try:
            return v.isoformat()
        except Exception:
            return str(v)
    return str(v)


def _count_invalid_ingestion_dates_in_pipeline(finance) -> int:
    """8 位以外或无法解析的 date（如 2060408 存成 int 只有 7 位数字）条数。"""
    from services.data_monitoring import _ingestion_date_ymd_int

    n = 0
    for d in finance["pipeline_tasks"].find(
        {"domain": "data_collection", "task_type": KEY}
    ):
        v = d.get("date")
        if v is None:
            continue
        if _ingestion_date_ymd_int(v) == 0:
            n += 1
    return n


def _normalize_run(doc: Optional[Dict[str, Any]], *, date_ymd8: Optional[str]) -> Dict[str, Any]:
    if not doc:
        return {
            "source": None,
            "status": "no_record",
            "date": None,
            "date_ymd8": None,
            "error": None,
            "records_changed": None,
            "completed_at": None,
        }
    if doc.get("domain") == "data_collection":
        pl = doc.get("payload") or {}
        return {
            "source": "pipeline_tasks",
            "status": (doc.get("status") or pl.get("status") or "unknown"),
            "date": doc.get("date"),
            "date_ymd8": date_ymd8,
            "error": doc.get("error") or pl.get("error"),
            "records_changed": pl.get("records_changed"),
            "completed_at": _fmt_dt(doc.get("completed_at") or doc.get("updated_at")),
        }
    pl = doc.get("payload") or {}
    st = doc.get("status") or pl.get("status") or "unknown"
    return {
        "source": "job_status",
        "status": st,
        "date": doc.get("date"),
        "date_ymd8": date_ymd8,
        "error": doc.get("error") or pl.get("error"),
        "records_changed": doc.get("records_changed") or pl.get("records_changed"),
        "completed_at": _fmt_dt(doc.get("completed_at") or doc.get("updated_at")),
    }


def _max_freshness_trade_date(data) -> Tuple[Optional[Any], Optional[str], int]:
    coll = data["money_flow"]
    n = coll.estimated_document_count() if hasattr(coll, "estimated_document_count") else coll.count_documents({})
    latest = coll.find_one({}, sort=[("trade_date", -1)], projection={"trade_date": 1, "_id": 0})
    if not latest:
        return None, None, int(n)
    raw = latest.get("trade_date")
    return raw, _norm_ymd(raw), int(n)


def _state_last(data) -> Optional[str]:
    doc = data["collect_state"].find_one({"_id": "money_flow_last_date"})
    if not doc:
        return None
    v = doc.get("last_date")
    return _norm_ymd(v) or (str(v) if v is not None else None)


def _calendar_days_ago(ymd: str) -> Optional[int]:
    if not ymd or len(ymd) != 8 or not ymd.isdigit():
        return None
    try:
        d0 = date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8]))
    except ValueError:
        return None
    return (date.today() - d0).days


def build_report() -> Dict[str, Any]:
    issues: List[str] = []
    warnings: List[str] = []
    try:
        from db import data_db, db
        from services.data_monitoring import _ingestion_date_ymd_int, _latest_ingestion_doc_for_catalog_key
    except Exception as e:
        return {
            "ok": False,
            "error": f"无法加载 quantFinance 依赖: {e}",
            "issues": [str(e)],
            "warnings": [],
        }

    try:
        db.client.admin.command("ping")
    except Exception as e:
        return {
            "ok": False,
            "error": f"Mongo 不可达: {e}",
            "issues": ["无法连接 MongoDB"],
            "warnings": [],
        }

    finance = db
    data = data_db
    run_doc = _latest_ingestion_doc_for_catalog_key(KEY)
    y8 = _ingestion_date_ymd_int(run_doc.get("date") if run_doc else None) if run_doc else 0
    run = _normalize_run(
        run_doc, date_ymd8=(f"{y8:08d}" if y8 else None)
    )
    raw_max, ymd_max, mcount = _max_freshness_trade_date(data)
    state_ymd = _state_last(data)
    invalid_rows = _count_invalid_ingestion_dates_in_pipeline(finance)

    if run["status"] == "failed":
        issues.append("最近一条 moneyflow 任务状态为 failed")
    if run["status"] == "no_record":
        warnings.append("finance 中无 moneyflow 的 pipeline_tasks/job_status 记录")
    if mcount == 0:
        issues.append("quant_data.money_flow 集合为空")
    if ymd_max and state_ymd and ymd_max != state_ymd:
        warnings.append(
            f"collect_state 的 last_date({state_ymd}) 与 数据最大 trade_date({ymd_max}) 不一致"
        )

    if invalid_rows:
        warnings.append(
            f"pipeline_tasks 中有 {invalid_rows} 条 moneyflow 的 `date` 非有效 8 位 YYYYMMDD"
            f"（历史脏数据如 2060408）；可删除或把 date 改为 20260408 等形式；"
            f"已修复 API/脚本按 8 位比大小选「业务上最新」记录。"
        )
    if run.get("date") is not None and (not y8) and mcount > 0:
        warnings.append(
            "当前展示的「选中的任务行」的 date 无法解析为 8 位 YYYYMMDD，"
            "但库中可能仍有其它行；已按归一化日期与更新时间重选最新。请清理脏 date。"
        )

    days = _calendar_days_ago(ymd_max) if ymd_max else None
    if days is not None and days > 7 and run.get("status") == "complete":
        warnings.append(
            f"数据最后日期偏旧：距「最大 trade_date」{days} 个自然日（>7），请结合节假日/任务排期自行判断"
        )

    rint = y8
    ymd_i = int(ymd_max) if ymd_max and ymd_max.isdigit() else 0
    if rint and ymd_i and rint != ymd_i:
        warnings.append(
            f"选中的有效任务 date(YYYYMMDD)={rint} 与 money_flow 最大 trade_date={ymd_max} 不一致（可能只记了子区间或未写 pipeline）"
        )

    return {
        "ok": True,
        "mongo_uri_host": os.getenv("MONGO_URI", "").split("@")[-1][:64],
        "finance_db": db.name,
        "data_db": data_db.name,
        "run": run,
        "data": {
            "money_flow_documents": mcount,
            "max_trade_date_raw": raw_max,
            "max_trade_date": ymd_max,
            "days_since_max_trade_date": days,
        },
        "collect_state": {"money_flow_last_date": state_ymd},
        "issues": issues,
        "warnings": warnings,
        "meta": {"invalid_pipeline_date_rows": invalid_rows},
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="检查 moneyflow 数据与任务健康情况")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    ap.add_argument(
        "--strict",
        action="store_true",
        help="若存在 issues 或库无数据，则以 exit code 1 退出",
    )
    args = ap.parse_args()
    r = build_report()
    if args.json:
        print(json.dumps(r, ensure_ascii=False, default=str, indent=2))
        if not r.get("ok"):
            sys.exit(1)
    else:
        if not r.get("ok"):
            print("错误:", r.get("error"))
            sys.exit(1)
        print("Mongo:", r.get("mongo_uri_host"), "| finance =", r["finance_db"], "| data =", r["data_db"])
        print()
        run = r["run"]
        print("【任务记录】", run.get("source") or "无")
        print("  status     :", run.get("status"))
        print("  date (raw) :", run.get("date"))
        print("  YYYYMMDD   :", run.get("date_ymd8") or "（无法从 raw 解析为 8 位）")
        print("  completed  :", run.get("completed_at"))
        print("  records    :", run.get("records_changed"))
        if run.get("error"):
            print("  error      :", run.get("error"))
        print()
        d = r["data"]
        print("【money_flow 集合】")
        print("  文档数     :", d.get("money_flow_documents"))
        print("  最大 trade_date (raw):", d.get("max_trade_date_raw"))
        print("  最大 trade_date (norm):", d.get("max_trade_date"))
        if d.get("days_since_max_trade_date") is not None:
            print("  距今天数   :", d["days_since_max_trade_date"], "（自然日）")
        print()
        print("【collect_state】 last_date :", r.get("collect_state", {}).get("money_flow_last_date"))
        print()
        iss = r.get("issues") or []
        warn = r.get("warnings") or []
        if iss:
            print("【需处理】")
            for i in iss:
                print(" -", i)
        if warn:
            print("【提示】")
            for w in warn:
                print(" -", w)
        if not iss and not warn:
            print("【提示】无告警（仍请结合交易日/上游接口延迟自行核对）")
        print()
        print("补跑示例: python -m quant_data_engine.tasks.run_moneyflow_sync --days 5")

    if args.strict and r.get("ok"):
        empty = (r.get("data") or {}).get("money_flow_documents", 0) == 0
        if r.get("issues") or empty:
            sys.exit(1)


if __name__ == "__main__":
    main()
