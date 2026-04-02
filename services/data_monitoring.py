"""
Data ingestion catalog + job_status + freshness helpers for admin / data-pulse APIs.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

# finance DB (main) and quant_data handle
from db import data_db, db

_CATALOG_PATH = Path(__file__).resolve().parent.parent / "config" / "data_pipelines_catalog.yaml"


def load_pipelines_catalog() -> List[Dict[str, Any]]:
    path = os.environ.get("DATA_PIPELINES_CATALOG", str(_CATALOG_PATH))
    p = Path(path)
    if not p.is_file():
        return []
    with open(p, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return list(data.get("pipelines") or [])


def normalize_job_status_doc(doc: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Map a job_status document to API fields (supports legacy nested payload)."""
    if not doc:
        return {
            "status": "no_record",
            "records_changed": None,
            "completed_at": None,
            "updated_at": None,
            "error": None,
        }
    nested = doc.get("payload") or {}
    status = doc.get("status") or nested.get("status") or "unknown"
    records = doc.get("records_changed")
    if records is None:
        records = nested.get("records_changed")
    completed = doc.get("completed_at") or doc.get("updated_at")
    err = doc.get("error") or nested.get("error")
    return {
        "status": status,
        "records_changed": records,
        "completed_at": completed,
        "updated_at": doc.get("updated_at"),
        "error": err,
    }


def _get_collection(database: str, collection: str):
    if database == "finance":
        return db[collection]
    if database == "quant_data":
        return data_db[collection]
    raise ValueError(f"Unknown database: {database}")


def _max_freshness_value(
    database: str,
    collection: str,
    field: str,
    field_kind: str,
    document_id: Optional[str] = None,
) -> Optional[Any]:
    coll = _get_collection(database, collection)
    if document_id:
        doc = coll.find_one({"_id": document_id})
        if not doc:
            return None
        return doc.get(field)
    if field_kind == "datetime":
        latest = coll.find_one({}, sort=[(field, -1)], projection={field: 1, "_id": 0})
    else:
        latest = coll.find_one({}, sort=[(field, -1)], projection={field: 1, "_id": 0})
    if not latest:
        return None
    return latest.get(field)


def build_pipelines_status_rows() -> List[Dict[str, Any]]:
    """Latest job_status per catalog pipeline."""
    catalog = load_pipelines_catalog()
    rows: List[Dict[str, Any]] = []
    for p in catalog:
        key = p.get("job_status_key") or p.get("id")
        label = p.get("label") or key
        doc = db["job_status"].find_one({"job_type": key}, sort=[("date", -1)])
        norm = normalize_job_status_doc(doc)
        completed = norm["completed_at"]
        rows.append(
            {
                "id": p.get("id"),
                "job_type": key,
                "label": label,
                "schedule_hint": p.get("schedule_hint"),
                "date": doc.get("date") if doc else None,
                "status": norm["status"],
                "records_changed": norm["records_changed"],
                "completed_at": completed.isoformat() if hasattr(completed, "isoformat") else None,
                "error": norm["error"],
            }
        )
    return rows


def build_sync_health_from_catalog() -> Dict[str, Any]:
    """Healthy = latest run status == complete for each distinct job_type in DB that appears in catalog."""
    catalog = load_pipelines_catalog()
    keys = {p.get("job_status_key") or p.get("id") for p in catalog}
    jobs_out: List[Dict[str, Any]] = []
    healthy = 0
    for key in keys:
        doc = db["job_status"].find_one({"job_type": key}, sort=[("date", -1)])
        norm = normalize_job_status_doc(doc)
        st = norm["status"]
        if st == "complete":
            healthy += 1
        jobs_out.append(
            {
                "type": key,
                "latest_date": doc.get("date") if doc else None,
                "status": st if doc else "no_record",
            }
        )
    total = len(keys)
    pct = round(healthy / total * 100) if total > 0 else 0
    return {
        "total_pipelines": total,
        "healthy": healthy,
        "health_pct": pct,
        "jobs": jobs_out,
    }


def build_freshness_rows() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for p in load_pipelines_catalog():
        fr = p.get("freshness") or {}
        if not fr.get("collection") or not fr.get("field"):
            continue
        database = fr.get("database", "quant_data")
        field_kind = fr.get("field_kind", "date_string")
        doc_id = fr.get("document_id")
        try:
            raw = _max_freshness_value(
                database,
                fr["collection"],
                fr["field"],
                field_kind,
                document_id=doc_id,
            )
        except Exception as e:
            raw = None
            err = str(e)
        else:
            err = None
        iso = None
        if raw is not None:
            if hasattr(raw, "isoformat"):
                iso = raw.isoformat()
            else:
                iso = str(raw)
        out.append(
            {
                "pipeline_id": p.get("id"),
                "label": p.get("label"),
                "database": database,
                "collection": fr.get("collection"),
                "field": fr.get("field"),
                "latest_value": iso,
                "raw_value": raw,
                "error": err,
            }
        )
    return out


def build_data_monitoring_overview() -> Dict[str, Any]:
    """Single payload for data-pulse + admin."""
    return {
        "pipelines": build_pipelines_status_rows(),
        "sync_health": build_sync_health_from_catalog(),
        "freshness": build_freshness_rows(),
        "catalog_version": "1",
    }


def expected_latest_trade_date_hint() -> str:
    """Approximate expected latest trade_date (weekday-only; ignores holidays)."""
    now = datetime.now()
    d = now.date()
    wd = d.weekday()
    if wd == 5:
        d = d.fromordinal(d.toordinal() - 1)
    elif wd == 6:
        d = d.fromordinal(d.toordinal() - 2)
    return d.strftime("%Y%m%d")


__all__ = [
    "load_pipelines_catalog",
    "normalize_job_status_doc",
    "build_pipelines_status_rows",
    "build_sync_health_from_catalog",
    "build_freshness_rows",
    "build_data_monitoring_overview",
    "expected_latest_trade_date_hint",
]
