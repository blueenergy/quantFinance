"""Build a retrain job document from a completed source job.

Shared by the HTTP retrain endpoint and the scheduled retrain worker so both
copy the same full recipe and apply the same versioning/panel rules.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from db import get_data_db


class RetrainError(Exception):
    def __init__(self, status_code: int, detail: Any):
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


def latest_volume_price_date() -> Optional[str]:
    row = (
        get_data_db()["volume_price"]
        .find_one({}, {"trade_date": 1}, sort=[("trade_date", -1)])
    )
    return str(row["trade_date"]) if row and row.get("trade_date") else None


def build_retrain_job_doc(
    collections: Dict[str, Any],
    source_job: Dict[str, Any],
    *,
    model_id: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    name: Optional[str] = None,
    family_id: Optional[str] = None,
    parquet_path: Optional[str] = None,
    snapshot_id: Optional[str] = None,
    user_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Return a pending job doc; raise ``RetrainError`` on invalid input."""
    if source_job.get("status") != "completed" or not source_job.get("result_id"):
        raise RetrainError(409, "source training job must be completed")
    model_id = str(model_id or "").strip()
    if not model_id:
        raise RetrainError(422, "model_id is required")
    models = collections.get("models")
    if models is not None and models.find_one({"model_id": model_id}):
        raise RetrainError(
            409,
            {
                "message": f"model_id={model_id} already exists; use a new version id",
                "model_id": model_id,
            },
        )

    params = dict(source_job.get("params") or {})
    start_date = start_date or str(params.get("start_date") or "")
    end_date = end_date or latest_volume_price_date()
    if not start_date or not end_date:
        raise RetrainError(422, "retrain requires start_date and end_date (or a source start_date)")
    if end_date < start_date:
        raise RetrainError(422, f"end_date {end_date} precedes start_date {start_date}")

    data_source = str(params.get("data_source") or "synthetic")
    explicit_parquet = parquet_path
    explicit_snapshot = snapshot_id
    source_parquet = str(params.get("parquet_path") or "").strip() or None
    source_snapshot = str(params.get("snapshot_id") or "").strip() or None
    parquet_path = explicit_parquet or source_parquet
    snapshot_id = explicit_snapshot or source_snapshot
    if not explicit_parquet and not explicit_snapshot:
        if data_source == "production_scores":
            # Force a fresh panel to the new end_date instead of reusing an
            # older fixed parquet path from the source job.
            parquet_path = None
            snapshot_id = None
        elif not source_parquet:
            raise RetrainError(
                422,
                "source job did not use production_scores and has no parquet_path; "
                "provide parquet_path/snapshot_id for the retrain",
            )

    name = name or f"{source_job.get('name') or model_id} retrain {end_date}"
    params.update(
        {
            "name": name,
            "model_id": model_id,
            "start_date": start_date,
            "end_date": end_date,
        }
    )
    if family_id:
        params["family_id"] = family_id
    if parquet_path:
        params["parquet_path"] = parquet_path
    else:
        params.pop("parquet_path", None)
    if snapshot_id:
        params["snapshot_id"] = snapshot_id
    else:
        params.pop("snapshot_id", None)

    return {
        "job_id": _new_job_id(),
        "user_id": user_id or str(source_job.get("user_id") or ""),
        "name": name,
        "status": "pending",
        "created_at": now or datetime.utcnow(),
        "params": params,
        "retrain_of": str(source_job.get("job_id") or ""),
    }


def _new_job_id() -> str:
    import uuid

    return str(uuid.uuid4())
