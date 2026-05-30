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
from datetime import timedelta
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


def build_pipeline_dag(date: Optional[str] = None) -> Dict[str, Any]:
    """Return DAG graph structure with today's run status for each node.

    Response shape::

        {
          "nodes": [
            {
              "id": "stock_daily",
              "label": "股票日线",
              "group": "layer0",
              "run_mode": "cron",
              "trigger_on_complete": false,
              "schedule_hint": "...",
              "status": "complete" | "failed" | "no_record" | "skipped" | ...,
              "date": "20260512",
              "completed_at": "2026-05-12T17:45:00+08:00",
              "records_changed": 5200,
              "error": null
            },
            ...
          ],
          "edges": [
            {"source": "stock_daily", "target": "market_sentiment"},
            ...
          ],
          "query_date": "20260512"
        }
    """
    from datetime import timezone as _tz

    BEIJING = _tz(timedelta(hours=8))

    def _to_bj_iso(dt: Optional[datetime]) -> Optional[str]:
        if not isinstance(dt, datetime):
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(BEIJING).isoformat()

    catalog = load_pipelines_catalog()
    query_date = date or datetime.now(BEIJING).strftime("%Y%m%d")

    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, str]] = []

    for entry in catalog:
        pid = entry.get("id", "")
        key = entry.get("job_status_key") or pid

        # Fetch latest run doc for this pipeline
        raw = _latest_ingestion_doc_for_catalog_key(key)
        run = normalize_ingestion_monitor_doc(raw)

        # Determine if the latest run matches the query date
        run_date_int = _ingestion_date_ymd_int(raw.get("date") if raw else None)
        query_date_int = _ingestion_date_ymd_int(query_date)
        if run_date_int != query_date_int:
            # No record for the queried date
            status = "no_record"
            run_date_str = None
            completed_at = None
            records_changed = None
            error = None
        else:
            status = run.get("status") or "no_record"
            run_date_str = str(raw.get("date")) if raw else None
            completed_at = _to_bj_iso(run.get("completed_at"))
            records_changed = run.get("records_changed")
            error = run.get("error")

        nodes.append({
            "id": pid,
            "label": entry.get("label", pid),
            "group": entry.get("group", "layer0"),
            "run_mode": entry.get("run_mode", "cron"),
            "trigger_on_complete": bool(entry.get("trigger_on_complete", False)),
            "depends_on": list(entry.get("depends_on") or []),
            "schedule_hint": entry.get("schedule_hint"),
            "status": status,
            "date": run_date_str,
            "completed_at": completed_at,
            "records_changed": records_changed,
            "error": error,
        })

        # Build edges from depends_on
        for dep in (entry.get("depends_on") or []):
            edges.append({"source": dep, "target": pid})

    return {
        "nodes": nodes,
        "edges": edges,
        "query_date": query_date,
    }


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


def _period_lte_filter(field: str, norm: str) -> Dict[str, Any]:
    """Match documents whose YYYYMMDD-like field is on or before ``norm``."""
    if not field or not norm or len(norm) != 8 or not norm.isdigit():
        return {}
    try:
        as_int = int(norm)
    except ValueError:
        return {field: {"$lte": norm}}
    return {
        "$or": [
            {field: {"$lte": norm}},
            {field: {"$lte": as_int}},
        ]
    }


def _universe_period_filter_query(period_filter: Optional[Dict[str, Any]], norm: str) -> Dict[str, Any]:
    """Build optional period-aware universe filters from the data catalog."""
    if not period_filter:
        return {}
    op = period_filter.get("op")
    field = period_filter.get("field")
    if op == "lte_period" and field:
        return _period_lte_filter(str(field), norm)
    return {}


def _merge_match_filters(*filters: Dict[str, Any]) -> Dict[str, Any]:
    """Combine simple Mongo match filters without losing top-level ``$or`` clauses."""
    parts = [f for f in filters if f]
    if not parts:
        return {}
    if len(parts) == 1:
        return parts[0]
    return {"$and": parts}


def _universe_base_query(match_key: str, norm: str, period_filter: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return _merge_match_filters(
        {match_key: {"$exists": True, "$nin": [None, ""]}},
        _universe_period_filter_query(period_filter, norm),
    )


def _coverage_latest_ratio(
    data_coll,
    field: str,
    latest_raw: Any,
    universe_coll,
    match_key: str,
    universe_period_filter: Optional[Dict[str, Any]] = None,
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
    universe_query = _universe_base_query(match_key, norm, universe_period_filter)
    try:
        denom = int(universe_coll.count_documents(universe_query))
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
                        {"$match": universe_query},
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
                cov_cfg.get("universe_period_filter"),
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


def _iter_coverage_targets() -> List[Dict[str, Any]]:
    """Catalog entries that define freshness + coverage (per data collection)."""
    out: List[Dict[str, Any]] = []
    for p in load_pipelines_catalog():
        fr = p.get("freshness") or {}
        cov = fr.get("coverage") or {}
        if not cov.get("universe_collection") or not cov.get("match_key"):
            continue
        field = fr.get("field")
        if not field:
            continue
        database = fr.get("database", "quant_data")
        u_db = cov.get("universe_database") or database
        match_key = str(cov["match_key"])
        universe_collection = str(cov["universe_collection"])
        universe_period_filter = cov.get("universe_period_filter")
        field_kind = fr.get("field_kind", "date_string")
        sheets = fr.get("freshness_sheets")
        if isinstance(sheets, list) and len(sheets) > 0:
            for sheet in sheets:
                c = sheet.get("collection")
                if not c:
                    continue
                out.append(
                    {
                        "database": database,
                        "collection": str(c),
                        "field": field,
                        "field_kind": field_kind,
                        "universe_database": u_db,
                        "universe_collection": universe_collection,
                        "match_key": match_key,
                        "universe_period_filter": universe_period_filter,
                    }
                )
        elif fr.get("collection"):
            out.append(
                {
                    "database": database,
                    "collection": str(fr["collection"]),
                    "field": field,
                    "field_kind": field_kind,
                    "universe_database": u_db,
                    "universe_collection": universe_collection,
                    "match_key": match_key,
                    "universe_period_filter": universe_period_filter,
                }
            )
    return out


def resolve_coverage_gap_config(database: str, collection: str) -> Optional[Dict[str, Any]]:
    dbn, coln = (database or "").strip(), (collection or "").strip()
    for t in _iter_coverage_targets():
        if t["database"] == dbn and t["collection"] == coln:
            return t
    return None


def _expr_date_equals_field(field: str, norm: str) -> Dict[str, Any]:
    """Mongo $expr fragment: field matches norm (string or int YYYYMMDD)."""
    try:
        as_int = int(norm)
    except ValueError:
        return {"$eq": [f"${field}", norm]}
    return {"$or": [{"$eq": [f"${field}", norm]}, {"$eq": [f"${field}", as_int]}]}


def query_coverage_gaps(
    *,
    database: str,
    collection: str,
    period: Optional[str] = None,
    skip: int = 0,
    limit: int = 200,
) -> Dict[str, Any]:
    """
    List universe stocks that have no row in ``collection`` at report period ``norm``.

    ``database`` / ``collection`` must appear in the pipelines catalog with a ``coverage``
    block (same allowlist as freshness coverage). ``period`` is optional YYYYMMDD; if
    omitted, uses global max of the freshness ``field`` in that collection.
    """
    cfg = resolve_coverage_gap_config(database, collection)
    if not cfg:
        raise ValueError("unknown_coverage_target")

    limit = max(1, min(int(limit), 2000))
    skip = max(0, int(skip))

    field = cfg["field"]
    field_kind = cfg.get("field_kind", "date_string")
    match_key = cfg["match_key"]
    u_db = cfg["universe_database"]
    u_coll_name = cfg["universe_collection"]

    data_coll = _get_collection(cfg["database"], cfg["collection"])
    universe_coll = _get_collection(u_db, u_coll_name)

    if period and str(period).strip():
        raw_for_cov: Any = str(period).strip().replace("-", "")[:8]
        norm = _normalize_yyyymmdd(raw_for_cov)
    else:
        raw_for_cov = _max_freshness_value(
            cfg["database"],
            cfg["collection"],
            field,
            field_kind,
            document_id=None,
        )
        norm = _normalize_yyyymmdd(raw_for_cov)

    if not norm:
        raise ValueError("invalid_or_missing_period")

    cov = _coverage_latest_ratio(
        data_coll,
        field,
        raw_for_cov,
        universe_coll,
        match_key,
        cfg.get("universe_period_filter"),
    )
    if not cov:
        raise ValueError("coverage_unavailable")

    date_expr = _expr_date_equals_field(field, norm)
    mk = match_key
    universe_query = _universe_base_query(mk, norm, cfg.get("universe_period_filter"))
    lookup_pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "$expr": {
                    "$and": [
                        {"$eq": [f"${mk}", "$$mkv"]},
                        date_expr,
                    ]
                }
            }
        },
        {"$limit": 1},
    ]

    pipeline: List[Dict[str, Any]] = [
        {"$match": universe_query},
        {
            "$lookup": {
                "from": cfg["collection"],
                "let": {"mkv": f"${mk}"},
                "pipeline": lookup_pipeline,
                "as": "hit",
            }
        },
        {"$match": {"hit": {"$eq": []}}},
        {
            "$facet": {
                "items": [
                    {"$skip": skip},
                    {"$limit": limit},
                    {"$project": {"_id": 0, mk: 1, "name": 1}},
                ],
                "cnt": [{"$count": "n"}],
            }
        },
    ]

    agg = list(universe_coll.aggregate(pipeline))
    block = agg[0] if agg else {}
    raw_items = block.get("items") or []
    cnt_list = block.get("cnt") or []
    missing_total = int(cnt_list[0]["n"]) if cnt_list else 0

    items: List[Dict[str, Any]] = []
    for doc in raw_items:
        code = doc.get(mk)
        if code is None:
            continue
        items.append({"ts_code": str(code), "name": doc.get("name")})

    out: Dict[str, Any] = {
        "database": cfg["database"],
        "collection": cfg["collection"],
        "field": field,
        "period": norm,
        "universe_collection": u_coll_name,
        "missing_total": missing_total,
        "items": items,
        "skip": skip,
        "limit": limit,
    }
    out.update(cov)
    return out


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
    "query_coverage_gaps",
    "resolve_coverage_gap_config",
]
