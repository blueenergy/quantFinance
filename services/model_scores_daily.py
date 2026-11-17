"""Daily model-score panel builder (read-only, quant_ml.model_predictions).

Scores from different models are not on a shared scale (ridge vs LightGBM),
so everything user-facing is rank / percentile. Comparison is only defined
inside one index universe on one trade date; cross-universe requests are
rejected rather than silently producing unaligned rows.
"""

from __future__ import annotations

import math
from datetime import date as _date
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

MAX_MODELS = 6
DEFAULT_TOP_N = 20
DEFAULT_LIMIT = 300
MAX_LIMIT = 1000
RECENT_DATES = 30
RECENT_DAYS = 30


def normalize_yyyymmdd(value: Optional[str]) -> Optional[str]:
    if value is None or value == "":
        return None
    text = str(value).replace("-", "").replace("/", "")[:8]
    if len(text) != 8 or not text.isdigit():
        return None
    try:
        datetime.strptime(text, "%Y%m%d")
    except ValueError:
        return None
    return text


def normalize_model_ids(raw: Any) -> List[str]:
    """Accept ``"a,b"`` or a list; dedupe and enforce the model cap."""
    if isinstance(raw, str):
        values = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, Iterable):
        values = [str(part).strip() for part in raw]
    else:
        values = []
    model_ids = [value for value in dict.fromkeys(values) if value]
    if len(model_ids) > MAX_MODELS:
        raise ValueError(f"at most {MAX_MODELS} models can be compared at once")
    return model_ids


def _iso(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value is not None else None


def _round(value: Optional[float], digits: int = 6) -> Optional[float]:
    if value is None or value != value:
        return None
    return round(float(value), digits)


def _shadow_enabled_models(shadow_profiles) -> set:
    """model ids the shadow pipeline currently produces daily."""
    if shadow_profiles is None:
        return set()
    out = set()
    try:
        docs = shadow_profiles.find(
            {"enabled": {"$ne": False}}, {"_id": 0, "model_id": 1, "params": 1}
        )
        for doc in docs:
            model_id = doc.get("model_id") or (doc.get("params") or {}).get("model_id")
            if model_id:
                out.add(str(model_id))
    except Exception:
        return set()
    return out


def _live_models(activations) -> set:
    """model ids referenced by an active strategy activation."""
    if activations is None:
        return set()
    out = set()
    try:
        docs = activations.find({"status": "active"}, {"_id": 0, "params_override": 1})
        for doc in docs:
            model_id = (doc.get("params_override") or {}).get("model_id")
            if model_id:
                out.add(str(model_id))
    except Exception:
        return set()
    return out


def accepted_model_reasons(shadow_profiles=None, activations=None) -> Dict[str, List[str]]:
    """Model ids the system currently runs, mapped to ``live`` / ``shadow``.

    This is the "platform recognises it" definition used by both the daily
    model-score catalog and the portfolio-research model picker. A model can
    be both live and shadow; order is deterministic (``live`` first).
    """
    reasons: Dict[str, List[str]] = {}
    for model_id in sorted(_live_models(activations)):
        reasons.setdefault(model_id, []).append("live")
    for model_id in sorted(_shadow_enabled_models(shadow_profiles)):
        reasons.setdefault(model_id, []).append("shadow")
    return reasons


def list_model_catalog(
    predictions,
    *,
    shadow_profiles=None,
    activations=None,
    registry=None,
    recent_days: int = RECENT_DAYS,
) -> List[Dict[str, Any]]:
    """Every ``model_id`` in ``model_predictions`` with its coverage.

    ``accepted`` marks a model the system actually runs: an active strategy
    activation (live) or an enabled shadow profile. The registry in
    ``quant_ml.models`` is the source of truth when available; the profile /
    activation derivation remains as a fallback for deployments that have not
    run the Phase 0 backfill. Everything else is a historical research model;
    the UI hides those by default because 19 choices are worse than 2 when
    most of them are retired.

    The count/coverage reads are N+1 by ``model_id``, which the compound
    index (model_id, trade_date, ts_code) serves as a prefix scan.
    """
    cutoff = (_date.today() - timedelta(days=int(recent_days))).strftime("%Y%m%d")
    accepted = accepted_model_reasons(shadow_profiles, activations)
    registry_by_model: Dict[str, Dict[str, Any]] = {}
    if registry is not None:
        try:
            registry_by_model = {
                str(doc.get("model_id") or doc.get("_id")): doc
                for doc in registry.find(
                    {},
                    {
                        "model_id": 1,
                        "state": 1,
                        "accepted": 1,
                        "accepted_reasons": 1,
                        "latest_validation": 1,
                    },
                )
                if doc.get("model_id") or doc.get("_id")
            }
        except Exception:
            registry_by_model = {}
    rows: List[Dict[str, Any]] = []
    for model_id in sorted(predictions.distinct("model_id") or []):
        if not model_id:
            continue
        first = predictions.find_one(
            {"model_id": model_id},
            {"_id": 0, "trade_date": 1, "index_code": 1, "research": 1, "shadow": 1, "source": 1},
            sort=[("trade_date", 1)],
        )
        last = predictions.find_one(
            {"model_id": model_id},
            {"_id": 0, "trade_date": 1, "index_code": 1, "research": 1, "shadow": 1, "source": 1},
            sort=[("trade_date", -1)],
        )
        if not first or not last:
            continue
        dates = sorted(predictions.distinct("trade_date", {"model_id": model_id}) or [])
        registry_doc = registry_by_model.get(str(model_id))
        if registry_doc:
            reasons = list(registry_doc.get("accepted_reasons") or [])
            accepted_flag = bool(registry_doc.get("accepted")) or bool(reasons)
            state = registry_doc.get("state")
        else:
            reasons = accepted.get(model_id, [])
            accepted_flag = bool(reasons)
            state = "live" if "live" in reasons else "shadow" if "shadow" in reasons else None
        validation = (registry_doc or {}).get("latest_validation") or {}
        rows.append(
            {
                "accepted": accepted_flag,
                "accepted_reasons": reasons,
                "model_id": model_id,
                "index_code": (last or {}).get("index_code") or (first or {}).get("index_code"),
                "first_trade_date": dates[0] if dates else _iso(first.get("trade_date")),
                "last_trade_date": dates[-1] if dates else _iso(last.get("trade_date")),
                "date_count": len(dates),
                "prediction_count": predictions.count_documents({"model_id": model_id}),
                "research": bool((last or {}).get("research")),
                "shadow": bool((last or {}).get("shadow")),
                "source": (last or {}).get("source"),
                "state": state,
                "validated": state == "validated",
                "validation": (
                    {"protocol": validation.get("protocol"), "verdict": validation.get("verdict")}
                    if validation
                    else None
                ),
                "recent": bool(dates and dates[-1] >= cutoff),
                "recent_trade_dates": list(reversed(dates[-RECENT_DATES:])),
            }
        )
    # Recent first, then newest coverage, then id. Three stable sorts instead
    # of one mixed-direction tuple.
    rows.sort(key=lambda row: row["model_id"])
    rows.sort(key=lambda row: row["last_trade_date"] or "", reverse=True)
    rows.sort(key=lambda row: not row["recent"])
    return rows


def resolve_trade_date(predictions, model_ids: Sequence[str]) -> Optional[str]:
    """Latest date of the first requested model that has any predictions.

    Anchoring on the first model (the live one by default) beats a cross-model
    intersection: models retired years apart share a stale intersection, and
    the UI greys out models that have no rows on the anchored date instead.
    """
    for model_id in model_ids:
        dates = predictions.distinct("trade_date", {"model_id": model_id}) or []
        if dates:
            return max(str(value) for value in dates)
    return None


def _score_rows(predictions, model_id: str, trade_date: str) -> List[Dict[str, Any]]:
    docs = list(
        predictions.find(
            {"model_id": model_id, "trade_date": trade_date},
            {"_id": 0, "ts_code": 1, "score": 1, "created_at": 1, "index_code": 1},
        )
    )
    merged: Dict[str, Dict[str, Any]] = {}
    for doc in docs:
        symbol = str(doc.get("ts_code") or "").strip()
        score = doc.get("score")
        if not symbol or score is None or score != score:
            continue
        current = merged.get(symbol)
        if current is None or (doc.get("created_at") or datetime.min) > (current.get("created_at") or datetime.min):
            merged[symbol] = doc
    return [
        {"symbol": symbol, "score": float(doc["score"]), "created_at": doc.get("created_at")}
        for symbol, doc in merged.items()
    ]


def _rank(rows: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
    """Competition ranks (ties share the better rank) + top-percentile."""
    ordered = sorted(rows, key=lambda row: (-row["score"], row["symbol"]))
    n = len(ordered)
    out: Dict[str, Dict[str, float]] = {}
    previous_score: Optional[float] = None
    previous_rank = 0
    for index, row in enumerate(ordered):
        rank = previous_rank if previous_score == row["score"] else index + 1
        previous_score, previous_rank = row["score"], rank
        pct = round(100.0 * (n - rank + 1) / n, 2) if n else None
        out[row["symbol"]] = {"rank": rank, "pct": pct, "score": row["score"]}
    return out


def _recent_created_at(rows: Sequence[Dict[str, Any]]) -> Optional[str]:
    stamps = [row["created_at"] for row in rows if row.get("created_at") is not None]
    return _iso(max(stamps)) if stamps else None


def _previous_trade_date(predictions, model_id: str, trade_date: str) -> Optional[str]:
    doc = predictions.find_one(
        {"model_id": model_id, "trade_date": {"$lt": trade_date}},
        {"_id": 0, "trade_date": 1},
        sort=[("trade_date", -1)],
    )
    value = (doc or {}).get("trade_date")
    return str(value) if value else None


def _spearman(left: Dict[str, Dict[str, float]], right: Dict[str, Dict[str, float]], symbols: Sequence[str]) -> Optional[float]:
    n = len(symbols)
    if n < 3:
        return None
    mean = (n + 1) / 2.0
    num = 0.0
    den_left = 0.0
    den_right = 0.0
    for symbol in symbols:
        a = left[symbol]["rank"] - mean
        b = right[symbol]["rank"] - mean
        num += a * b
        den_left += a * a
        den_right += b * b
    if den_left <= 0 or den_right <= 0:
        return None
    return round(num / math.sqrt(den_left * den_right), 4)


def _stats(scores: Sequence[float]) -> Dict[str, Any]:
    values = sorted(scores)
    n = len(values)
    if not n:
        return {"count": 0, "mean": None, "std": None, "p50": None, "p90": None}

    def _quantile(fraction: float) -> float:
        position = (n - 1) * fraction
        lower = int(math.floor(position))
        upper = int(math.ceil(position))
        if lower == upper:
            return values[lower]
        return values[lower] + (values[upper] - values[lower]) * (position - lower)

    mean = sum(values) / n
    std = math.sqrt(sum((value - mean) ** 2 for value in values) / n) if n > 1 else 0.0
    return {
        "count": n,
        "mean": _round(mean),
        "std": _round(std),
        "p50": _round(_quantile(0.5)),
        "p90": _round(_quantile(0.9)),
    }


def build_daily_model_scores(
    predictions,
    stock_info,
    *,
    model_ids: Sequence[str],
    trade_date: Optional[str] = None,
    top_n: int = DEFAULT_TOP_N,
    limit: int = DEFAULT_LIMIT,
    watchlist: Optional[Sequence[str]] = None,
    positions: Optional[Sequence[str]] = None,
    include_prev: bool = True,
) -> Dict[str, Any]:
    """Rank panel for one trade date, optionally across several models.

    Returns ``models`` (coverage + freshness), ``rows`` (union of ranked
    symbols with per-model rank), ``correlation`` and ``disagreements``.
    """
    model_ids = list(model_ids)
    if not model_ids:
        raise ValueError("model_ids is required")
    if len(model_ids) > MAX_MODELS:
        raise ValueError(f"at most {MAX_MODELS} models can be compared at once")
    top_n = max(1, min(int(top_n or DEFAULT_TOP_N), 200))
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))

    resolved_date = normalize_yyyymmdd(trade_date) or resolve_trade_date(predictions, model_ids)
    if not resolved_date:
        raise ValueError("no trade_date given and the requested models share no dates")

    models: List[Dict[str, Any]] = []
    ranks: Dict[str, Dict[str, Dict[str, float]]] = {}
    prev_ranks: Dict[str, Dict[str, Dict[str, float]]] = {}
    universes: List[str] = []
    for model_id in model_ids:
        rows = _score_rows(predictions, model_id, resolved_date)
        meta = predictions.find_one({"model_id": model_id}, {"_id": 0, "index_code": 1})
        universe = (meta or {}).get("index_code")
        if universe:
            universes.append(str(universe))
        ranks[model_id] = _rank(rows)
        prev_date = _previous_trade_date(predictions, model_id, resolved_date) if include_prev else None
        if prev_date:
            prev_ranks[model_id] = _rank(_score_rows(predictions, model_id, prev_date))
        models.append(
            {
                "model_id": model_id,
                "index_code": universe,
                "rows": len(rows),
                "available": bool(rows),
                "created_at": _recent_created_at(rows),
                "prev_trade_date": prev_date,
                "stats": _stats([row["score"] for row in rows]),
            }
        )

    if len(set(universes)) > 1:
        raise ValueError(
            "models span several index universes "
            f"({', '.join(sorted(set(universes)))}); compare models from one universe only"
        )

    available = [model["model_id"] for model in models if model["available"]]
    if not available:
        raise ValueError(f"no model_predictions for {resolved_date} on any requested model")

    symbols = sorted({symbol for model_id in available for symbol in ranks[model_id]})
    names = _stock_metadata(stock_info, symbols)
    watch_set = {str(value) for value in (watchlist or [])}
    position_set = {str(value) for value in (positions or [])}

    table_rows: List[Dict[str, Any]] = []
    for symbol in symbols:
        model_values: Dict[str, Dict[str, Any]] = {}
        rank_values: List[int] = []
        for model_id in available:
            value = ranks[model_id].get(symbol)
            if value is None:
                continue
            entry: Dict[str, Any] = {
                "rank": value["rank"],
                "pct": value["pct"],
                "score": _round(value["score"]),
            }
            prev = prev_ranks.get(model_id, {}).get(symbol)
            entry["delta_rank"] = (prev["rank"] - value["rank"]) if prev else None
            model_values[model_id] = entry
            rank_values.append(value["rank"])
        meta = names.get(symbol, {})
        table_rows.append(
            {
                "symbol": symbol,
                "name": meta.get("name"),
                "industry": meta.get("industry"),
                "in_watchlist": symbol in watch_set or symbol.split(".")[0] in watch_set,
                "in_positions": symbol in position_set or symbol.split(".")[0] in position_set,
                "avg_rank": round(sum(rank_values) / len(rank_values), 2) if rank_values else None,
                "best_rank": min(rank_values) if rank_values else None,
                "models": model_values,
            }
        )
    table_rows.sort(key=lambda row: (row["avg_rank"] is None, row["avg_rank"] or 0, row["symbol"]))
    rows_out = table_rows[:limit]

    correlation: List[Dict[str, Any]] = []
    disagreements: List[Dict[str, Any]] = []
    for left_index, left_id in enumerate(available):
        for right_id in available[left_index + 1:]:
            common = sorted(set(ranks[left_id]) & set(ranks[right_id]))
            left_top = {symbol for symbol, value in ranks[left_id].items() if value["rank"] <= top_n}
            right_top = {symbol for symbol, value in ranks[right_id].items() if value["rank"] <= top_n}
            correlation.append(
                {
                    "left": left_id,
                    "right": right_id,
                    "common_symbols": len(common),
                    "spearman": _spearman(ranks[left_id], ranks[right_id], common),
                    "top_n": top_n,
                    "top_n_overlap": len(left_top & right_top),
                    "top_n_jaccard": round(len(left_top & right_top) / len(left_top | right_top), 4)
                    if (left_top | right_top)
                    else None,
                }
            )
            disagreements.append(
                {
                    "left": left_id,
                    "right": right_id,
                    "top_n": top_n,
                    "only_left": _named(names, sorted(left_top - right_top)),
                    "only_right": _named(names, sorted(right_top - left_top)),
                }
            )

    return {
        "trade_date": resolved_date,
        "universe": universes[0] if universes else None,
        "top_n": top_n,
        "limit": limit,
        "total_symbols": len(table_rows),
        "models": models,
        "rows": rows_out,
        "correlation": correlation,
        "disagreements": disagreements,
    }


def _stock_metadata(stock_info, symbols: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    if not symbols:
        return {}
    docs = list(
        stock_info.find(
            {"symbol": {"$in": list(symbols)}},
            {"_id": 0, "symbol": 1, "name": 1, "industry": 1},
        )
    )
    return {str(doc.get("symbol")): doc for doc in docs if doc.get("symbol")}


def _named(names: Dict[str, Dict[str, Any]], symbols: Sequence[str]) -> List[Dict[str, Any]]:
    return [
        {"symbol": symbol, "name": names.get(symbol, {}).get("name")}
        for symbol in symbols
    ]
