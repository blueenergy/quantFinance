"""Shared backtest batch creation, summary refresh, and result listing."""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from bson import ObjectId
from fastapi import HTTPException

from services.backtest_batch_schemas import BacktestBatchCreate, BacktestCombo

MAX_COMPARE_TASKS = 500

LEASE_RESET_FIELDS: Dict[str, Any] = {
    "lease_token": None,
    "lease_expires_at": None,
    "heartbeat_at": None,
}


def lease_field_defaults() -> Dict[str, Any]:
    return {
        "attempts": 0,
        "heartbeat_at": None,
        "lease_token": None,
        "lease_expires_at": None,
        "updated_at": None,
    }

RESULT_METRICS_PROJECTION = {"_id": 0, "task_id": 1, "metrics": 1}
TASK_ROW_PROJECTION = {
    "_id": 0,
    "task_id": 1,
    "symbol": 1,
    "asset_type": 1,
    "status": 1,
    "error_message": 1,
    "strategy_key": 1,
    "preset": 1,
    "strategy_params": 1,
    "combo_key": 1,
    "combo_index": 1,
    "combo_label": 1,
    "batch_index": 1,
}


def normalize_asset_type(asset_type: Optional[str]) -> str:
    value = (asset_type or "stock").strip().lower()
    if value not in {"stock", "etf"}:
        raise HTTPException(status_code=400, detail="asset_type must be 'stock' or 'etf'")
    return value


def infer_asset_type_from_symbol(symbol: str, preferred: str = "stock") -> str:
    """Infer stock vs ETF from ts_code; fixes 300196.SZ-style stocks mislabeled as ETF."""
    sym = str(symbol or "").strip().upper()
    pref = (preferred or "stock").strip().lower()
    if not sym:
        return "etf" if pref == "etf" else "stock"

    code, suffix = (sym.split(".", 1) + [""])[:2] if "." in sym else (sym, "")
    if not suffix:
        return "etf" if pref == "etf" else "stock"

    if suffix == "SZ":
        if code.startswith(("15", "16")):
            return "etf"
        if code.startswith(("000", "001", "002", "003", "300", "301")):
            return "stock"

    if suffix == "SH":
        if code.startswith(("51", "56", "58")):
            return "etf"
        if code.startswith(("600", "601", "603", "605", "688", "689")):
            return "stock"

    if suffix == "BJ" and code[:1] in {"4", "8", "9"}:
        return "stock"

    return "etf" if pref == "etf" else "stock"


def new_batch_id() -> str:
    return f"bb_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"


def new_task_id(symbol: str) -> str:
    clean_symbol = str(symbol or "symbol").replace(".", "_")
    return f"bt_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{clean_symbol}_{uuid.uuid4().hex[:6]}"


def normalize_symbol(symbol: str, asset_type: str) -> str:
    symbol = str(symbol or "").strip().upper()
    if not symbol:
        return ""
    if "." in symbol:
        return symbol
    if asset_type == "etf":
        if symbol.startswith(("15", "16")):
            return f"{symbol}.SZ"
        if symbol.startswith("5"):
            return f"{symbol}.SH"
        return symbol
    if symbol.startswith("6"):
        return f"{symbol}.SH"
    if symbol.startswith(("0", "3")):
        return f"{symbol}.SZ"
    if symbol.startswith(("8", "4")):
        return f"{symbol}.BJ"
    return symbol


def dedupe_symbols(symbols: List[str], asset_type: str) -> List[str]:
    out: List[str] = []
    seen = set()
    for raw in symbols or []:
        sym = normalize_symbol(raw, asset_type)
        if not sym or sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
    return out


def load_index_symbols(collections: Dict[str, Any], index_code: str) -> List[str]:
    coll = collections.get("index_constituents")
    code = str(index_code or "").strip()
    if coll is None or not code:
        return []
    latest = coll.find_one(
        {"index_code": code},
        {"_id": 0, "weight_trade_date": 1, "update_date": 1},
        sort=[("weight_trade_date", -1), ("update_date", -1)],
    )
    if not latest:
        return []
    query = {"index_code": code}
    if latest.get("weight_trade_date"):
        query["weight_trade_date"] = latest["weight_trade_date"]
    if latest.get("update_date"):
        query["update_date"] = latest["update_date"]
    return dedupe_symbols(
        [d.get("symbol") for d in coll.find(query, {"_id": 0, "symbol": 1}).sort("symbol", 1)],
        "stock",
    )


def resolve_batch_symbols(collections: Dict[str, Any], task: BacktestBatchCreate) -> List[str]:
    universe_type = (task.universe_type or "manual").strip().lower()
    asset_type = normalize_asset_type(task.asset_type)
    if universe_type == "manual":
        symbols = dedupe_symbols(task.symbols, asset_type)
    elif universe_type == "index":
        symbols = load_index_symbols(collections, task.universe_value or "")
    elif universe_type == "watchlist":
        watchlist_col = collections.get("user_watchlists")
        doc = (
            watchlist_col.find_one({"_id": task.universe_value})
            if watchlist_col is not None and task.universe_value
            else None
        )
        symbols = dedupe_symbols((doc or {}).get("symbols", []), asset_type)
    elif universe_type == "strategy_pool":
        pool_col = collections.get("strategy_stock_pool")
        query: Dict[str, Any] = {}
        if task.universe_value:
            query["preset"] = task.universe_value
        rows = (
            list(pool_col.find(query, {"_id": 0, "symbol": 1}).sort("date", -1).limit(2000))
            if pool_col is not None
            else []
        )
        symbols = dedupe_symbols([r.get("symbol") for r in rows], asset_type)
    else:
        raise HTTPException(
            status_code=400,
            detail="universe_type must be manual, index, watchlist, or strategy_pool",
        )
    if not symbols:
        raise HTTPException(status_code=400, detail="No symbols resolved for batch backtest")
    return symbols


def _normalize_param_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("strategy_params contains non-finite number")
        if value.is_integer():
            return int(value)
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, dict):
        return {str(k): _normalize_param_value(v) for k, v in sorted(value.items(), key=lambda x: x[0])}
    if isinstance(value, (list, tuple)):
        return [_normalize_param_value(v) for v in value]
    return value


def normalize_preset(preset: Optional[str]) -> str:
    return str(preset or "").strip()


def compute_combo_key(strategy_key: str, preset: Optional[str], strategy_params: Dict[str, Any]) -> str:
    payload = {
        "strategy_key": str(strategy_key or "").strip(),
        "preset": normalize_preset(preset),
        "strategy_params": _normalize_param_value(strategy_params or {}),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def resolve_combos(task: BacktestBatchCreate) -> List[BacktestCombo]:
    if task.combos is not None:
        return list(task.combos)
    return [
        BacktestCombo(
            strategy_key=task.strategy_key,
            preset=task.preset,
            strategy_params=dict(task.strategy_params or {}),
        )
    ]


def build_backtest_task_doc(
    *,
    current_user: dict,
    symbol: str,
    asset_type: str,
    strategy_key: str,
    preset: Optional[str],
    strategy_params: Dict[str, Any],
    start_date: str,
    end_date: str,
    initial_cash: float,
    batch_id: Optional[str] = None,
    batch_name: Optional[str] = None,
    batch_index: Optional[int] = None,
    combo_key: Optional[str] = None,
    combo_index: Optional[int] = None,
    combo_label: Optional[str] = None,
) -> Dict[str, Any]:
    task_doc = {
        "task_id": new_task_id(symbol),
        "user_id": current_user["id"],
        "symbol": symbol,
        "asset_type": asset_type,
        "strategy_key": strategy_key,
        "preset": preset,
        "strategy_params": strategy_params or {},
        "start_date": start_date,
        "end_date": end_date,
        "initial_cash": initial_cash,
        "status": "pending",
        "worker_id": None,
        "created_at": datetime.now(),
        "started_at": None,
        "completed_at": None,
        "error_message": None,
        **lease_field_defaults(),
    }
    if batch_id:
        task_doc["batch_id"] = batch_id
        task_doc["batch_name"] = batch_name
        task_doc["batch_index"] = batch_index
    if combo_key:
        task_doc["combo_key"] = combo_key
    if combo_index is not None:
        task_doc["combo_index"] = combo_index
    if combo_label:
        task_doc["combo_label"] = combo_label
    return task_doc


def _metric_value(metrics: Dict[str, Any], key: str) -> Optional[float]:
    value = metrics.get(key)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean_for_response(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, int):
        if -(2 ** 63) <= value <= (2 ** 63 - 1):
            return value
        return str(value)
    if isinstance(value, dict):
        return {str(k): _clean_for_response(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean_for_response(v) for v in value]
    if hasattr(value, "item"):
        try:
            return _clean_for_response(value.item())
        except Exception:
            pass
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    return str(value)


def batch_status_from_counts(total: int, counts: Dict[str, int]) -> str:
    if total <= 0:
        return "failed"
    completed = counts.get("completed", 0)
    failed = counts.get("failed", 0)
    cancelled = counts.get("cancelled", 0)
    running = counts.get("pending", 0) + counts.get("claimed", 0) + counts.get("running", 0)
    if running:
        return "running" if completed or failed or cancelled else "pending"
    if completed == total:
        return "completed"
    if completed > 0 and (failed or cancelled):
        return "partial_failed"
    if cancelled == total:
        return "cancelled"
    return "failed"


def summarize_batch(tasks: List[Dict[str, Any]], results_by_task: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    counts: Dict[str, int] = {}
    returns: List[float] = []
    drawdowns: List[float] = []
    win_rates: List[float] = []
    for task in tasks:
        status_value = str(task.get("status") or "unknown")
        counts[status_value] = counts.get(status_value, 0) + 1
        result = results_by_task.get(task.get("task_id"))
        metrics = result.get("metrics", {}) if isinstance(result, dict) else {}
        for target, key in ((returns, "total_return"), (drawdowns, "max_drawdown"), (win_rates, "win_rate")):
            val = _metric_value(metrics, key)
            if val is not None:
                target.append(val)

    def avg(values: List[float]) -> Optional[float]:
        return sum(values) / len(values) if values else None

    def median(values: List[float]) -> Optional[float]:
        if not values:
            return None
        ordered = sorted(values)
        mid = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[mid]
        return (ordered[mid - 1] + ordered[mid]) / 2

    total = len(tasks)
    return {
        "total": total,
        "status_counts": counts,
        "completed": counts.get("completed", 0),
        "failed": counts.get("failed", 0),
        "cancelled": counts.get("cancelled", 0),
        "running": counts.get("pending", 0) + counts.get("claimed", 0) + counts.get("running", 0),
        "avg_total_return": avg(returns),
        "median_total_return": median(returns),
        "avg_max_drawdown": avg(drawdowns),
        "median_win_rate": median(win_rates),
        "status": batch_status_from_counts(total, counts),
    }


def refresh_batch_summary(collections: Dict[str, Any], batch_id: str, user_id: str) -> Dict[str, Any]:
    tasks = list(
        collections["backtest_tasks"].find(
            {"batch_id": batch_id, "user_id": user_id},
            TASK_ROW_PROJECTION,
        )
    )
    task_ids = [t["task_id"] for t in tasks]
    results = (
        list(
            collections["backtest_results"].find(
                {"task_id": {"$in": task_ids}},
                RESULT_METRICS_PROJECTION,
            )
        )
        if task_ids
        else []
    )
    results_by_task = {r.get("task_id"): r for r in results}
    summary = summarize_batch(tasks, results_by_task)
    collections["backtest_batches"].update_one(
        {"batch_id": batch_id, "user_id": user_id},
        {"$set": {"summary": summary, "status": summary["status"], "updated_at": datetime.now()}},
    )
    return summary


def serialize_batch(batch: Dict[str, Any], summary: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    out = {k: v for k, v in batch.items() if k != "_id"}
    out["id"] = str(batch.get("_id")) if batch.get("_id") else None
    if summary is not None:
        out["summary"] = summary
        out["status"] = summary.get("status", out.get("status"))
    return _clean_for_response(out)


def lookup_symbol_name(collections: Dict[str, Any], symbol: Optional[str], asset_type: str) -> Optional[str]:
    if not symbol:
        return None
    if asset_type == "etf":
        etf_basic_col = collections.get("etf_basic")
        if etf_basic_col is None:
            return None
        info = etf_basic_col.find_one({"ts_code": symbol})
    else:
        stock_info_col = collections.get("stock_info")
        if stock_info_col is None:
            return None
        info = stock_info_col.find_one({"symbol": symbol})
    if info and isinstance(info, dict):
        name = info.get("name")
        return name if isinstance(name, str) and name else None
    return None


def is_compare_batch(batch: Dict[str, Any]) -> bool:
    return str(batch.get("experiment_type") or "scan").lower() == "compare"


def create_batch(
    collections: Dict[str, Any],
    current_user: dict,
    task: BacktestBatchCreate,
    *,
    extra_batch_fields: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    backtest_batches_col = collections["backtest_batches"]
    backtest_tasks_col = collections["backtest_tasks"]
    asset_type = normalize_asset_type(task.asset_type)
    symbols = resolve_batch_symbols(collections, task)
    if task.limit_symbols and task.limit_symbols > 0:
        symbols = symbols[: task.limit_symbols]
    if symbols:
        asset_type = infer_asset_type_from_symbol(symbols[0], asset_type)

    combos = resolve_combos(task)
    combo_keys_seen: set[str] = set()
    combo_entries: List[Tuple[BacktestCombo, str]] = []
    for combo in combos:
        key = compute_combo_key(combo.strategy_key, combo.preset, combo.strategy_params or {})
        if key in combo_keys_seen:
            raise HTTPException(status_code=422, detail=f"Duplicate combo_key in request: {key}")
        combo_keys_seen.add(key)
        combo_entries.append((combo, key))

    total_tasks = len(symbols) * len(combo_entries)
    if task.combos is not None and total_tasks > MAX_COMPARE_TASKS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Too many tasks: {len(symbols)} symbols × {len(combo_entries)} combos = "
                f"{total_tasks} (max {MAX_COMPARE_TASKS})"
            ),
        )

    batch_id = new_batch_id()
    primary = combo_entries[0][0]
    name = task.name or f"{primary.strategy_key} batch {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    now = datetime.now()

    combos_doc = [
        {
            "strategy_key": combo.strategy_key,
            "preset": combo.preset,
            "strategy_params": combo.strategy_params or {},
            "label": combo.label,
            "combo_key": combo_key,
        }
        for combo, combo_key in combo_entries
    ]

    batch_doc: Dict[str, Any] = {
        "batch_id": batch_id,
        "user_id": current_user["id"],
        "name": name,
        "universe_type": (task.universe_type or "manual").strip().lower(),
        "universe_value": task.universe_value,
        "symbols_count": len(symbols),
        "limit_symbols": task.limit_symbols,
        "symbols_preview": symbols[:20],
        "asset_type": asset_type,
        "strategy_key": primary.strategy_key,
        "preset": primary.preset,
        "strategy_params": primary.strategy_params or {},
        "start_date": task.start_date,
        "end_date": task.end_date,
        "initial_cash": task.initial_cash,
        "status": "pending",
        "summary": {"total": total_tasks, "status_counts": {"pending": total_tasks}},
        "created_at": now,
        "updated_at": now,
        "completed_at": None,
        "experiment_type": task.experiment_type,
        "combos_count": len(combo_entries),
    }
    if task.combos is not None:
        batch_doc["combos"] = combos_doc

    if extra_batch_fields:
        batch_doc.update(extra_batch_fields)

    child_tasks: List[Dict[str, Any]] = []
    batch_index = 0
    for symbol in symbols:
        for combo_index, (combo, combo_key) in enumerate(combo_entries):
            child_tasks.append(
                build_backtest_task_doc(
                    current_user=current_user,
                    symbol=symbol,
                    asset_type=asset_type,
                    strategy_key=combo.strategy_key,
                    preset=combo.preset,
                    strategy_params=combo.strategy_params or {},
                    start_date=task.start_date,
                    end_date=task.end_date,
                    initial_cash=task.initial_cash,
                    batch_id=batch_id,
                    batch_name=name,
                    batch_index=batch_index,
                    combo_key=combo_key,
                    combo_index=combo_index,
                    combo_label=combo.label,
                )
            )
            batch_index += 1

    backtest_batches_col.insert_one(batch_doc)
    if child_tasks:
        backtest_tasks_col.insert_many(child_tasks)

    import logging
    logging.getLogger(__name__).info(
        "create_batch %s experiment_type=%s combos=%s symbols=%s created_tasks=%s",
        batch_id,
        task.experiment_type,
        len(combo_entries),
        len(symbols),
        len(child_tasks),
    )

    return {
        **serialize_batch(batch_doc),
        "created_tasks": len(child_tasks),
        "task_ids": [t["task_id"] for t in child_tasks],
    }


def build_experiment_type_query(experiment_type: Optional[str]) -> Optional[Dict[str, Any]]:
    if not experiment_type:
        return None
    value = str(experiment_type).strip().lower()
    if value == "compare":
        return {"experiment_type": "compare"}
    if value == "scan":
        return {
            "$or": [
                {"experiment_type": "scan"},
                {"experiment_type": {"$exists": False}},
                {"experiment_type": None},
            ]
        }
    raise HTTPException(status_code=400, detail="experiment_type must be scan or compare")


def list_batch_result_rows(
    collections: Dict[str, Any],
    batch_id: str,
    user_id: str,
    *,
    status: Optional[str] = None,
    sort_by: str = "total_return",
    order: str = "desc",
    limit: int = 200,
    offset: int = 0,
) -> Dict[str, Any]:
    batch = collections["backtest_batches"].find_one({"batch_id": batch_id, "user_id": user_id})
    if not batch:
        raise HTTPException(status_code=404, detail="Backtest batch not found")

    query: Dict[str, Any] = {"batch_id": batch_id, "user_id": user_id}
    if status:
        query["status"] = status
    tasks = list(collections["backtest_tasks"].find(query, TASK_ROW_PROJECTION).sort("batch_index", 1))
    task_ids = [t["task_id"] for t in tasks]
    results = (
        list(
            collections["backtest_results"].find(
                {"task_id": {"$in": task_ids}},
                RESULT_METRICS_PROJECTION,
            )
        )
        if task_ids
        else []
    )
    results_by_task = {r.get("task_id"): r for r in results}

    rows: List[Dict[str, Any]] = []
    for task in tasks:
        result = results_by_task.get(task["task_id"]) or {}
        metrics = result.get("metrics", {}) or {}
        asset_type = normalize_asset_type(task.get("asset_type"))
        rows.append(
            {
                "task_id": task["task_id"],
                "batch_id": batch_id,
                "symbol": task.get("symbol"),
                "asset_type": asset_type,
                "stock_name": lookup_symbol_name(collections, task.get("symbol"), asset_type),
                "status": task.get("status"),
                "error_message": task.get("error_message"),
                "strategy_key": task.get("strategy_key"),
                "preset": task.get("preset"),
                "strategy_params": task.get("strategy_params") or {},
                "combo_key": task.get("combo_key"),
                "combo_index": task.get("combo_index"),
                "combo_label": task.get("combo_label"),
                "metrics": metrics,
                "total_return": metrics.get("total_return"),
                "invested_return": metrics.get("invested_return"),
                "invested_cash": metrics.get("invested_cash"),
                "capital_utilization": metrics.get("capital_utilization"),
                "max_drawdown": metrics.get("max_drawdown"),
                "sharpe_ratio": metrics.get("sharpe_ratio"),
                "win_rate": metrics.get("win_rate"),
                "total_trades": metrics.get("total_trades"),
            }
        )

    reverse = order.lower() != "asc"
    present = [r for r in rows if r.get(sort_by) is not None]
    missing = [r for r in rows if r.get(sort_by) is None]
    present.sort(key=lambda r: r.get(sort_by), reverse=reverse)
    rows = present + missing
    total = len(rows)
    return _clean_for_response(
        {
            "batch_id": batch_id,
            "total": total,
            "offset": offset,
            "limit": limit,
            "rows": rows[offset:offset + limit],
        }
    )


def list_all_batch_result_rows(
    collections: Dict[str, Any],
    batch_id: str,
    user_id: str,
    *,
    status: Optional[str] = None,
    sort_by: str = "total_return",
    order: str = "desc",
    page_size: int = 500,
) -> List[Dict[str, Any]]:
    """Fetch all result rows by paging through list_batch_result_rows."""
    all_rows: List[Dict[str, Any]] = []
    offset = 0
    while True:
        payload = list_batch_result_rows(
            collections,
            batch_id,
            user_id,
            status=status,
            sort_by=sort_by,
            order=order,
            limit=page_size,
            offset=offset,
        )
        rows = payload.get("rows") or []
        all_rows.extend(rows)
        total = int(payload.get("total") or 0)
        offset += len(rows)
        if offset >= total or not rows:
            break
    return all_rows
