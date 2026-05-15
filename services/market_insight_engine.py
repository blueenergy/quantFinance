from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from pymongo import ASCENDING, DESCENDING

from db import get_data_db, get_db_collections
from utils.stock_utils import serialize_doc


EVENT_TYPES = {
    "MARKET_BREADTH_RECOVERY",
    "MARKET_BREADTH_DIVERGENCE",
    "EARLY_SECTOR_DIFFUSION",
    "SECTOR_DIFFUSION",
    "SECTOR_FALSE_STRENGTH",
    "SECTOR_FADE",
    "VOLUME_QUALITY_SHIFT",
}


def _compact_trade_date(raw: Optional[str] = None) -> str:
    if raw:
        td = str(raw).strip().replace("-", "")[:8]
        if len(td) == 8 and td.isdigit():
            return td
    return datetime.now().strftime("%Y%m%d")


def _num(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _snapshot_minus(snapshot_time: str, minutes: int) -> str:
    try:
        dt = datetime.strptime(str(snapshot_time), "%Y%m%d%H%M")
    except ValueError:
        return str(snapshot_time)
    return (dt - timedelta(minutes=minutes)).strftime("%Y%m%d%H%M")


def _latest_spectrum(trade_date: str) -> Dict[str, Any]:
    return (
        get_data_db()["market_spectrum_realtime_daily"].find_one(
            {"snapshot_trade_date": trade_date},
            {"_id": 0},
            sort=[("snapshot_time", DESCENDING)],
        )
        or {}
    )


def _previous_spectrum(trade_date: str, snapshot_time: str, minutes: int = 15) -> Dict[str, Any]:
    target = _snapshot_minus(snapshot_time, minutes)
    return (
        get_data_db()["market_spectrum_realtime_daily"].find_one(
            {"snapshot_trade_date": trade_date, "snapshot_time": {"$lte": target}},
            {"_id": 0},
            sort=[("snapshot_time", DESCENDING)],
        )
        or {}
    )


def _latest_sector_snapshot_time(trade_date: str) -> Optional[str]:
    doc = get_data_db()["realtime_sector_strength"].find_one(
        {"trade_date": trade_date},
        {"_id": 0, "snapshot_time": 1},
        sort=[("snapshot_time", DESCENDING)],
    )
    return doc.get("snapshot_time") if doc else None


def _previous_sector_doc(doc: Dict[str, Any], minutes: int = 15) -> Dict[str, Any]:
    target = _snapshot_minus(str(doc.get("snapshot_time") or ""), minutes)
    return (
        get_data_db()["realtime_sector_strength"].find_one(
            {
                "trade_date": doc.get("trade_date"),
                "level": doc.get("level"),
                "sector_code": doc.get("sector_code"),
                "snapshot_time": {"$lte": target},
            },
            {"_id": 0},
            sort=[("snapshot_time", DESCENDING)],
        )
        or {}
    )


def _sector_doc(trade_date: str, snapshot_time: str, level: str, sector_code: str) -> Dict[str, Any]:
    return (
        get_data_db()["realtime_sector_strength"].find_one(
            {
                "trade_date": trade_date,
                "snapshot_time": snapshot_time,
                "level": level,
                "sector_code": sector_code,
            },
            {"_id": 0},
        )
        or {}
    )


def _sector_parent_codes(doc: Dict[str, Any]) -> Dict[str, str]:
    """Resolve SW parent hierarchy for an L3 sector from member mapping."""
    if doc.get("level") != "L3":
        return {}
    data_cols = get_data_db()
    member_col = data_cols.get("sw_index_member")
    if member_col is None:
        return {}
    member = member_col.find_one(
        {"src": "SW2021", "is_new": "Y", "l3_code": doc.get("sector_code")},
        {"_id": 0, "l1_code": 1, "l1_name": 1, "l2_code": 1, "l2_name": 1, "l3_code": 1, "l3_name": 1},
    )
    if not member:
        return {}
    return {
        "l1_code": str(member.get("l1_code") or ""),
        "l1_name": str(member.get("l1_name") or ""),
        "l2_code": str(member.get("l2_code") or ""),
        "l2_name": str(member.get("l2_name") or ""),
    }


def _event_id(event: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "event_type": event["event_type"],
        "trade_date": event["trade_date"],
        "snapshot_time": event["snapshot_time"],
        "subject_type": event["subject_type"],
        "subject_code": event.get("subject_code", ""),
    }


def _upsert_events(events: List[Dict[str, Any]]) -> int:
    if not events:
        return 0
    coll = get_db_collections()["market_insight_events"]
    now = time.time()
    for event in events:
        event["created_at"] = event.get("created_at") or now
        event["updated_at"] = now
        coll.update_one(_event_id(event), {"$set": event, "$setOnInsert": {"first_seen_at": now}}, upsert=True)
    coll.create_index(
        [("trade_date", DESCENDING), ("snapshot_time", DESCENDING), ("event_type", ASCENDING)],
        name="trade_time_type_idx",
        background=True,
    )
    coll.create_index(
        [("event_type", ASCENDING), ("subject_code", ASCENDING), ("snapshot_time", DESCENDING)],
        name="type_subject_time_idx",
        background=True,
    )
    return len(events)


def generate_market_insight_events(trade_date: Optional[str] = None) -> Dict[str, Any]:
    td = _compact_trade_date(trade_date)
    spectrum = _latest_spectrum(td)
    events: List[Dict[str, Any]] = []
    snapshot_time = spectrum.get("snapshot_time") or _latest_sector_snapshot_time(td)
    if not snapshot_time:
        return {"success": False, "trade_date": td, "error": "No realtime data", "written": 0, "events": []}

    yang_now = _num(spectrum.get("yang_spectrum"))
    prev_spectrum = _previous_spectrum(td, str(snapshot_time), 15) if spectrum else {}
    yang_prev = _num(prev_spectrum.get("yang_spectrum"))
    if yang_now is not None and yang_prev is not None:
        delta = yang_now - yang_prev
        if delta >= 0.05:
            events.append(
                {
                    "event_type": "MARKET_BREADTH_RECOVERY",
                    "severity": "medium" if delta < 0.10 else "high",
                    "confidence": min(95.0, 60.0 + delta * 300),
                    "title": "全市场广度出现修复",
                    "summary": f"全市场阳谱 15 分钟内从 {yang_prev:.1%} 回升至 {yang_now:.1%}。",
                    "trade_date": td,
                    "snapshot_time": str(snapshot_time),
                    "subject_type": "market",
                    "subject_code": "all",
                    "subject_name": "全市场",
                    "evidence": {
                        "yang_spectrum_now": yang_now,
                        "yang_spectrum_15m_ago": yang_prev,
                        "delta_yang_spectrum_15m": delta,
                    },
                    "supporting_points": ["阳谱短时间明显回升", "市场内部上涨家数正在扩散"],
                    "counter_points": [],
                    "watch_conditions": ["观察未来 15 分钟阳谱是否继续维持或上行"],
                }
            )
        elif delta <= -0.05 and yang_now < 0.45:
            events.append(
                {
                    "event_type": "MARKET_BREADTH_DIVERGENCE",
                    "severity": "medium",
                    "confidence": min(90.0, 55.0 + abs(delta) * 280),
                    "title": "全市场广度走弱",
                    "summary": f"全市场阳谱 15 分钟内从 {yang_prev:.1%} 降至 {yang_now:.1%}，需警惕指数掩盖分化。",
                    "trade_date": td,
                    "snapshot_time": str(snapshot_time),
                    "subject_type": "market",
                    "subject_code": "all",
                    "subject_name": "全市场",
                    "evidence": {
                        "yang_spectrum_now": yang_now,
                        "yang_spectrum_15m_ago": yang_prev,
                        "delta_yang_spectrum_15m": delta,
                    },
                    "supporting_points": ["阳谱短时间明显下行", "市场广度低于中性区间"],
                    "counter_points": ["未接入指数实时涨跌，暂不能完全判定虚涨"],
                    "watch_conditions": ["观察指数是否仍维持强势但阳谱继续走弱"],
                }
            )

    sector_time = _latest_sector_snapshot_time(td)
    if sector_time:
        cursor = get_data_db()["realtime_sector_strength"].find(
            {"trade_date": td, "snapshot_time": sector_time, "level": {"$in": ["L2", "L3"]}},
            {"_id": 0},
        )
        for doc in cursor:
            prev = _previous_sector_doc(doc, 15)
            ratio_now = _num(doc.get("positive_ratio"))
            ratio_prev = _num(prev.get("positive_ratio"))
            rel_now = _num(doc.get("relative_positive_ratio"), 0.0) or 0.0
            avg_trade = _num(doc.get("avg_trade_size_yuan"))
            avg_pct_chg = _num(doc.get("avg_pct_chg"))
            if ratio_now is None or ratio_prev is None:
                continue
            delta = ratio_now - ratio_prev
            subject = str(doc.get("sector_name") or doc.get("sector_code"))
            base = {
                "trade_date": td,
                "snapshot_time": str(doc.get("snapshot_time")),
                "subject_type": "sector",
                "subject_code": str(doc.get("sector_code") or ""),
                "subject_name": subject,
                "level": doc.get("level"),
                "evidence": {
                    "positive_ratio_now": ratio_now,
                    "positive_ratio_15m_ago": ratio_prev,
                    "delta_positive_ratio_15m": delta,
                    "relative_positive_ratio": rel_now,
                    "avg_trade_size_yuan": avg_trade,
                    "avg_pct_chg": avg_pct_chg,
                    "stock_count": doc.get("stock_count"),
                    "market_positive_ratio": doc.get("market_positive_ratio"),
                },
            }
            early_event = _build_early_sector_diffusion_event(
                doc=doc,
                base=base,
                ratio_now=ratio_now,
                ratio_prev=ratio_prev,
                delta=delta,
                rel_now=rel_now,
            )
            if early_event:
                events.append(early_event)
            elif delta >= 0.20 and ratio_now >= 0.55 and rel_now >= 0.08:
                events.append(
                    {
                        **base,
                        "event_type": "SECTOR_DIFFUSION",
                        "severity": "high" if delta >= 0.30 else "medium",
                        "confidence": min(96.0, 58.0 + delta * 100 + max(rel_now, 0) * 80),
                        "title": f"{subject}出现板块扩散",
                        "summary": f"{subject}上涨比例 15 分钟内从 {ratio_prev:.1%} 升至 {ratio_now:.1%}，强于全市场。",
                        "supporting_points": ["板块上涨家数比例快速提升", "板块强度高于全市场"],
                        "counter_points": ["需要继续观察成交质量是否同步增强"],
                        "watch_conditions": ["观察未来 15 分钟 positive_ratio 是否维持在 55% 以上"],
                    }
                )
            elif delta <= -0.20 and ratio_now <= 0.45:
                events.append(
                    {
                        **base,
                        "event_type": "SECTOR_FADE",
                        "severity": "medium",
                        "confidence": min(92.0, 55.0 + abs(delta) * 100),
                        "title": f"{subject}出现退潮迹象",
                        "summary": f"{subject}上涨比例 15 分钟内从 {ratio_prev:.1%} 降至 {ratio_now:.1%}。",
                        "supporting_points": ["板块上涨家数比例快速下降"],
                        "counter_points": ["单次快照可能受短线波动影响"],
                        "watch_conditions": ["观察板块是否继续低于全市场广度"],
                    }
                )
            elif _is_false_strength(
                ratio_now=ratio_now,
                rel_now=rel_now,
                avg_pct_chg=avg_pct_chg,
                avg_trade=avg_trade,
                stock_count=int(doc.get("stock_count") or 0),
            ):
                events.append(
                    {
                        **base,
                        "event_type": "SECTOR_FALSE_STRENGTH",
                        "severity": "medium",
                        "confidence": min(90.0, 58.0 + max(rel_now, 0) * 120 + max(avg_pct_chg or 0, 0) * 4),
                        "title": f"{subject}可能存在假强",
                        "summary": (
                            f"{subject}相对全市场强度为 {rel_now:.1%}，但内部上涨比例仅 {ratio_now:.1%}，"
                            "需警惕少数个股带动的表面强势。"
                        ),
                        "supporting_points": ["板块相对强度为正", "内部上涨家数比例不足", "强势可能集中在少数成分股"],
                        "counter_points": ["如果后续上涨家数扩散，假强信号可能被解除"],
                        "watch_conditions": [
                            "观察 positive_ratio 是否补升至 50% 以上",
                            "观察 median_pct_chg 是否跟随 avg_pct_chg 改善",
                        ],
                    }
                )
            elif avg_trade is not None and avg_trade >= 30000 and abs(delta) < 0.08 and ratio_now >= 0.45:
                events.append(
                    {
                        **base,
                        "event_type": "VOLUME_QUALITY_SHIFT",
                        "severity": "low",
                        "confidence": 62.0,
                        "title": f"{subject}成交质量出现变化",
                        "summary": f"{subject}均笔成交额约 {avg_trade:,.0f} 元，但板块扩散尚未明显。",
                        "supporting_points": ["均笔成交额偏高", "板块尚未过热"],
                        "counter_points": ["上涨比例变化不大，暂不能确认方向"],
                        "watch_conditions": ["观察成交质量是否转化为板块扩散"],
                    }
                )

    written = _upsert_events(events)
    return {"success": True, "trade_date": td, "snapshot_time": str(snapshot_time), "written": written, "events": events}


def _is_false_strength(
    *,
    ratio_now: float,
    rel_now: float,
    avg_pct_chg: Optional[float],
    avg_trade: Optional[float],
    stock_count: int,
) -> bool:
    """Detect relative strength that lacks enough internal participation."""
    if stock_count < 5:
        return False
    if ratio_now >= 0.50:
        return False
    has_relative_strength = rel_now >= 0.10
    has_price_strength = avg_pct_chg is not None and avg_pct_chg >= 0.8
    has_trade_quality = avg_trade is not None and avg_trade >= 25000
    return has_relative_strength and (has_price_strength or has_trade_quality)


def _build_early_sector_diffusion_event(
    *,
    doc: Dict[str, Any],
    base: Dict[str, Any],
    ratio_now: float,
    ratio_prev: float,
    delta: float,
    rel_now: float,
) -> Optional[Dict[str, Any]]:
    """Identify L3 diffusion before its parent sectors have confirmed."""
    if doc.get("level") != "L3":
        return None
    if not (delta >= 0.18 and ratio_now >= 0.58 and rel_now >= 0.10):
        return None
    parents = _sector_parent_codes(doc)
    l2_code = parents.get("l2_code")
    l1_code = parents.get("l1_code")
    if not l2_code:
        return None

    trade_date = str(doc.get("trade_date") or "")
    snapshot_time = str(doc.get("snapshot_time") or "")
    l2_doc = _sector_doc(trade_date, snapshot_time, "L2", l2_code)
    l1_doc = _sector_doc(trade_date, snapshot_time, "L1", l1_code) if l1_code else {}
    l2_ratio = _num(l2_doc.get("positive_ratio"))
    l2_rel = _num(l2_doc.get("relative_positive_ratio"), 0.0) or 0.0
    l2_prev = _previous_sector_doc(l2_doc, 15) if l2_doc else {}
    l2_prev_ratio = _num(l2_prev.get("positive_ratio"))
    l2_delta = l2_ratio - l2_prev_ratio if l2_ratio is not None and l2_prev_ratio is not None else None

    parent_confirmed = (
        l2_ratio is not None
        and l2_ratio >= 0.55
        and l2_rel >= 0.08
        and (l2_delta is None or l2_delta >= 0.12)
    )
    if parent_confirmed:
        return None

    subject = str(doc.get("sector_name") or doc.get("sector_code"))
    l2_name = parents.get("l2_name") or str(l2_doc.get("sector_name") or "")
    evidence = {
        **base["evidence"],
        "parent_l2_code": l2_code,
        "parent_l2_name": l2_name,
        "parent_l2_positive_ratio": l2_ratio,
        "parent_l2_relative_positive_ratio": l2_rel,
        "parent_l2_delta_positive_ratio_15m": l2_delta,
        "parent_l1_code": l1_code,
        "parent_l1_name": parents.get("l1_name") or str(l1_doc.get("sector_name") or ""),
        "parent_l1_positive_ratio": _num(l1_doc.get("positive_ratio")),
        "parent_confirmed": parent_confirmed,
    }
    return {
        **base,
        "event_type": "EARLY_SECTOR_DIFFUSION",
        "severity": "medium",
        "confidence": min(93.0, 62.0 + delta * 90 + max(rel_now, 0) * 70),
        "title": f"{subject}出现早期扩散",
        "summary": (
            f"{subject}上涨比例 15 分钟内从 {ratio_prev:.1%} 升至 {ratio_now:.1%}，"
            f"但上级 {l2_name or l2_code} 尚未同步确认。"
        ),
        "evidence": evidence,
        "supporting_points": ["L3 细分行业上涨家数快速扩散", "强度已经明显高于全市场", "上级 L2/L1 尚未充分扩散"],
        "counter_points": ["早期扩散可能回落，需要父级行业后续确认"],
        "watch_conditions": [
            "观察上级 L2 的上涨比例是否跟随突破 55%",
            "观察该 L3 的 positive_ratio 是否继续维持在 58% 以上",
        ],
    }


def get_market_insight_overview(trade_date: Optional[str] = None) -> Dict[str, Any]:
    td = _compact_trade_date(trade_date)
    spectrum = _latest_spectrum(td)
    latest_event = get_db_collections()["market_insight_events"].find_one(
        {"trade_date": td}, {"_id": 0, "snapshot_time": 1}, sort=[("snapshot_time", DESCENDING)]
    )
    event_count = get_db_collections()["market_insight_events"].count_documents({"trade_date": td})
    yang = _num(spectrum.get("yang_spectrum"))
    state = "unknown"
    if yang is not None:
        if yang >= 0.55:
            state = "修复/强势"
        elif yang <= 0.35:
            state = "退潮/弱势"
        else:
            state = "分化/震荡"
    return {
        "success": True,
        "trade_date": td,
        "market_state": state,
        "latest_spectrum": spectrum,
        "latest_event_snapshot_time": (latest_event or {}).get("snapshot_time"),
        "event_count": event_count,
    }


def list_market_insight_events(
    trade_date: Optional[str] = None,
    event_type: Optional[str] = None,
    level: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    query: Dict[str, Any] = {}
    if trade_date:
        query["trade_date"] = _compact_trade_date(trade_date)
    if event_type:
        query["event_type"] = event_type
    if level:
        query["level"] = level
    cursor = (
        get_db_collections()["market_insight_events"]
        .find(query)
        .sort([("snapshot_time", DESCENDING), ("confidence", DESCENDING)])
        .limit(limit)
    )
    return [serialize_doc(doc) for doc in cursor]


def get_sector_strength(level: str = "L2", trade_date: Optional[str] = None, window: int = 30, limit: int = 50) -> Dict[str, Any]:
    td = _compact_trade_date(trade_date)
    latest_time = _latest_sector_snapshot_time(td)
    if not latest_time:
        return {"success": True, "trade_date": td, "snapshot_time": None, "data": []}
    target = _snapshot_minus(latest_time, window)
    current_rows = list(
        get_data_db()["realtime_sector_strength"]
        .find({"trade_date": td, "snapshot_time": latest_time, "level": level}, {"_id": 0})
        .sort("relative_positive_ratio", DESCENDING)
        .limit(limit)
    )
    prev_by_code: Dict[str, Dict[str, Any]] = {}
    prev_cursor = get_data_db()["realtime_sector_strength"].find(
        {"trade_date": td, "snapshot_time": {"$lte": target}, "level": level}, {"_id": 0}
    ).sort("snapshot_time", DESCENDING)
    for doc in prev_cursor:
        code = doc.get("sector_code")
        if code and code not in prev_by_code:
            prev_by_code[code] = doc
    for row in current_rows:
        prev = prev_by_code.get(row.get("sector_code")) or {}
        now_ratio = _num(row.get("positive_ratio"))
        prev_ratio = _num(prev.get("positive_ratio"))
        row["delta_positive_ratio"] = now_ratio - prev_ratio if now_ratio is not None and prev_ratio is not None else None
        row["window_minutes"] = window
    return {"success": True, "trade_date": td, "snapshot_time": latest_time, "data": current_rows}


def get_sector_strength_series(
    *,
    level: str,
    sector_code: str,
    trade_date: Optional[str] = None,
    limit: int = 120,
    simulate_if_sparse: bool = False,
) -> Dict[str, Any]:
    """Return intraday sector strength snapshots for a single sector."""
    td = _compact_trade_date(trade_date)
    query = {"trade_date": td, "level": level, "sector_code": sector_code}
    cursor = (
        get_data_db()["realtime_sector_strength"]
        .find(query, {"_id": 0})
        .sort("snapshot_time", DESCENDING)
        .limit(limit)
    )
    rows = list(reversed([serialize_doc(doc) for doc in cursor]))
    previous = None
    for row in rows:
        current = _num(row.get("positive_ratio"))
        row["delta_from_previous"] = current - previous if current is not None and previous is not None else None
        if current is not None:
            previous = current
    is_simulated = False
    if simulate_if_sparse and 0 < len(rows) < 2:
        rows = _simulate_sector_strength_series(rows[0], limit=min(max(limit, 6), 12))
        is_simulated = True
    return {
        "success": True,
        "trade_date": td,
        "level": level,
        "sector_code": sector_code,
        "count": len(rows),
        "is_simulated": is_simulated,
        "data": rows,
    }


def _simulate_sector_strength_series(seed: Dict[str, Any], limit: int = 8) -> List[Dict[str, Any]]:
    """Build a non-persistent trend series when weekend/off-hours data is sparse."""
    snapshot_time = str(seed.get("snapshot_time") or "")
    try:
        end_dt = datetime.strptime(snapshot_time, "%Y%m%d%H%M")
    except ValueError:
        end_dt = datetime.now()
    current_ratio = _num(seed.get("positive_ratio"), 0.5) or 0.5
    relative = _num(seed.get("relative_positive_ratio"), 0.0) or 0.0
    direction = 1 if relative >= 0 else -1
    amplitude = min(0.18, max(0.06, abs(relative) * 0.8))
    points = max(int(limit or 8), 2)
    rows: List[Dict[str, Any]] = []
    previous = None
    for index in range(points):
        progress = index / (points - 1)
        ratio = current_ratio - direction * amplitude * (1 - progress)
        ratio = max(0.0, min(1.0, ratio))
        row = {
            **seed,
            "snapshot_time": (end_dt - timedelta(minutes=(points - 1 - index) * 5)).strftime("%Y%m%d%H%M"),
            "positive_ratio": ratio,
            "delta_from_previous": ratio - previous if previous is not None else None,
            "simulated": True,
        }
        previous = ratio
        rows.append(row)
    return rows
