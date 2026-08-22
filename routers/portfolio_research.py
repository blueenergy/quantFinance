"""Portfolio research jobs and strategy publishing API."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Dict, List, Literal, Optional, Union
import os
import uuid

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING

from auth import get_current_user
from db import get_db_collections, get_trading_db_collections
from services import portfolio_research_s3 as research_s3


router = APIRouter(prefix="/portfolio-research", tags=["portfolio-research"])
logger = logging.getLogger(__name__)

UNIVERSE_BENCHMARK_SYMBOLS = {
    "csi1000": "000852.SH",
    "csi2000": "932000.CSI",
}

ATOMIC_SCORE_DIMENSIONS = {
    "technical",
    "fundamental",
    "value",
    "growth",
    "money_flow",
    "cycle",
}
ALLOWED_SCORE_COLUMNS = {
    "composite_score",
    "composite_balanced_score",
    "composite_aggressive_score",
    "composite_conservative_score",
    "composite_defensive_score",
    "composite_value_oriented_score",
    "composite_trading_oriented_score",
    "composite_growth_oriented_score",
    "composite_cycle_oriented_score",
    "composite_growth_cycle_score",
    *(f"{dimension}_score" for dimension in ATOMIC_SCORE_DIMENSIONS),
}


def _blank_to_none(value: Any) -> Any:
    """Treat HTML empty inputs as unset so clients do not trip 422 float parsing."""
    if value == "" or value is None:
        return None
    return value


class ColumnScoreSpec(BaseModel):
    mode: Literal["column"]
    column: str

    @field_validator("column")
    @classmethod
    def _validate_column(cls, value: str) -> str:
        column = str(value).strip()
        if column not in ALLOWED_SCORE_COLUMNS:
            raise ValueError(f"unsupported score column: {column}")
        return column


class WeightedScoreSpec(BaseModel):
    mode: Literal["weighted"]
    weights: Dict[str, float]

    @field_validator("weights")
    @classmethod
    def _validate_weights(cls, value: Dict[str, float]) -> Dict[str, float]:
        if not value:
            raise ValueError("weights must not be empty")
        unknown = sorted(set(value) - ATOMIC_SCORE_DIMENSIONS)
        if unknown:
            raise ValueError(f"unsupported score dimensions: {unknown}")
        if any(float(weight) < 0 for weight in value.values()):
            raise ValueError("weights must be non-negative")
        if sum(float(weight) for weight in value.values()) <= 0:
            raise ValueError("weights must have a positive total")
        return {str(dimension): float(weight) for dimension, weight in value.items()}


ScoreSpec = Annotated[
    Union[ColumnScoreSpec, WeightedScoreSpec],
    Field(discriminator="mode"),
]
RegimeMode = Literal["off", "bull_g60_else_cash"]


class CreateResearchJobRequest(BaseModel):
    name: str = Field(default="组合研究")
    universe_index: str = Field(default="csi1000")
    start_date: str
    end_date: str
    score_column: str = Field(default="composite_growth_cycle_score")
    growth_cycle_weights: List[str] = Field(default_factory=lambda: ["30:70"])
    score_specs: Optional[List[ScoreSpec]] = None
    top_n_values: List[int] = Field(default_factory=lambda: [10, 20, 50])
    selection_mode: Literal["fixed_top_n", "dynamic_score_threshold"] = Field(default="fixed_top_n")
    threshold_lookback_days: int = Field(default=10, ge=1)
    max_positions: int = Field(default=20, ge=1)
    horizon: int = Field(default=20, ge=1)
    rebalance_interval_days: Optional[List[int]] = None
    active_caps: List[float] = Field(default_factory=lambda: [0.2, 0.25, 0.3, 0.4])
    transaction_cost: float = Field(default=0.001, ge=0)
    buy_commission_rate: Optional[float] = Field(default=None, ge=0)
    sell_commission_rate: Optional[float] = Field(default=None, ge=0)
    min_commission: Optional[float] = Field(default=None, ge=0)
    stamp_tax_rate: Optional[float] = Field(default=None, ge=0)
    transfer_fee_rate: Optional[float] = Field(default=None, ge=0)
    index_benchmark_symbol: str = Field(default="000852.SH")
    cash_buffer: float = Field(default=0.03, ge=0, le=1)
    initial_capital: float = Field(default=1_000_000.0, gt=0)
    trailing_stop_pct: Optional[float] = Field(default=None, ge=0, le=1)
    trailing_stop_pcts: Optional[List[float]] = None
    industry_l1: Optional[str] = Field(default=None, description="Optional Shenwan L1 code or name filter")
    regime_cash: bool = Field(
        default=False,
        description="If true and regime_modes is omitted, run only bull-invest / non-bull-cash.",
    )
    regime_modes: Optional[List[RegimeMode]] = Field(
        default=None,
        min_length=1,
        description="Position modes to sweep: off and/or bull_g60_else_cash. Independent first-class arms.",
    )
    force: bool = Field(default=False)

    @field_validator("industry_l1", mode="before")
    @classmethod
    def _optional_industry_blank(cls, value: Any) -> Any:
        return _blank_to_none(value)

    @field_validator(
        "buy_commission_rate",
        "sell_commission_rate",
        "min_commission",
        "stamp_tax_rate",
        "transfer_fee_rate",
        "trailing_stop_pct",
        mode="before",
    )
    @classmethod
    def _optional_float_blank(cls, value: Any) -> Any:
        return _blank_to_none(value)

    @model_validator(mode="after")
    def _validate_score_inputs(self) -> "CreateResearchJobRequest":
        if self.score_specs and "growth_cycle_weights" in self.model_fields_set and self.growth_cycle_weights:
            raise ValueError("score_specs and growth_cycle_weights are mutually exclusive")
        if self.selection_mode == "dynamic_score_threshold":
            if len(self.top_n_values) != 1 or self.top_n_values[0] <= 0:
                raise ValueError("dynamic_score_threshold requires top_n_values to contain exactly one positive integer")
            if self.max_positions < self.top_n_values[0]:
                raise ValueError("max_positions must be greater than or equal to the dynamic top_n value")
        return self


class PublishResearchResultRequest(BaseModel):
    status: str = Field(default="draft", pattern="^(draft|enabled)$")
    preset_id: Optional[str] = None
    name: Optional[str] = None
    create_new_version: bool = Field(default=False)
    comment: str = Field(default="")
    selected_row: Optional[Dict[str, Any]] = None


class RerunResearchJobRequest(BaseModel):
    """Clone a research job; optional fields override the source job params.

    Unset fields keep the source values. ``force`` defaults to true so a rerun
    is not skipped by cache/idempotency.
    """

    name: Optional[str] = None
    universe_index: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    score_column: Optional[str] = None
    growth_cycle_weights: Optional[List[str]] = None
    score_specs: Optional[List[ScoreSpec]] = None
    top_n_values: Optional[List[int]] = None
    selection_mode: Optional[Literal["fixed_top_n", "dynamic_score_threshold"]] = None
    threshold_lookback_days: Optional[int] = Field(default=None, ge=1)
    max_positions: Optional[int] = Field(default=None, ge=1)
    horizon: Optional[int] = Field(default=None, ge=1)
    rebalance_interval_days: Optional[List[int]] = None
    active_caps: Optional[List[float]] = None
    transaction_cost: Optional[float] = Field(default=None, ge=0)
    buy_commission_rate: Optional[float] = Field(default=None, ge=0)
    sell_commission_rate: Optional[float] = Field(default=None, ge=0)
    min_commission: Optional[float] = Field(default=None, ge=0)
    stamp_tax_rate: Optional[float] = Field(default=None, ge=0)
    transfer_fee_rate: Optional[float] = Field(default=None, ge=0)
    index_benchmark_symbol: Optional[str] = None
    cash_buffer: Optional[float] = Field(default=None, ge=0, le=1)
    initial_capital: Optional[float] = Field(default=None, gt=0)
    trailing_stop_pct: Optional[float] = Field(default=None, ge=0, le=1)
    trailing_stop_pcts: Optional[List[float]] = None
    industry_l1: Optional[str] = None
    regime_cash: Optional[bool] = None
    regime_modes: Optional[List[RegimeMode]] = Field(default=None, min_length=1)
    force: bool = Field(default=True, description="Default true so a rerun is not skipped by cache/idempotency")

    @field_validator(
        "horizon",
        "transaction_cost",
        "buy_commission_rate",
        "sell_commission_rate",
        "min_commission",
        "stamp_tax_rate",
        "transfer_fee_rate",
        "cash_buffer",
        "initial_capital",
        "trailing_stop_pct",
        "index_benchmark_symbol",
        "industry_l1",
        mode="before",
    )
    @classmethod
    def _optional_blank(cls, value: Any) -> Any:
        return _blank_to_none(value)

    @field_validator("rebalance_interval_days", "top_n_values", "active_caps", "trailing_stop_pcts", "regime_modes", mode="before")
    @classmethod
    def _drop_null_list_items(cls, value: Any) -> Any:
        """Axios JSON-encodes NaN as null; drop those items instead of 422."""
        if not isinstance(value, list):
            return value
        return [item for item in value if item is not None]

    @model_validator(mode="after")
    def _validate_score_inputs(self) -> "RerunResearchJobRequest":
        if self.score_specs and self.growth_cycle_weights:
            raise ValueError("score_specs and growth_cycle_weights are mutually exclusive")
        # Only what the caller actually overrode is checked here; unset fields fall
        # back to the source job and the merged payload is revalidated by
        # CreateResearchJobRequest.
        if self.selection_mode == "dynamic_score_threshold":
            if self.top_n_values is not None and (len(self.top_n_values) != 1 or self.top_n_values[0] <= 0):
                raise ValueError("dynamic_score_threshold requires top_n_values to contain exactly one positive integer")
            if self.max_positions is not None and self.top_n_values and self.max_positions < self.top_n_values[0]:
                raise ValueError("max_positions must be greater than or equal to the dynamic top_n value")
        return self


def _jsonable(value: Any) -> Any:
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


def _current_user_id(current_user: Dict[str, Any]) -> str:
    user_id = current_user.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="current user id is missing")
    return str(user_id)


def _reviewer(current_user: Dict[str, Any]) -> str:
    return str(current_user.get("username") or current_user.get("email") or current_user.get("id") or "unknown")


def _normalize_date(value: str) -> str:
    raw = str(value or "").replace("-", "").strip()
    if len(raw) >= 8 and raw[:8].isdigit():
        return raw[:8]
    raise HTTPException(status_code=400, detail=f"invalid date: {value}")


def _ensure_research_indexes(collections: Dict[str, Any]) -> None:
    collections["portfolio_research_jobs"].create_index([("job_id", ASCENDING)], unique=True)
    collections["portfolio_research_jobs"].create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])
    collections["portfolio_research_jobs"].create_index([("status", ASCENDING), ("created_at", ASCENDING)])
    collections["portfolio_research_results"].create_index([("result_id", ASCENDING)], unique=True)
    collections["portfolio_research_results"].create_index([("job_id", ASCENDING)])
    collections["portfolio_research_results"].create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])
    collections["portfolio_research_reviews"].create_index([("job_id", ASCENDING), ("created_at", DESCENDING)])
    collections["portfolio_research_reviews"].create_index([("research_result_id", ASCENDING), ("created_at", DESCENDING)])
    collections["portfolio_strategy_param_presets"].create_index([("preset_id", ASCENDING)], unique=True)
    collections["portfolio_strategy_param_presets"].create_index([("strategy_template_id", ASCENDING), ("status", ASCENDING)])
    collections["portfolio_strategy_param_presets"].create_index(
        [("user_id", ASCENDING), ("strategy_template_id", ASCENDING), ("params_hash", ASCENDING), ("status", ASCENDING)]
    )


def _as_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item]
    if value:
        return [str(value)]
    return []


def _latest_stock_score_date(stock_scores: Any, job: Dict[str, Any]) -> Optional[str]:
    params = job.get("params") or {}
    end_date = str(params.get("end_date") or job.get("end_date") or "").replace("-", "")
    if not end_date:
        return None
    query: Dict[str, Any] = {"score_date": {"$lte": end_date}}
    universes = _as_list(params.get("universe_index") or job.get("universe_index"))
    if universes:
        query["index_codes"] = {"$in": universes}
    row = stock_scores.find_one(query, {"_id": 0, "score_date": 1}, sort=[("score_date", DESCENDING)])
    return str(row["score_date"]) if row and row.get("score_date") else None


def _normalized_watermark(job: Dict[str, Any], result: Optional[Dict[str, Any]], stock_scores: Any = None) -> Dict[str, Any]:
    watermark = dict((result or {}).get("data_watermark") or job.get("data_watermark") or {})
    if "backtest_score_max_date" not in watermark and watermark.get("stock_scores_max_date"):
        # Older research results used stock_scores_max_date for the backtestable dataset max date.
        watermark["backtest_score_max_date"] = watermark.get("stock_scores_max_date")
        latest_score = _latest_stock_score_date(stock_scores, job) if stock_scores is not None else None
        if latest_score:
            watermark["stock_scores_max_date"] = latest_score
    return watermark


def _job_summary(job: Dict[str, Any], result: Optional[Dict[str, Any]] = None, stock_scores: Any = None) -> Dict[str, Any]:
    row = dict(job)
    row["data_watermark"] = _normalized_watermark(job, result, stock_scores)
    if result:
        row["best_row"] = result.get("best_row")
        row["candidate_strategy_config"] = result.get("candidate_strategy_config")
        row["published_preset_id"] = result.get("published_preset_id")
        row["published_status"] = result.get("published_status")
        row["published_at"] = result.get("published_at")
        row["publish_action"] = result.get("publish_action")
    return row


RESEARCH_JOB_LIST_PROJECTION = {
    "_id": 0,
    "job_id": 1,
    "user_id": 1,
    "name": 1,
    "strategy_family": 1,
    "universe_index": 1,
    "start_date": 1,
    "end_date": 1,
    "params": 1,
    "status": 1,
    "attempts": 1,
    "result_id": 1,
    "error_message": 1,
    "created_at": 1,
    "updated_at": 1,
    "started_at": 1,
    "completed_at": 1,
    "heartbeat_at": 1,
    "progress_stage": 1,
    "progress_message": 1,
    "data_watermark": 1,
    "rerun_of_job_id": 1,
}


RESEARCH_RESULT_SUMMARY_PROJECTION = {
    "_id": 0,
    "result_id": 1,
    "job_id": 1,
    "best_row": 1,
    "candidate_strategy_config": 1,
    "published_preset_id": 1,
    "published_status": 1,
    "published_at": 1,
    "publish_action": 1,
}


def _unique_preset_id(collections: Dict[str, Any], requested: str, create_new_version: bool) -> str:
    preset_id = requested
    existing = collections["portfolio_strategy_param_presets"].find_one({"preset_id": preset_id})
    if not existing:
        return preset_id
    if not create_new_version:
        raise HTTPException(status_code=409, detail="preset_id already exists")
    suffix = datetime.utcnow().strftime("v%Y%m%d%H%M%S")
    candidate = f"{preset_id}_{suffix}"
    if collections["portfolio_strategy_param_presets"].find_one({"preset_id": candidate}):
        candidate = f"{preset_id}_{suffix}_{uuid.uuid4().hex[:6]}"
    return candidate


PRESET_PARAM_KEYS = {
    "universe_index",
    "index_benchmark_symbol",
    "score_type",
    "score_column",
    "growth_weight",
    "cycle_weight",
    "top_n",
    # selection_mode and its dynamic-threshold parameters are deliberately absent:
    # live construction only supports fixed Top-N, and adding keys here would change
    # params_hash for existing fixed presets, so republishing would fork a duplicate
    # preset instead of appending evidence to the current one.
    "rebalance_days",
    "construction_mode",
    "max_industry_weight",
    "lot_size",
    "transaction_cost",
    "buy_commission_rate",
    "sell_commission_rate",
    "min_commission",
    "stamp_tax_rate",
    "transfer_fee_rate",
    "execution_price",
    "cash_buffer",
    "initial_capital",
    "trailing_stop_pct",
    "candidate_pool_enabled",
    "industry_l1",
    "regime_cash",
}


def _report_dir_candidates() -> List[Path]:
    candidates: List[Path] = []
    for base_dir in (
        os.getenv("PORTFOLIO_RESEARCH_REPORT_DIR"),
        "/app/reports/portfolio-research",
        "reports/portfolio-research",
        Path(__file__).resolve().parents[2] / "shared" / "portfolio-research",
    ):
        if base_dir:
            candidates.append(Path(base_dir))
    return candidates


def _preset_params_from_candidate(candidate: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in candidate.items() if key in PRESET_PARAM_KEYS}


def _slug(value: Any) -> str:
    text = re.sub(r"[^0-9A-Za-z]+", "_", str(value or "")).strip("_")
    return text[:40] or "na"


def _candidate_strategy_config_from_row(job: Dict[str, Any], row: Dict[str, Any]) -> Dict[str, Any]:
    params = job.get("params") or {}
    universe = str(params.get("universe_index") or job.get("universe_index") or "csi1000")
    growth_weight = row.get("growth_weight")
    cycle_weight = row.get("cycle_weight")
    legacy_match = re.fullmatch(
        r"growth_cycle_g(\d+)_c(\d+)",
        str(row.get("score_variant") or ""),
    )
    if legacy_match and growth_weight is None and cycle_weight is None:
        total = float(int(legacy_match.group(1)) + int(legacy_match.group(2)))
        if total > 0:
            growth_weight = int(legacy_match.group(1)) / total
            cycle_weight = int(legacy_match.group(2)) / total
    if row.get("score_weights"):
        score_type = "weighted_score"
    elif growth_weight is not None and cycle_weight is not None:
        score_type = "growth_cycle_weighted"
    else:
        score_type = "score_column"
    top_n = int(row.get("top_n") or (params.get("top_n_values") or [10])[0])
    selection_mode = str(row.get("selection_mode") or params.get("selection_mode") or "fixed_top_n")
    threshold_lookback_days = int(
        row.get("threshold_lookback_days")
        if row.get("threshold_lookback_days") is not None
        else params.get("threshold_lookback_days", 10)
    )
    max_positions = int(
        row.get("max_positions") if row.get("max_positions") is not None else params.get("max_positions", 20)
    )
    rebalance_days = int(row.get("rebalance_interval_days") or params.get("horizon") or 20)
    variant = str(row.get("variant") or "top_n")
    score_variant = str(row.get("score_variant") or params.get("score_column") or "score")
    preset_id = "research_{universe}_{score}_{variant}_top{top_n}_{days}d_{suffix}".format(
        universe=_slug(universe),
        score=_slug(score_variant),
        variant=_slug(variant),
        top_n=top_n,
        days=rebalance_days,
        suffix=str(job.get("job_id") or uuid.uuid4())[:8],
    )
    trailing_stop_pct = row.get("trailing_stop_pct")
    if trailing_stop_pct is not None:
        try:
            trailing_stop_pct = float(trailing_stop_pct)
            if trailing_stop_pct <= 0:
                trailing_stop_pct = None
        except (TypeError, ValueError):
            trailing_stop_pct = None
    else:
        trailing_stop_pct = params.get("trailing_stop_pct")
    candidate = {
        "preset_id": preset_id,
        "strategy_template_id": "growth_cycle_index_enhanced",
        "name": f"{universe.upper()} {score_variant} {variant} Top{top_n} {rebalance_days}d",
        "universe_index": universe,
        "index_benchmark_symbol": params.get("index_benchmark_symbol") or row.get("index_benchmark_symbol") or "000852.SH",
        "score_type": score_type,
        "score_column": row.get("score_column") or params.get("score_column") or "composite_growth_cycle_score",
        "growth_weight": growth_weight,
        "cycle_weight": cycle_weight,
        "score_weights": row.get("score_weights"),
        "top_n": top_n,
        "selection_mode": selection_mode,
        "threshold_lookback_days": threshold_lookback_days,
        "max_positions": max_positions,
        "rebalance_days": rebalance_days,
        "construction_mode": row.get("construction_mode") or "top_n",
        "max_industry_weight": row.get("max_industry_weight"),
        "lot_size": int(params.get("lot_size") or 100),
        "transaction_cost": float(params.get("transaction_cost") or 0.001),
        "buy_commission_rate": params.get("buy_commission_rate"),
        "sell_commission_rate": params.get("sell_commission_rate"),
        "min_commission": params.get("min_commission"),
        "stamp_tax_rate": params.get("stamp_tax_rate"),
        "transfer_fee_rate": params.get("transfer_fee_rate"),
        "execution_price": params.get("execution_price") or "next_open",
        "cash_buffer": float(params.get("cash_buffer") or 0.03),
        "initial_capital": float(params.get("initial_capital") or 1_000_000.0),
        "trailing_stop_pct": trailing_stop_pct,
        "candidate_pool_enabled": bool(params.get("candidate_pool_enabled", True)),
        "industry_l1": str(params.get("industry_l1") or "").strip() or None,
        "status": "draft",
        "version": 1,
    }
    if str(row.get("regime_mode") or "off") == "bull_g60_else_cash":
        candidate["regime_cash"] = True
    return candidate


def _load_full_result_rows(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    preview_rows = list(result.get("rows") or [])
    json_path = (result.get("artifact") or {}).get("json_path")

    def _read_artifact_rows() -> Optional[List[Dict[str, Any]]]:
        if not json_path:
            return None
        path = _resolve_artifact_path(str(json_path))
        if path is None or not path.exists():
            candidate = Path(str(json_path))
            path = candidate if candidate.exists() else None
        if path is not None:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
            artifact_rows = list(payload.get("rows") or [])
            return artifact_rows or None
        artifact_meta = result.get("artifact") or {}
        if research_s3.artifact_uses_s3(artifact_meta):
            try:
                raw = research_s3.fetch_report_json_bytes(artifact_meta)
            except research_s3.ArtifactStorageUnavailable as exc:
                logger.warning("S3 research result unavailable: %s", exc)
                return None
            if raw:
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return None
                artifact_rows = list(payload.get("rows") or [])
                return artifact_rows or None
        return None

    artifact_rows = _read_artifact_rows()
    if artifact_rows and len(artifact_rows) > len(preview_rows):
        return artifact_rows

    total = int(result.get("row_count_total") or len(preview_rows))
    if total <= len(preview_rows):
        return preview_rows
    return artifact_rows or preview_rows


def _params_hash(params: Dict[str, Any]) -> str:
    payload = json.dumps(params, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _research_evidence(job: Dict[str, Any], result: Dict[str, Any], status: str, comment: str) -> Dict[str, Any]:
    return {
        "research_job_id": job["job_id"],
        "research_result_id": result["result_id"],
        "status": status,
        "comment": comment,
        "best_row": result.get("best_row"),
        "data_window": {"start_date": job.get("start_date"), "end_date": job.get("end_date")},
        "data_watermark_snapshot": result.get("data_watermark"),
        "attached_at": datetime.utcnow(),
    }


def _resolve_artifact_path(html_path: str) -> Optional[Path]:
    candidates = [Path(html_path)]
    filename = Path(html_path).name
    for base_dir in _report_dir_candidates():
        candidates.append(base_dir / filename)

    seen = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def _resolved_report_roots() -> List[Path]:
    """Absolute allowed roots for research artifacts (env + known mounts)."""
    roots: List[Path] = []
    for candidate in _report_dir_candidates():
        try:
            roots.append(candidate.resolve())
        except OSError:
            continue
    return roots


def _is_safe_artifact_path(path: Path) -> bool:
    """True only for paths strictly inside an allowed report root (never the root itself)."""
    try:
        resolved = path.resolve()
    except OSError:
        return False
    for root in _resolved_report_roots():
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        if resolved == root:
            return False
        return True
    return False


def _resolve_existing_path(raw_path: Optional[str]) -> Optional[Path]:
    """Resolve a recorded artifact path to an existing file or directory."""
    if not raw_path:
        return None
    candidates = [Path(raw_path)]
    path = Path(raw_path)
    for base_dir in _report_dir_candidates():
        candidates.append(base_dir / path.name)
        if len(path.parts) >= 2:
            candidates.append(base_dir / Path(*path.parts[-2:]))

    seen = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.exists():
            return candidate
    return None


def _safe_remove_path(path: Path, *, is_dir: bool, stats: Dict[str, Any]) -> None:
    if not _is_safe_artifact_path(path):
        msg = f"{path}: refused (outside allowed report directories or is a report root)"
        stats["errors"].append(msg)
        logger.warning("refused unsafe research artifact delete: %s", path)
        return
    try:
        if is_dir:
            shutil.rmtree(path)
        else:
            path.unlink()
        stats["removed"].append(str(path))
    except OSError as exc:
        stats["errors"].append(f"{path}: {exc}")
        logger.warning("failed to delete research artifact %s: %s", path, exc)


def _delete_artifact_files(
    artifact: Optional[Dict[str, Any]],
    *,
    job_id: Optional[str] = None,
    result_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Best-effort removal of HTML/JSON/combos files. Never raises.

    Only deletes paths that resolve strictly inside configured report directories.
    """
    stats: Dict[str, Any] = {"removed": [], "missing": [], "errors": []}
    if not artifact:
        return stats

    if research_s3.artifact_uses_s3(artifact):
        if not job_id or not result_id:
            stats["errors"].append("refused S3 cleanup without job_id and result_id")
        else:
            expected_prefix = research_s3.artifact_prefix(job_id, result_id)
            s3_stats = research_s3.delete_artifact_prefix(artifact, expected_prefix=expected_prefix)
            stats["removed"].extend(s3_stats.get("removed") or [])
            stats["errors"].extend(s3_stats.get("errors") or [])

    for key in ("html_path", "json_path"):
        raw = artifact.get(key)
        if not raw:
            continue
        path = _resolve_existing_path(str(raw))
        if path is None or not path.is_file():
            stats["missing"].append(str(raw))
            continue
        _safe_remove_path(path, is_dir=False, stats=stats)

    combos_dir_raw = artifact.get("combos_dir")
    combos_dir = _resolve_existing_path(str(combos_dir_raw)) if combos_dir_raw else None
    if combos_dir_raw and combos_dir is None:
        stats["missing"].append(str(combos_dir_raw))
    elif combos_dir is not None and combos_dir.is_dir():
        _safe_remove_path(combos_dir, is_dir=True, stats=stats)
    else:
        for entry in artifact.get("combos") or []:
            file_rel = entry.get("file")
            if not file_rel:
                continue
            path = _resolve_combo_file(combos_dir_raw, file_rel)
            if path is None:
                stats["missing"].append(str(file_rel))
                continue
            _safe_remove_path(path, is_dir=False, stats=stats)

    return stats


@router.get("/sw-l1-catalog")
async def list_sw_l1_catalog(current_user: dict = Depends(get_current_user)):
    """Return Shenwan 2021 L1 industries for research UI pickers."""
    from db import data_db

    coll = data_db["sw_index_classify"]
    cursor = coll.find(
        {"src": "SW2021", "level": "L1"},
        {"_id": 0, "index_code": 1, "industry_name": 1, "name": 1},
    )
    rows: List[Dict[str, str]] = []
    seen: set[str] = set()
    for doc in cursor:
        code = str(doc.get("index_code") or "").strip()
        if not code or code in seen:
            continue
        name = str(doc.get("industry_name") or doc.get("name") or code).strip()
        rows.append({"code": code, "name": name})
        seen.add(code)
    rows.sort(key=lambda item: item["code"])
    return {"success": True, "data": rows}


@router.post("/jobs")
async def create_portfolio_research_job(request: CreateResearchJobRequest, current_user: dict = Depends(get_current_user)):
    collections = get_trading_db_collections()
    _ensure_research_indexes(collections)
    user_id = _current_user_id(current_user)
    job = _build_research_job_document(user_id, request)
    collections["portfolio_research_jobs"].insert_one(job)
    return {"success": True, "data": _jsonable(job)}


def _build_research_job_document(user_id: str, request: CreateResearchJobRequest) -> Dict[str, Any]:
    start_date = _normalize_date(request.start_date)
    end_date = _normalize_date(request.end_date)
    if start_date > end_date:
        raise HTTPException(status_code=400, detail="start_date must be <= end_date")

    now = datetime.utcnow()
    params = request.model_dump()
    if request.score_specs:
        params.pop("growth_cycle_weights", None)
    params["start_date"] = start_date
    params["end_date"] = end_date
    params["rebalance_interval_days"] = request.rebalance_interval_days or [request.horizon]
    if request.universe_index == "csi2000" and request.index_benchmark_symbol == "000852.SH":
        params["index_benchmark_symbol"] = UNIVERSE_BENCHMARK_SYMBOLS["csi2000"]
    industry_l1 = str(request.industry_l1 or "").strip()
    if industry_l1:
        params["industry_l1"] = industry_l1
        default_bench = UNIVERSE_BENCHMARK_SYMBOLS.get(request.universe_index, "000852.SH")
        if str(params.get("index_benchmark_symbol") or "") == default_bench:
            if industry_l1.upper().endswith(".SI"):
                params["index_benchmark_symbol"] = industry_l1
            else:
                params.pop("index_benchmark_symbol", None)
    return {
        "job_id": str(uuid.uuid4()),
        "user_id": user_id,
        "name": request.name,
        "strategy_family": (
            "weighted_score"
            if request.score_specs and any(spec.mode == "weighted" for spec in request.score_specs)
            else "score_column"
            if request.score_specs or not request.growth_cycle_weights
            else "growth_cycle"
        ),
        "universe_index": request.universe_index,
        "start_date": start_date,
        "end_date": end_date,
        "params": params,
        "status": "pending",
        "attempts": 0,
        "result_id": None,
        "error_message": None,
        "created_at": now,
        "updated_at": now,
    }


def _rerun_name(base_name: Optional[str]) -> str:
    name = str(base_name or "组合研究").strip() or "组合研究"
    if name.endswith("(rerun)"):
        return name
    return f"{name} (rerun)"


def _create_request_from_job(
    job: Dict[str, Any],
    *,
    options: Optional[RerunResearchJobRequest] = None,
) -> CreateResearchJobRequest:
    """Rebuild a create payload from a stored job, applying optional overrides."""
    options = options or RerunResearchJobRequest()
    params = dict(job.get("params") or {})
    if not params.get("universe_index"):
        params["universe_index"] = job.get("universe_index")
    if not params.get("start_date"):
        params["start_date"] = job.get("start_date")
    if not params.get("end_date"):
        params["end_date"] = job.get("end_date")

    field_names = set(CreateResearchJobRequest.model_fields.keys())
    payload = {key: params[key] for key in field_names if key in params and params[key] is not None}

    overrides = options.model_dump(exclude_unset=True)
    if overrides.get("score_specs"):
        payload.pop("growth_cycle_weights", None)
    if overrides.get("growth_cycle_weights"):
        payload.pop("score_specs", None)
    for key, value in overrides.items():
        if key == "force":
            continue
        if value is not None and key in field_names:
            payload[key] = value

    name_override = overrides.get("name") if "name" in overrides else None
    payload["name"] = _rerun_name(name_override or job.get("name") or params.get("name"))
    payload["force"] = options.force
    try:
        return CreateResearchJobRequest(**payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"cannot rebuild research params for rerun: {exc}") from exc


@router.post("/jobs/{job_id}/rerun")
async def rerun_portfolio_research_job(
    job_id: str,
    request: Optional[RerunResearchJobRequest] = None,
    current_user: dict = Depends(get_current_user),
):
    """Clone a research job into a new pending job.

    Optional body fields override source params. Does not modify the source
    job/result — creates a fresh job_id so the original report remains available.
    """
    collections = get_trading_db_collections()
    _ensure_research_indexes(collections)
    user_id = _current_user_id(current_user)
    source = collections["portfolio_research_jobs"].find_one({"job_id": job_id, "user_id": user_id})
    if not source:
        raise HTTPException(status_code=404, detail="research job not found")

    options = request or RerunResearchJobRequest()
    create_request = _create_request_from_job(source, options=options)
    job = _build_research_job_document(user_id, create_request)
    job["rerun_of_job_id"] = job_id
    collections["portfolio_research_jobs"].insert_one(job)
    return {"success": True, "data": _jsonable(job)}


@router.delete("/jobs/{job_id}")
async def delete_portfolio_research_job(job_id: str, current_user: dict = Depends(get_current_user)):
    """Hard-delete a research job and its result/reviews/artifacts.

    Does not delete published parameter presets (they may be referenced by plans).
    Pending/running jobs cannot be deleted while the worker may still write results.
    """
    collections = get_trading_db_collections()
    user_id = _current_user_id(current_user)
    job = collections["portfolio_research_jobs"].find_one({"job_id": job_id, "user_id": user_id})
    if not job:
        raise HTTPException(status_code=404, detail="research job not found")

    status = str(job.get("status") or "")
    if status in {"pending", "running"}:
        raise HTTPException(
            status_code=409,
            detail=f"cannot delete research job while status is {status}",
        )

    result = collections["portfolio_research_results"].find_one({"job_id": job_id, "user_id": user_id})
    artifact = (result or {}).get("artifact")
    published_preset_id = (result or {}).get("published_preset_id")

    # Delete durable DB records first; file cleanup is best-effort afterwards so a
    # later Mongo failure cannot leave a visible job with its report already gone.
    deleted_results = collections["portfolio_research_results"].delete_many({"job_id": job_id, "user_id": user_id})
    review_query: Dict[str, Any] = {
        "user_id": user_id,
        "$or": [
            {"job_id": job_id},
            {"research_job_id": job_id},
        ],
    }
    result_id = (result or {}).get("result_id")
    if result_id:
        review_query["$or"].append({"research_result_id": result_id})
    deleted_reviews = collections["portfolio_research_reviews"].delete_many(review_query)
    deleted_jobs = collections["portfolio_research_jobs"].delete_one({"job_id": job_id, "user_id": user_id})

    artifact_stats = _delete_artifact_files(artifact, job_id=job_id, result_id=result_id)
    return {
        "success": True,
        "data": {
            "job_id": job_id,
            "deleted_jobs": getattr(deleted_jobs, "deleted_count", 1 if deleted_jobs else 0),
            "deleted_results": getattr(deleted_results, "deleted_count", 0),
            "deleted_reviews": getattr(deleted_reviews, "deleted_count", 0),
            "artifact_cleanup": artifact_stats,
            "published_preset_kept": published_preset_id,
        },
    }


@router.get("/jobs")
async def list_portfolio_research_jobs(
    status: Optional[str] = Query(default=None),
    universe_index: Optional[str] = Query(default=None),
    growth_cycle_weight: Optional[str] = Query(
        default=None,
        description="Filter jobs whose params.growth_cycle_weights contains this pair, e.g. 30:70",
    ),
    limit: int = Query(default=50, ge=1, le=200),
    current_user: dict = Depends(get_current_user),
):
    collections = get_trading_db_collections()
    user_id = _current_user_id(current_user)
    query: Dict[str, Any] = {"user_id": user_id}
    if status:
        query["status"] = status
    universe = (universe_index or "").strip()
    if universe:
        # Jobs store universe both at top-level and inside params; match either.
        query["$or"] = [
            {"universe_index": universe},
            {"params.universe_index": universe},
        ]
    weight = _normalize_growth_cycle_weight(growth_cycle_weight)
    if weight:
        # Mongo equality on an array field means "array contains this element".
        query["params.growth_cycle_weights"] = weight
    jobs = list(collections["portfolio_research_jobs"].find(query, RESEARCH_JOB_LIST_PROJECTION).sort("created_at", -1).limit(limit))
    result_ids = [job.get("result_id") for job in jobs if job.get("result_id")]
    results = {
        row.get("result_id"): row
        for row in collections["portfolio_research_results"].find({"result_id": {"$in": result_ids}}, RESEARCH_RESULT_SUMMARY_PROJECTION)
    } if result_ids else {}
    stock_scores = get_db_collections()["stock_scores"]
    return {"success": True, "data": _jsonable([_job_summary(job, results.get(job.get("result_id")), stock_scores) for job in jobs])}


def _normalize_growth_cycle_weight(value: Optional[str]) -> Optional[str]:
    """Normalize ``30:70`` / ``30：70`` style weight pairs; empty → None."""
    if value is None:
        return None
    text = str(value).strip().replace("：", ":")
    if not text:
        return None
    return text


@router.get("/jobs/{job_id}")
async def get_portfolio_research_job(job_id: str, current_user: dict = Depends(get_current_user)):
    collections = get_trading_db_collections()
    user_id = _current_user_id(current_user)
    job = collections["portfolio_research_jobs"].find_one({"job_id": job_id, "user_id": user_id}, RESEARCH_JOB_LIST_PROJECTION)
    if not job:
        raise HTTPException(status_code=404, detail="research job not found")
    result = None
    if job.get("result_id"):
        result = collections["portfolio_research_results"].find_one({"result_id": job["result_id"], "user_id": user_id}, RESEARCH_RESULT_SUMMARY_PROJECTION)
    stock_scores = get_db_collections()["stock_scores"]
    return {"success": True, "data": _jsonable(_job_summary(job, result, stock_scores))}


@router.get("/jobs/{job_id}/results")
async def get_portfolio_research_results(job_id: str, current_user: dict = Depends(get_current_user)):
    collections = get_trading_db_collections()
    user_id = _current_user_id(current_user)
    job = collections["portfolio_research_jobs"].find_one({"job_id": job_id, "user_id": user_id})
    if not job:
        raise HTTPException(status_code=404, detail="research job not found")
    result = collections["portfolio_research_results"].find_one({"job_id": job_id, "user_id": user_id})
    if not result:
        raise HTTPException(status_code=404, detail="research result not found")
    return {"success": True, "data": _jsonable(result)}


@router.get("/jobs/{job_id}/results/rows")
async def list_portfolio_research_result_rows(
    job_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    current_user: dict = Depends(get_current_user),
):
    collections = get_trading_db_collections()
    user_id = _current_user_id(current_user)
    job = collections["portfolio_research_jobs"].find_one({"job_id": job_id, "user_id": user_id})
    if not job:
        raise HTTPException(status_code=404, detail="research job not found")
    result = collections["portfolio_research_results"].find_one({"job_id": job_id, "user_id": user_id})
    if not result:
        raise HTTPException(status_code=404, detail="research result not found")
    rows = _load_full_result_rows(result)
    total = len(rows)
    start = (page - 1) * page_size
    end = start + page_size
    return {
        "success": True,
        "data": {
            "rows": _jsonable(rows[start:end]),
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": max(1, (total + page_size - 1) // page_size),
            "source": "artifact_json" if total > len(result.get("rows") or []) else "mongo_preview",
        },
    }


@router.get("/jobs/{job_id}/artifact")
async def get_portfolio_research_artifact(job_id: str, current_user: dict = Depends(get_current_user)):
    collections = get_trading_db_collections()
    user_id = _current_user_id(current_user)
    result = collections["portfolio_research_results"].find_one({"job_id": job_id, "user_id": user_id})
    if not result:
        raise HTTPException(status_code=404, detail="research result not found")
    artifact = result.get("artifact") or {}
    html_path = artifact.get("html_path")
    if not html_path:
        raise HTTPException(status_code=404, detail="artifact not found")
    path = _resolve_artifact_path(str(html_path))
    if path is not None:
        return FileResponse(str(path), media_type="text/html")
    if research_s3.artifact_uses_s3(artifact):
        try:
            body = research_s3.fetch_report_html_bytes(artifact)
        except research_s3.ArtifactStorageUnavailable as exc:
            raise HTTPException(status_code=503, detail="artifact storage unavailable") from exc
        if body is not None:
            return Response(content=body, media_type="text/html")
    raise HTTPException(status_code=404, detail="artifact file not found")


def _resolve_combo_file(combos_dir: Optional[str], file_rel: Optional[str]) -> Optional[Path]:
    """Resolve a per-combo detail JSON, mirroring the artifact path fallbacks.

    ``file_rel`` is usually ``<combo_key>.json`` under ``combos_dir``; legacy rows
    may store ``<combos_dir>/<key>.json`` or absolute container paths.
    """
    candidates: List[Path] = []
    file_path = Path(file_rel) if file_rel else None
    combos_path = Path(combos_dir) if combos_dir else None
    combo_file_name = file_path.name if file_path else None
    combos_leaf = combos_path.name if combos_path else None

    if file_path:
        candidates.append(file_path)
    if combos_path and combo_file_name:
        candidates.append(combos_path / combo_file_name)
    for base_dir in _report_dir_candidates():
        if combos_leaf and combo_file_name:
            candidates.append(base_dir / combos_leaf / combo_file_name)
        if file_path:
            candidates.append(base_dir / file_path)
        if combos_path and combo_file_name:
            candidates.append(base_dir / combos_path.name / combo_file_name)
        if file_path and len(file_path.parts) >= 2:
            candidates.append(base_dir / Path(*file_path.parts[-2:]))

    seen = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


@router.get("/jobs/{job_id}/combos")
async def list_portfolio_research_combos(job_id: str, current_user: dict = Depends(get_current_user)):
    collections = get_trading_db_collections()
    user_id = _current_user_id(current_user)
    result = collections["portfolio_research_results"].find_one({"job_id": job_id, "user_id": user_id})
    if not result:
        raise HTTPException(status_code=404, detail="research result not found")
    combos = (result.get("artifact") or {}).get("combos") or []
    return {"success": True, "data": _jsonable(combos)}


@router.get("/jobs/{job_id}/combos/{combo_key}")
async def get_portfolio_research_combo_detail(job_id: str, combo_key: str, current_user: dict = Depends(get_current_user)):
    collections = get_trading_db_collections()
    user_id = _current_user_id(current_user)
    result = collections["portfolio_research_results"].find_one({"job_id": job_id, "user_id": user_id})
    if not result:
        raise HTTPException(status_code=404, detail="research result not found")
    artifact = result.get("artifact") or {}
    combos = artifact.get("combos") or []
    entry = next((c for c in combos if c.get("combo_key") == combo_key), None)
    if not entry:
        raise HTTPException(status_code=404, detail="combo detail not found")
    path = _resolve_combo_file(artifact.get("combos_dir"), entry.get("file"))
    if path is not None:
        return FileResponse(str(path), media_type="application/json")
    if research_s3.artifact_uses_s3(artifact):
        try:
            body = research_s3.fetch_combo_bytes(artifact, combo_key, entry.get("file"))
        except research_s3.ArtifactStorageUnavailable as exc:
            raise HTTPException(status_code=503, detail="artifact storage unavailable") from exc
        if body is not None:
            return Response(content=body, media_type="application/json")
    raise HTTPException(status_code=404, detail="combo detail file not found")


@router.post("/results/{result_id}/publish")
async def publish_portfolio_research_result(
    result_id: str,
    request: PublishResearchResultRequest,
    current_user: dict = Depends(get_current_user),
):
    collections = get_trading_db_collections()
    user_id = _current_user_id(current_user)
    result = collections["portfolio_research_results"].find_one({"result_id": result_id, "user_id": user_id})
    if not result:
        raise HTTPException(status_code=404, detail="research result not found")
    if result.get("published_preset_id"):
        raise HTTPException(status_code=409, detail="research result has already been published")
    job = collections["portfolio_research_jobs"].find_one({"job_id": result.get("job_id"), "user_id": user_id})
    if not job or job.get("status") != "completed":
        raise HTTPException(status_code=400, detail="research job is not completed")
    candidate = dict(result.get("candidate_strategy_config") or {})
    if request.selected_row:
        candidate = _candidate_strategy_config_from_row(job, request.selected_row)
    elif (
        "regime_cash" not in candidate
        and str((result.get("best_row") or {}).get("regime_mode") or "off") == "bull_g60_else_cash"
    ):
        # Backfill results emitted by workers from before regime mode was
        # included in candidate_strategy_config.
        candidate["regime_cash"] = True
    if not candidate:
        raise HTTPException(status_code=400, detail="candidate strategy config is missing")
    job_params = job.get("params") or {}
    if (
        (candidate.get("selection_mode") or "fixed_top_n") == "dynamic_score_threshold"
        or job_params.get("selection_mode") == "dynamic_score_threshold"
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "Dynamic score-threshold research results cannot be published because "
                "live portfolio construction does not support dynamic_score_threshold yet."
            ),
        )
    candidate_score_type = candidate.get("score_type")
    if candidate_score_type and candidate_score_type != "growth_cycle_weighted":
        raise HTTPException(
            status_code=409,
            detail=(
                "Only growth_cycle_weighted research results can be published. "
                "Generic score-column and weighted-score construction templates "
                "are not productized yet."
            ),
        )
    if candidate.get("score_weights"):
        raise HTTPException(
            status_code=409,
            detail="Generic weighted-score research results cannot be published yet.",
        )

    now = datetime.utcnow()
    strategy_template_id = str(candidate.get("strategy_template_id") or candidate.get("template_id") or "growth_cycle_index_enhanced")
    preset_params = _preset_params_from_candidate(candidate)
    params_hash = _params_hash(preset_params)
    existing_preset = collections["portfolio_strategy_param_presets"].find_one(
        {
            "user_id": user_id,
            "strategy_template_id": strategy_template_id,
            "status": {"$in": ["draft", "enabled", "recommended"]},
            "$or": [{"params_hash": params_hash}, {"params": preset_params}],
        }
    )
    if existing_preset:
        preset_id = existing_preset["preset_id"]
        evidence = _research_evidence(job, result, existing_preset.get("status") or request.status, request.comment)
        collections["portfolio_strategy_param_presets"].update_one(
            {"preset_id": preset_id, "user_id": user_id},
            {
                "$set": {
                    "params_hash": params_hash,
                    "updated_at": now,
                    "latest_evidence": evidence,
                    "latest_research_job_id": job["job_id"],
                    "latest_research_result_id": result_id,
                },
                "$push": {"evidence": evidence},
            },
        )
        review = {
            "review_id": str(uuid.uuid4()),
            "job_id": job["job_id"],
            "research_result_id": result_id,
            "preset_id": preset_id,
            "strategy_template_id": strategy_template_id,
            "decision": "evidence_attached",
            "status": existing_preset.get("status") or request.status,
            "comment": request.comment,
            "reviewer": _reviewer(current_user),
            "user_id": user_id,
            "created_at": now,
        }
        collections["portfolio_research_reviews"].insert_one(review)
        collections["portfolio_research_results"].update_one(
            {"result_id": result_id},
            {
                "$set": {
                    "published_preset_id": preset_id,
                    "published_status": existing_preset.get("status") or request.status,
                    "published_at": now,
                    "publish_action": "evidence_attached",
                }
            },
        )
        return {
            "success": True,
            "data": _jsonable({"preset": existing_preset, "review": review, "attached_to_existing": True}),
        }

    requested_id = request.preset_id or candidate.get("preset_id") or f"research_{result_id[:8]}"
    preset_id = _unique_preset_id(collections, str(requested_id), request.create_new_version)
    evidence = _research_evidence(job, result, request.status, request.comment)
    preset = {
        "preset_id": preset_id,
        "strategy_template_id": strategy_template_id,
        "user_id": user_id,
        "name": request.name or candidate.get("name") or preset_id,
        "description": request.comment,
        "params": preset_params,
        "params_hash": params_hash,
        "status": request.status,
        "is_default": False,
        "priority": 100,
        "source": "portfolio_research",
        "research_job_id": job["job_id"],
        "research_result_id": result_id,
        "backtest_summary": request.selected_row or result.get("best_row"),
        "data_window": {"start_date": job.get("start_date"), "end_date": job.get("end_date")},
        "data_watermark_snapshot": result.get("data_watermark"),
        "evidence": [evidence],
        "latest_evidence": evidence,
        "latest_research_job_id": job["job_id"],
        "latest_research_result_id": result_id,
        "created_by": user_id,
        "created_at": now,
        "updated_at": now,
    }
    collections["portfolio_strategy_param_presets"].insert_one(preset)
    review = {
        "review_id": str(uuid.uuid4()),
        "job_id": job["job_id"],
        "research_result_id": result_id,
        "preset_id": preset_id,
        "strategy_template_id": strategy_template_id,
        "decision": "preset_published",
        "status": request.status,
        "comment": request.comment,
        "reviewer": _reviewer(current_user),
        "user_id": user_id,
        "created_at": now,
    }
    collections["portfolio_research_reviews"].insert_one(review)
    collections["portfolio_research_results"].update_one(
        {"result_id": result_id},
        {
            "$set": {
                "published_preset_id": preset_id,
                "published_status": request.status,
                "published_at": now,
                "publish_action": "preset_published",
            }
        },
    )
    return {"success": True, "data": _jsonable({"preset": preset, "review": review, "attached_to_existing": False})}


@router.post("/parameter-presets/{preset_id}/disable")
async def disable_portfolio_research_preset(preset_id: str, current_user: dict = Depends(get_current_user)):
    collections = get_trading_db_collections()
    user_id = _current_user_id(current_user)
    preset = collections["portfolio_strategy_param_presets"].find_one({"preset_id": preset_id, "user_id": user_id})
    if not preset:
        raise HTTPException(status_code=404, detail="parameter preset not found")
    if preset.get("source") != "portfolio_research":
        raise HTTPException(status_code=400, detail="only portfolio research presets can be disabled here")
    now = datetime.utcnow()
    collections["portfolio_strategy_param_presets"].update_one(
        {"preset_id": preset_id, "user_id": user_id},
        {"$set": {"status": "disabled", "disabled_by": user_id, "disabled_at": now, "updated_at": now}},
    )
    review = {
        "review_id": str(uuid.uuid4()),
        "job_id": preset.get("research_job_id"),
        "research_result_id": preset.get("research_result_id"),
        "preset_id": preset_id,
        "strategy_template_id": preset.get("strategy_template_id"),
        "decision": "disabled",
        "reviewer": _reviewer(current_user),
        "user_id": user_id,
        "created_at": now,
    }
    collections["portfolio_research_reviews"].insert_one(review)
    return {"success": True, "data": _jsonable(review)}
