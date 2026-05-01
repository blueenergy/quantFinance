"""
Data ingestion catalog + latest run docs (pipeline_tasks preferred, job_status fallback)
+ freshness helpers for admin / data-pulse APIs.
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


def _ingestion_date_ymd_int(value: Any) -> int:
    """
    将 job_status / pipeline_tasks 的 date 归一为 YYYYMMDD 整型，用于比大小。
    非 8 位数字、缺失或非法时返回 0（避免把 7 位脏数据 2060408 当成「最大」与 20260421 混排）。
    """
    if value is None:
        return 0
    s = str(value).replace("-", "").replace("/", "").strip()
    digits = "".join(c for c in s if c.isdigit())[:8]
    if len(digits) < 8:
        return 0
    return int(digits)


def _ingestion_row_sort_key(doc: Optional[Dict[str, Any]]) -> tuple:
    """(业务日期, 时间戳) — 按业务日期选最新，同日复用 updated 最大的。"""
    if not doc:
        return (0, 0.0)
    y = _ingestion_date_ymd_int(doc.get("date"))
    u = doc.get("updated_at") or doc.get("completed_at")
    if not isinstance(u, datetime):
        return (y, 0.0)
    if u.tzinfo is None:
        u = u.replace(tzinfo=timezone.utc)
    return (y, float(u.timestamp()))


def _latest_ingestion_doc_for_catalog_key(key: str) -> Optional[Dict[str, Any]]:
    """取「业务上最新」的入库记录：合并 pipeline_tasks 与 job_status 后按归一化 date 比大小。

    原因：1) 不可依赖 Mongo 对 `date` 的 sort（int/str、7/8 位混存会排错）；
    2) 仅看 pipeline 时，若其中只有脏 date，会漏掉 job_status 里正确的记录。
    """
    pt_docs = list(
        db["pipeline_tasks"].find({"domain": "data_collection", "task_type": key})
    )
    js_docs = list(db["job_status"].find({"job_type": key}))
    all_docs = pt_docs + js_docs
    if not all_docs:
        return None
    return max(all_docs, key=_ingestion_row_sort_key)


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


def normalize_ingestion_monitor_doc(doc: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Normalize latest run doc from pipeline_tasks (data_collection) or legacy job_status."""
    if not doc:
        return normalize_job_status_doc(None)
    if doc.get("domain") == "data_collection":
        pl = doc.get("payload") or {}
        status = doc.get("status") or pl.get("status") or "unknown"
        records = pl.get("records_changed")
        completed = doc.get("completed_at") or doc.get("updated_at")
        err = doc.get("error") or pl.get("error")
        return {
            "status": status,
            "records_changed": records,
            "completed_at": completed,
            "updated_at": doc.get("updated_at"),
            "error": err,
        }
    return normalize_job_status_doc(doc)


def _get_collection(database: str, collection: str):
    if database == "finance":
        return db[collection]
    if database == "quant_data":
        return data_db[collection]
    raise ValueError(f"Unknown database: {database}")


def _normalize_yyyymmdd(value: Any) -> Optional[str]:
    """Normalize stored date-ish values to ``YYYYMMDD`` string for comparisons."""
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


def _field_equality_filter(field: str, norm: str) -> Dict[str, Any]:
    """Match common Mongo representations (string vs int) for a calendar end date."""
    if not norm or len(norm) != 8 or not norm.isdigit():
        return {field: norm}
    try:
        as_int = int(norm)
    except ValueError:
        return {field: norm}
    return {"$or": [{field: norm}, {field: as_int}]}


def _coverage_latest_ratio(
    data_coll,
    field: str,
    latest_raw: Any,
    universe_coll,
    match_key: str,
) -> Optional[Dict[str, Any]]:
    """
    Global max ``field`` in ``data_coll`` defines the target period ``L``.

    Denominator: universe rows with non-empty ``match_key`` (e.g. ``stock_info``).

    Numerator: distinct ``match_key`` in ``data_coll`` at period ``L`` that also exist
    in the universe collection (intersection), so the ratio is "of listed stocks we
    track, how many already have this report period".
    """
    norm = _normalize_yyyymmdd(latest_raw)
    if not norm:
        return None
    try:
        denom = int(
            universe_coll.count_documents(
                {match_key: {"$exists": True, "$nin": [None, ""]}},
            )
        )
    except Exception:
        return None
    if denom <= 0:
        return None
    universe_name = universe_coll.name
    try:
        pipeline = [
            {"$match": {**_field_equality_filter(field, norm), match_key: {"$exists": True, "$nin": [None, ""]}}},
            {
                "$lookup": {
                    "from": universe_name,
                    "let": {"mk": f"${match_key}"},
                    "pipeline": [
                        {"$match": {"$expr": {"$eq": [f"${match_key}", "$$mk"]}}},
                        {"$limit": 1},
                    ],
                    "as": "_universe_hit",
                }
            },
            {"$match": {"_universe_hit": {"$ne": []}}},
            {"$group": {"_id": f"${match_key}"}},
            {"$count": "n"},
        ]
        agg = list(data_coll.aggregate(pipeline))
        num = int(agg[0]["n"]) if agg else 0
    except Exception:
        return None
    pct = round(100.0 * float(num) / float(denom), 2)
    return {
        "coverage_as_of": norm,
        "coverage_numerator": num,
        "coverage_denominator": denom,
        "coverage_pct": pct,
    }


def _append_freshness_row_for_collection(
    out: List[Dict[str, Any]],
    p: Dict[str, Any],
    fr: Dict[str, Any],
    *,
    database: str,
    collection: str,
    display_label: str,
) -> None:
    """Append one freshness row (max field + optional coverage) for a Mongo collection."""
    field = fr.get("field")
    if not field:
        return
    field_kind = fr.get("field_kind", "date_string")
    doc_id = fr.get("document_id")
    try:
        raw = _max_freshness_value(
            database,
            collection,
            field,
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
    row: Dict[str, Any] = {
        "pipeline_id": p.get("id"),
        "label": display_label,
        "database": database,
        "collection": collection,
        "field": field,
        "latest_value": iso,
        "raw_value": raw,
        "error": err,
    }
    cov_cfg = fr.get("coverage") or {}
    if raw is not None and not err and cov_cfg.get("universe_collection") and cov_cfg.get("match_key"):
        try:
            u_db = cov_cfg.get("universe_database") or database
            data_coll = _get_collection(database, collection)
            universe_coll = _get_collection(u_db, cov_cfg["universe_collection"])
            cov = _coverage_latest_ratio(
                data_coll,
                field,
                raw,
                universe_coll,
                str(cov_cfg["match_key"]),
            )
            if cov:
                row.update(cov)
        except Exception as cov_err:
            row["coverage_error"] = str(cov_err)
    out.append(row)


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
    """Latest pipeline_tasks (or job_status) per catalog pipeline."""
    catalog = load_pipelines_catalog()
    rows: List[Dict[str, Any]] = []
    for p in catalog:
        key = p.get("job_status_key") or p.get("id")
        label = p.get("label") or key
        doc = _latest_ingestion_doc_for_catalog_key(key)
        norm = normalize_ingestion_monitor_doc(doc)
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
    key_to_label = {p.get("job_status_key") or p.get("id"): p.get("label") or p.get("id") for p in catalog}
    keys = {p.get("job_status_key") or p.get("id") for p in catalog}
    jobs_out: List[Dict[str, Any]] = []
    healthy = 0
    for key in keys:
        doc = _latest_ingestion_doc_for_catalog_key(key)
        norm = normalize_ingestion_monitor_doc(doc)
        st = norm["status"]
        if st == "complete":
            healthy += 1
        jobs_out.append(
            {
                "type": key,
                "label": key_to_label.get(key, key),
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
        if not fr.get("field"):
            continue
        database = fr.get("database", "quant_data")
        sheets = fr.get("freshness_sheets")
        if isinstance(sheets, list) and len(sheets) > 0:
            for sheet in sheets:
                coll = sheet.get("collection")
                if not coll:
                    continue
                label = sheet.get("label") or str(coll)
                _append_freshness_row_for_collection(
                    out,
                    p,
                    fr,
                    database=database,
                    collection=str(coll),
                    display_label=str(label),
                )
            continue
        coll = fr.get("collection")
        if not coll:
            continue
        _append_freshness_row_for_collection(
            out,
            p,
            fr,
            database=database,
            collection=str(coll),
            display_label=str(p.get("label") or p.get("id") or coll),
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
    "normalize_ingestion_monitor_doc",
    "build_pipelines_status_rows",
    "build_sync_health_from_catalog",
    "build_freshness_rows",
    "build_data_monitoring_overview",
    "expected_latest_trade_date_hint",
]
