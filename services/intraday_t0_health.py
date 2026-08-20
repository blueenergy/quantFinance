"""Intraday T+0 health checks — liveness (dev/CI) vs readiness (prod pre-flight).

Liveness verifies the service can run (Mongo, collections, engine smoke).
Readiness verifies upstream realtime data and signal pipeline for a trade date.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from db import get_data_db, get_db_collections, realtime_db

from services.intraday_t0_signals import (
    classify_signal,
    compact_trade_date,
    ensure_intraday_t0_indexes,
)

CheckStatus = Literal["pass", "fail", "warn", "skip"]

ORDERBOOK_FIELDS = (
    "realtime_bid_price1",
    "realtime_bid_volume1",
    "realtime_ask_price1",
    "realtime_ask_volume1",
    "realtime_num",
    "realtime_amount_yuan",
)

FINANCE_COLLECTIONS = (
    "intraday_t0_mock_positions",
    "intraday_t0_signals",
    "intraday_t0_position_snapshots",
    "intraday_t0_signal_evaluations",
    "trader_positions",
)

REALTIME_COLLECTIONS = (
    "volume_price_realtime_latest",
    "market_spectrum_realtime_daily",
)


@dataclass
class HealthCheck:
    name: str
    status: CheckStatus
    detail: str
    required: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class HealthReport:
    mode: str
    trade_date: str
    overall: CheckStatus
    checks: List[HealthCheck] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.overall in {"pass", "warn"},
            "mode": self.mode,
            "trade_date": self.trade_date,
            "overall": self.overall,
            "checks": [c.to_dict() for c in self.checks],
            "timestamp": datetime.now().isoformat(),
        }


def _today_compact() -> str:
    return datetime.now().strftime("%Y%m%d")


def _rollup(checks: List[HealthCheck]) -> CheckStatus:
    required_fails = [c for c in checks if c.required and c.status == "fail"]
    if required_fails:
        return "fail"
    if any(c.status == "warn" for c in checks):
        return "warn"
    return "pass"


def _engine_smoke_check() -> HealthCheck:
    features = {
        "intraday_position": 0.85,
        "pnl_pct": 2.0,
        "bid_ask_imbalance": -0.25,
        "sector_positive_ratio": 0.30,
        "market_yang_spectrum": 0.35,
        "time_of_day": "14:00-14:30",
        "pct_chg": -0.5,
        "avg_trade_size_yuan": 25000,
    }
    signal_type, score, reasons = classify_signal({}, features, "202608201420")
    ok = signal_type == "SELL_WATCH" and score >= 58 and reasons
    return HealthCheck(
        name="engine_smoke",
        status="pass" if ok else "fail",
        detail=f"signal_type={signal_type} score={score}",
    )


def _mongo_ping_check() -> HealthCheck:
    try:
        get_db_collections()["intraday_t0_signals"].database.client.admin.command("ping")
        return HealthCheck(name="mongo_ping", status="pass", detail="ok")
    except Exception as exc:
        return HealthCheck(name="mongo_ping", status="fail", detail=str(exc))


def _indexes_check() -> HealthCheck:
    try:
        ensure_intraday_t0_indexes()
        return HealthCheck(name="indexes", status="pass", detail="intraday_t0 indexes ensured")
    except Exception as exc:
        return HealthCheck(name="indexes", status="fail", detail=str(exc))


def _collections_check() -> HealthCheck:
    missing: List[str] = []
    try:
        finance_names = set(get_db_collections()["intraday_t0_signals"].database.list_collection_names())
        for name in FINANCE_COLLECTIONS:
            if name not in finance_names:
                missing.append(f"finance.{name}")
        realtime_names = set(realtime_db.list_collection_names())
        for name in REALTIME_COLLECTIONS:
            if name not in realtime_names:
                missing.append(f"realtime.{name}")
    except Exception as exc:
        return HealthCheck(name="collections", status="fail", detail=str(exc))
    if missing:
        return HealthCheck(
            name="collections",
            status="warn",
            detail=f"missing (may be created on first write): {', '.join(missing)}",
            required=False,
        )
    return HealthCheck(name="collections", status="pass", detail="finance + realtime collections present")


def _internal_key_check() -> HealthCheck:
    key = (os.getenv("HERMES_QUANT_INTERNAL_KEY") or "").strip()
    if key and key not in {"change_me", "your_key"}:
        return HealthCheck(name="internal_key", status="pass", detail="HERMES_QUANT_INTERNAL_KEY configured")
    return HealthCheck(
        name="internal_key",
        status="warn",
        detail="HERMES_QUANT_INTERNAL_KEY missing or placeholder (generate-all will fail)",
        required=False,
    )


def _realtime_quotes_check(trade_date: str) -> List[HealthCheck]:
    coll = get_data_db()["volume_price_realtime_latest"]
    total = coll.count_documents({"trade_date": trade_date})
    with_close = coll.count_documents({"trade_date": trade_date, "close": {"$gt": 0}})
    sample = coll.find_one(
        {"trade_date": trade_date, "close": {"$gt": 0}},
        {"symbol": 1, "close": 1, **{f: 1 for f in ORDERBOOK_FIELDS}},
    )
    ob_ok = bool(
        sample and all(sample.get(f) not in (None, "", 0) for f in ORDERBOOK_FIELDS[:4])
    )
    return [
        HealthCheck(
            name="realtime_quotes",
            status="pass" if total > 0 and with_close > 0 else "fail",
            detail=f"trade_date={trade_date} rows={total} with_close={with_close}",
        ),
        HealthCheck(
            name="orderbook_fields",
            status="pass" if ob_ok else "fail",
            detail=(
                f"sample={sample.get('symbol') if sample else None} "
                f"bid1={sample.get('realtime_bid_price1') if sample else None} "
                f"ask1={sample.get('realtime_ask_price1') if sample else None}"
            ),
        ),
    ]


def _market_spectrum_check(trade_date: str) -> HealthCheck:
    coll = get_data_db()["market_spectrum_realtime_daily"]
    doc = coll.find_one({"snapshot_trade_date": trade_date}, sort=[("snapshot_time", -1)])
    yang = doc.get("yang_spectrum") if doc else None
    ok = doc is not None and yang is not None
    return HealthCheck(
        name="market_spectrum",
        status="pass" if ok else "fail",
        detail=f"snapshot_time={doc.get('snapshot_time') if doc else None} yang_spectrum={yang}",
    )


def _signals_check(trade_date: str, user_id: Optional[str]) -> HealthCheck:
    query: Dict[str, Any] = {"trade_date": trade_date}
    if user_id:
        query["user_id"] = user_id
    coll = get_db_collections()["intraday_t0_signals"]
    total = coll.count_documents(query)
    latest = coll.find_one(query, sort=[("snapshot_time", -1)])
    watch = coll.count_documents({**query, "signal_type": {"$in": ["BUY_WATCH", "SELL_WATCH"]}})
    # Signals depend on market hours + trigger; warn outside strict prod gate.
    status: CheckStatus = "pass" if total > 0 else "warn"
    return HealthCheck(
        name="intraday_t0_signals",
        status=status,
        detail=(
            f"trade_date={trade_date} total={total} watch={watch} "
            f"latest_snapshot={latest.get('snapshot_time') if latest else None}"
        ),
        required=False,
    )


def _position_sources_check(user_id: Optional[str]) -> HealthCheck:
    pos_query: Dict[str, Any] = {"qty": {"$gt": 0}}
    mock_query: Dict[str, Any] = {"enabled": True}
    if user_id:
        pos_query["user_id"] = user_id
        mock_query["user_id"] = user_id
    real_count = get_db_collections()["trader_positions"].count_documents(pos_query)
    mock_count = get_db_collections()["intraday_t0_mock_positions"].count_documents(mock_query)
    ok = real_count > 0 or mock_count > 0
    return HealthCheck(
        name="position_sources",
        status="pass" if ok else "warn",
        detail=f"trader_positions={real_count} mock_enabled={mock_count}",
        required=False,
    )


def _trigger_config_check() -> HealthCheck:
    url = (os.getenv("INTRADAY_T0_TRIGGER_URL") or "").strip()
    if url:
        return HealthCheck(name="trigger_url_config", status="pass", detail=url, required=False)
    return HealthCheck(
        name="trigger_url_config",
        status="warn",
        detail="INTRADAY_T0_TRIGGER_URL unset on quant-api (expected on data-engine host)",
        required=False,
    )


def run_liveness_checks() -> HealthReport:
    """Dev/CI: service structural health without requiring live market data."""
    trade_date = _today_compact()
    checks = [
        _mongo_ping_check(),
        _indexes_check(),
        _collections_check(),
        _engine_smoke_check(),
        _internal_key_check(),
    ]
    return HealthReport(mode="liveness", trade_date=trade_date, overall=_rollup(checks), checks=checks)


def run_readiness_checks(
    trade_date: Optional[str] = None,
    user_id: Optional[str] = None,
) -> HealthReport:
    """Prod pre-flight: liveness + upstream realtime pipeline for trade_date."""
    td = compact_trade_date(trade_date)
    report = run_liveness_checks()
    checks = list(report.checks)
    checks.extend(_realtime_quotes_check(td))
    checks.append(_market_spectrum_check(td))
    checks.append(_signals_check(td, user_id))
    checks.append(_position_sources_check(user_id))
    checks.append(_trigger_config_check())
    return HealthReport(mode="readiness", trade_date=td, overall=_rollup(checks), checks=checks)
