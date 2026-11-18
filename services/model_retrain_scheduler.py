"""Monthly/quarterly retrain scheduler.

Reads ``quant_ml.model_retrain_schedules`` and enqueues a versioned training
job on the first trading day of each period. The scheduler is idempotent:
an existing model_id/period is skipped and the schedule advances.
"""

from __future__ import annotations

import json
import logging
from datetime import date as _date
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from db import get_data_db, get_ml_db_collections
from services.model_retrain import RetrainError, build_retrain_job_doc, latest_volume_price_date

log = logging.getLogger(__name__)
CN_TZ = ZoneInfo("Asia/Shanghai")
COLLECTION_SCHEDULES = "model_retrain_schedules"


def _today() -> str:
    return datetime.now(CN_TZ).strftime("%Y%m%d")


def _is_trading_day(data_db, day: str) -> bool:
    row = data_db["trade_calendar"].find_one(
        {"exchange": "SSE", "trade_date": day},
        {"_id": 0, "is_open": 1},
    )
    if not row:
        return True  # calendar absent/partial: do not suppress the run
    return str(row.get("is_open")) in {"1", "True", "true"}


def _period_key(day: str, cadence: str) -> str:
    parsed = datetime.strptime(day, "%Y%m%d").date()
    if cadence == "monthly":
        return f"{parsed.year:04d}{parsed.month:02d}"
    quarter = (parsed.month - 1) // 3 + 1
    return f"{parsed.year:04d}Q{quarter}"


def _next_period_start(day: str, cadence: str) -> str:
    parsed = datetime.strptime(day, "%Y%m%d").date()
    if cadence == "monthly":
        year = parsed.year + (1 if parsed.month == 12 else 0)
        month = 1 if parsed.month == 12 else parsed.month + 1
        return f"{year:04d}{month:02d}01"
    year = parsed.year
    quarter = (parsed.month - 1) // 3 + 1
    if quarter == 4:
        year += 1
        quarter = 1
    else:
        quarter += 1
    month = (quarter - 1) * 3 + 1
    return f"{year:04d}{month:02d}01"


def _first_trading_day(data_db, start: str) -> str:
    row = data_db["trade_calendar"].find_one(
        {
            "exchange": "SSE",
            "is_open": {"$in": [1, "1"]},
            "trade_date": {"$gte": start},
        },
        {"_id": 0, "trade_date": 1},
        sort=[("trade_date", 1)],
    )
    return str(row["trade_date"]) if row and row.get("trade_date") else start


def initial_next_run_date(cadence: str, today: Optional[str] = None) -> str:
    data_db = get_data_db()
    day = today or _today()
    return _first_trading_day(data_db, _next_period_start(day, cadence))


def run_due_retrains(*, dry_run: bool = False) -> Dict[str, Any]:
    collections = get_ml_db_collections()
    schedules_coll = collections.get(COLLECTION_SCHEDULES)
    jobs = collections["model_training_jobs"]
    models = collections.get("models")
    if schedules_coll is None:
        return {"ok": False, "error": "model_retrain_schedules collection unavailable"}
    data_db = get_data_db()
    today = _today()
    if not _is_trading_day(data_db, today):
        return {"ok": True, "today": today, "skipped": "not_trading_day", "results": []}
    latest = latest_volume_price_date()
    results: List[Dict[str, Any]] = []
    for schedule in schedules_coll.find({"enabled": {"$ne": False}}):
        schedule_id = str(schedule.get("schedule_id") or "")
        cadence = str(schedule.get("cadence") or "").strip().lower()
        next_run = str(schedule.get("next_run_date") or "").strip()
        if next_run and next_run > today:
            continue
        if cadence not in {"monthly", "quarterly"}:
            results.append({"schedule_id": schedule_id, "status": "error", "error": f"unknown cadence {cadence}"})
            continue
        family_id = str(schedule.get("family_id") or "").strip()
        prefix = str(schedule.get("model_id_prefix") or family_id).strip()
        if not family_id or not prefix:
            results.append({"schedule_id": schedule_id, "status": "error", "error": "family_id/model_id_prefix required"})
            continue
        period = _period_key(today, cadence)
        model_id = f"{prefix}-{period}"
        next_run_date = _first_trading_day(data_db, _next_period_start(today, cadence))

        existing = models.find_one({"model_id": model_id}) if models is not None else None
        existing_job = jobs.find_one({"params.model_id": model_id})
        if existing or existing_job:
            schedules_coll.update_one(
                {"_id": schedule["_id"]},
                {
                    "$set": {
                        "last_run_date": today,
                        "last_model_id": model_id,
                        "last_skipped": "model_id already exists",
                        "next_run_date": next_run_date,
                        "updated_at": datetime.utcnow(),
                    }
                },
            )
            results.append({"schedule_id": schedule_id, "status": "skipped", "model_id": model_id})
            continue

        source_job = jobs.find_one({"job_id": str(schedule.get("source_job_id") or "")})
        if not source_job:
            schedules_coll.update_one(
                {"_id": schedule["_id"]},
                {"$set": {"last_error": "source_job not found", "updated_at": datetime.utcnow()}},
            )
            results.append({"schedule_id": schedule_id, "status": "error", "error": "source_job not found"})
            continue
        user_id = str(schedule.get("user_id") or source_job.get("user_id") or "")
        try:
            doc = build_retrain_job_doc(
                collections,
                source_job,
                model_id=model_id,
                end_date=latest,
                family_id=family_id,
                user_id=user_id,
            )
        except RetrainError as exc:
            schedules_coll.update_one(
                {"_id": schedule["_id"]},
                {"$set": {"last_error": str(exc.detail), "updated_at": datetime.utcnow()}},
            )
            results.append({"schedule_id": schedule_id, "status": "error", "error": str(exc.detail)})
            continue
        doc["schedule_id"] = schedule_id
        if dry_run:
            results.append({"schedule_id": schedule_id, "status": "would_enqueue", "model_id": model_id})
            continue
        jobs.insert_one(doc)
        schedules_coll.update_one(
            {"_id": schedule["_id"]},
            {
                "$set": {
                    "last_run_date": today,
                    "last_job_id": doc["job_id"],
                    "last_model_id": model_id,
                    "last_skipped": None,
                    "last_error": None,
                    "next_run_date": next_run_date,
                    "updated_at": datetime.utcnow(),
                }
            },
        )
        results.append(
            {
                "schedule_id": schedule_id,
                "status": "enqueued",
                "model_id": model_id,
                "job_id": doc["job_id"],
            }
        )
    return {"ok": True, "today": today, "dry_run": dry_run, "results": results}


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO)
    result = run_due_retrains()
    print(json.dumps(result, default=str, ensure_ascii=False))
    errors = [item for item in result.get("results") or [] if item.get("status") == "error"]
    return 1 if errors or not result.get("ok") else 0


if __name__ == "__main__":
    raise SystemExit(main())
