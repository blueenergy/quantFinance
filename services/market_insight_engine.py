from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from pymongo import ASCENDING, DESCENDING

from db import get_data_db, get_db_collections
from utils.stock_utils import serialize_doc
from utils.symbol_utils import normalize_symbol


EVENT_TYPES = {
    "MARKET_BREADTH_RECOVERY",
    "MARKET_BREADTH_DIVERGENCE",
    "COUNTER_TREND_SECTOR_STRENGTH",
    "EARLY_SECTOR_DIFFUSION",
    "SECTOR_DIFFUSION",
    "SECTOR_FALSE_STRENGTH",
    "SECTOR_FADE",
    "VOLUME_QUALITY_SHIFT",
}

EVENT_TYPE_METADATA: Dict[str, Dict[str, Any]] = {
    "MARKET_BREADTH_RECOVERY": {
        "label": "广度修复",
        "category": "市场状态类",
        "meaning": "全市场阳谱在短时间内回升，说明上涨家数正在从低位修复。",
        "why_it_matters": "它反映的是市场内部参与度改善，而不只是指数价格上涨。",
        "evidence_guide": ["重点看 yang_spectrum_now", "比较 delta_yang_spectrum_15m 是否持续为正"],
        "false_positive": "如果修复只持续一两个快照，可能只是盘中反抽。",
        "next_watch": ["观察未来 15 分钟阳谱是否继续维持或上行", "结合板块扩散事件判断修复来源"],
    },
    "MARKET_BREADTH_DIVERGENCE": {
        "label": "广度走弱",
        "category": "市场状态类",
        "meaning": "全市场阳谱短时间下降，市场内部上涨家数减少。",
        "why_it_matters": "它可以提醒用户指数表面稳定时，内部结构可能已经变弱。",
        "evidence_guide": ["重点看 yang_spectrum_now 是否低于中性区", "看 delta_yang_spectrum_15m 是否明显为负"],
        "false_positive": "午间或尾盘流动性变化可能放大短时波动。",
        "next_watch": ["观察指数是否与阳谱背离", "观察是否有逆势扩散板块出现"],
    },
    "SECTOR_DIFFUSION": {
        "label": "板块扩散",
        "category": "板块扩散类",
        "meaning": "某个板块内部上涨家数快速增加，并且强于全市场。",
        "why_it_matters": "扩散说明资金不只集中在少数个股，板块内部共振更真实。",
        "evidence_guide": ["看 positive_ratio_now", "看 delta_positive_ratio_15m", "看 relative_positive_ratio"],
        "false_positive": "如果成交质量没有同步增强，可能只是短线轮动。",
        "next_watch": ["观察 positive_ratio 是否维持在 55% 以上", "观察 L1/L2/L3 是否相互确认"],
    },
    "SECTOR_FADE": {
        "label": "板块退潮",
        "category": "板块扩散类",
        "meaning": "板块内部上涨家数快速下降，扩散状态开始收缩。",
        "why_it_matters": "它提示板块热度或承接开始减弱，避免只看少数仍上涨个股。",
        "evidence_guide": ["看 positive_ratio_now 是否跌破中性区", "看 delta_positive_ratio_15m 是否明显为负"],
        "false_positive": "强势板块盘中洗盘也可能造成短时退潮。",
        "next_watch": ["观察是否重新扩散", "观察相对全市场强度是否继续下滑"],
    },
    "EARLY_SECTOR_DIFFUSION": {
        "label": "早期扩散",
        "category": "板块扩散类",
        "meaning": "L3 细分行业已经开始扩散，但所属 L2/L1 大行业还没有同步确认。",
        "why_it_matters": "这可能是结构变化的早期信号，说明资金先在更细分方向试探。",
        "evidence_guide": ["看 L3 positive_ratio", "看 parent_l2_positive_ratio", "看 parent_confirmed 是否为 false"],
        "false_positive": "如果父级行业迟迟不扩散，可能只是细分方向的短线脉冲。",
        "next_watch": ["观察父级 L2 上涨比例是否突破 55%", "观察 L3 是否继续维持强度"],
    },
    "COUNTER_TREND_SECTOR_STRENGTH": {
        "label": "逆势扩散",
        "category": "板块扩散类",
        "meaning": "全市场广度偏弱时，某个局部板块仍然逆势扩散。",
        "why_it_matters": "它帮助用户发现弱市中仍有资金聚集的方向，可能是真强候选。",
        "evidence_guide": ["看 market_weak", "看 market_yang_spectrum", "看板块 positive_ratio 和 relative_positive_ratio"],
        "false_positive": "逆势扩散可能是短线避险轮动，不一定能持续。",
        "next_watch": ["观察全市场弱势是否持续", "观察该板块能否继续扩散"],
    },
    "SECTOR_FALSE_STRENGTH": {
        "label": "板块假强",
        "category": "去妄存真类",
        "meaning": "板块看起来强，但内部上涨家数不足，可能只是少数权重股或核心股撑起来。",
        "why_it_matters": "它帮助用户识别表面强势和真实扩散之间的差异。",
        "evidence_guide": ["看 positive_ratio_now 是否低于 50%", "比较 avg_pct_chg 和 median_pct_chg", "看 relative_positive_ratio"],
        "false_positive": "强势初期也可能先由龙头带动，后续扩散后会转为真强。",
        "next_watch": ["观察上涨家数是否补升到 50% 以上", "观察中位涨幅是否改善"],
    },
    "VOLUME_QUALITY_SHIFT": {
        "label": "成交质量变化",
        "category": "质量识别类",
        "meaning": "板块均笔成交额等质量指标出现变化，但方向尚未完全确认。",
        "why_it_matters": "它提示资金行为可能正在变化，适合作为后续扩散的观察线索。",
        "evidence_guide": ["看 avg_trade_size_yuan", "结合 positive_ratio 判断是否转化为扩散"],
        "false_positive": "大单成交可能来自个别股票，不能单独作为方向信号。",
        "next_watch": ["观察成交质量是否转化为板块扩散", "观察价格和上涨家数是否跟随"],
    },
}

EVENT_ATTENTION_WEIGHTS = {
    "COUNTER_TREND_SECTOR_STRENGTH": 18.0,
    "EARLY_SECTOR_DIFFUSION": 16.0,
    "SECTOR_FALSE_STRENGTH": 14.0,
    "MARKET_BREADTH_DIVERGENCE": 12.0,
    "SECTOR_DIFFUSION": 10.0,
    "MARKET_BREADTH_RECOVERY": 8.0,
    "SECTOR_FADE": 8.0,
    "VOLUME_QUALITY_SHIFT": 4.0,
}

SEVERITY_ATTENTION_WEIGHTS = {"high": 12.0, "medium": 6.0, "low": 2.0}
RELEVANCE_SOURCE_WEIGHTS = {"real_position": 18.0, "mock_position": 14.0, "watchlist": 10.0}
RELEVANCE_SOURCE_LABELS = {
    "real_position": "命中真实持仓",
    "mock_position": "命中T+0模拟仓",
    "watchlist": "命中自选股",
}
FOCUS_FEEDBACK_ACTIONS = {"valuable", "ignore", "false_positive"}
FOCUS_FEEDBACK_LABELS = {
    "valuable": "有价值",
    "ignore": "忽略",
    "false_positive": "误报",
}
FOCUS_FEEDBACK_WEIGHTS = {"valuable": 8.0, "ignore": -12.0, "false_positive": -18.0}
FOCUS_REVIEW_LABELS = {
    "confirmed": "已验证",
    "faded": "反证",
    "unconfirmed": "未验证",
    "insufficient_data": "待观察",
}
PREFERENCE_FEEDBACK_DELTAS = {
    "valuable": {"valuable_count": 1},
    "ignore": {"ignore_count": 1},
    "false_positive": {"false_positive_count": 1},
}
PREFERENCE_REVIEW_DELTAS = {
    "confirmed": {"confirmed_count": 1},
    "faded": {"faded_count": 1},
}
PREFERENCE_COUNT_FIELDS = (
    "valuable_count",
    "ignore_count",
    "false_positive_count",
    "confirmed_count",
    "faded_count",
)
PREFERENCE_DIMENSION_WEIGHTS = {"event_type": 1.0, "category": 0.6, "subject_type": 0.4}
MAX_PREFERENCE_WEIGHT = 5.0
QUALITY_REVIEW_STATUSES = ("confirmed", "faded", "unconfirmed", "insufficient_data")
QUALITY_DIMENSION_WEIGHTS = {"event_type": 1.0, "category": 0.6, "subject_type": 0.3}
MAX_GLOBAL_QUALITY_WEIGHT = 4.0
GLOBAL_QUALITY_MIN_DECISIVE_COUNT = 3


def get_event_type_metadata() -> Dict[str, Dict[str, Any]]:
    return EVENT_TYPE_METADATA


def get_event_categories() -> List[str]:
    return sorted({str(item.get("category")) for item in EVENT_TYPE_METADATA.values() if item.get("category")})


def _event_types_for_category(category: str) -> List[str]:
    return sorted(
        event_type
        for event_type, metadata in EVENT_TYPE_METADATA.items()
        if metadata.get("category") == category
    )


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


def _snapshot_plus(snapshot_time: str, minutes: int) -> str:
    try:
        dt = datetime.strptime(str(snapshot_time), "%Y%m%d%H%M")
    except ValueError:
        return str(snapshot_time)
    return (dt + timedelta(minutes=minutes)).strftime("%Y%m%d%H%M")


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
    market_delta = yang_now - yang_prev if yang_now is not None and yang_prev is not None else None
    market_weak = (
        yang_now is not None
        and (
            yang_now < 0.45
            or (market_delta is not None and market_delta <= -0.03)
        )
    )
    if yang_now is not None and yang_prev is not None:
        delta = market_delta
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
            elif _is_counter_trend_sector_strength(
                market_weak=market_weak,
                ratio_now=ratio_now,
                delta=delta,
                rel_now=rel_now,
            ):
                events.append(
                    {
                        **base,
                        "event_type": "COUNTER_TREND_SECTOR_STRENGTH",
                        "severity": "high",
                        "confidence": min(94.0, 62.0 + delta * 100 + max(rel_now, 0) * 80),
                        "title": f"{subject}出现逆势扩散",
                        "summary": (
                            f"全市场广度偏弱时，{subject}上涨比例 15 分钟内从 {ratio_prev:.1%} "
                            f"升至 {ratio_now:.1%}，相对全市场强度为 {rel_now:.1%}。"
                        ),
                        "evidence": {
                            **base["evidence"],
                            "market_yang_spectrum": yang_now,
                            "market_delta_yang_spectrum_15m": market_delta,
                            "market_weak": market_weak,
                        },
                        "supporting_points": ["全市场广度偏弱", "局部板块仍在扩散", "板块强度明显高于全市场"],
                        "counter_points": ["逆势扩散也可能是短线避险轮动，需要后续确认"],
                        "watch_conditions": [
                            "观察全市场弱势是否持续",
                            "观察该板块 positive_ratio 是否继续维持在 58% 以上",
                        ],
                    }
                )
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


def _is_counter_trend_sector_strength(
    *,
    market_weak: bool,
    ratio_now: float,
    delta: float,
    rel_now: float,
) -> bool:
    return market_weak and ratio_now >= 0.58 and delta >= 0.15 and rel_now >= 0.15


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
    category: Optional[str] = None,
    severity: Optional[str] = None,
    min_confidence: Optional[float] = None,
    level: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    query: Dict[str, Any] = {}
    if trade_date:
        query["trade_date"] = _compact_trade_date(trade_date)
    if category:
        category_event_types = _event_types_for_category(category)
        if not category_event_types:
            return []
        if event_type and event_type not in category_event_types:
            return []
        query["event_type"] = event_type or {"$in": category_event_types}
    if event_type:
        query["event_type"] = event_type
    if severity:
        query["severity"] = severity
    if min_confidence is not None:
        query["confidence"] = {"$gte": min_confidence}
    if level:
        query["level"] = level
    cursor = (
        get_db_collections()["market_insight_events"]
        .find(query)
        .sort([("snapshot_time", DESCENDING), ("confidence", DESCENDING)])
        .limit(limit)
    )
    return [serialize_doc(doc) for doc in cursor]


def get_market_insight_focus(
    trade_date: Optional[str] = None,
    limit: int = 5,
    user_id: Optional[str] = None,
) -> Dict[str, Any]:
    td = _compact_trade_date(trade_date)
    rows = list_market_insight_events(trade_date=td, limit=200)
    if not rows:
        return {"success": True, "trade_date": td, "count": 0, "data": []}

    latest_snapshot = max(str(row.get("snapshot_time") or "") for row in rows)
    relevance_context = _build_user_relevance_context(user_id)
    feedback_context = _build_focus_feedback_context(user_id, td)
    review_context = _build_focus_review_context(td)
    preference_context = _build_user_preference_context(user_id)
    quality_context = _build_global_quality_context()
    by_subject: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        focus = _build_focus_item(
            row,
            latest_snapshot,
            relevance_context,
            feedback_context,
            review_context,
            preference_context,
            quality_context,
        )
        key = _focus_subject_key(row)
        current = by_subject.get(key)
        if not current or focus["attention_score"] > current["attention_score"]:
            by_subject[key] = focus
        else:
            current["related_event_count"] += 1
            current["related_event_types"] = sorted(
                set(current["related_event_types"] + [str(row.get("event_type") or "")])
            )

    focus_items = sorted(
        by_subject.values(),
        key=lambda item: (item["attention_score"], item["primary_event"].get("snapshot_time") or ""),
        reverse=True,
    )[:limit]
    return {"success": True, "trade_date": td, "count": len(focus_items), "data": focus_items}


def _focus_subject_key(event: Dict[str, Any]) -> str:
    subject_type = str(event.get("subject_type") or "")
    subject_code = str(event.get("subject_code") or event.get("subject_name") or "")
    if subject_type == "market":
        return "market:all"
    return f"{subject_type}:{subject_code}"


def _build_focus_item(
    event: Dict[str, Any],
    latest_snapshot: str,
    relevance_context: Optional[Dict[str, Any]] = None,
    feedback_context: Optional[Dict[str, Any]] = None,
    review_context: Optional[Dict[str, Any]] = None,
    preference_context: Optional[Dict[str, Any]] = None,
    quality_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    event_type = str(event.get("event_type") or "")
    confidence = _num(event.get("confidence"), 0.0) or 0.0
    severity = str(event.get("severity") or "")
    type_weight = EVENT_ATTENTION_WEIGHTS.get(event_type, 0.0)
    severity_weight = SEVERITY_ATTENTION_WEIGHTS.get(severity, 0.0)
    recency_weight = _recency_attention_weight(str(event.get("snapshot_time") or ""), latest_snapshot)
    subject_weight = 4.0 if event.get("subject_type") == "sector" else 2.0
    relevance = _event_user_relevance(event, relevance_context or {})
    relevance_weight = _num(relevance.get("score"), 0.0) or 0.0
    focus_key = _focus_event_key(event)
    feedback = _focus_feedback_for_key(focus_key, feedback_context or {})
    review = _focus_review_for_key(focus_key, review_context or {})
    feedback_weight = FOCUS_FEEDBACK_WEIGHTS.get(str(feedback.get("action") or ""), 0.0)
    preference_weight = _event_preference_weight(event, preference_context or {})
    global_quality_weight = _event_global_quality_weight(event, quality_context or {})
    attention_score = min(
        100.0,
        max(
            0.0,
            confidence * 0.55
            + type_weight
            + severity_weight
            + recency_weight
            + subject_weight
            + relevance_weight
            + feedback_weight
            + preference_weight
            + global_quality_weight,
        ),
    )
    metadata = EVENT_TYPE_METADATA.get(event_type, {})
    return {
        "focus_key": focus_key,
        "attention_score": round(attention_score, 1),
        "subject_name": event.get("subject_name") or "全市场",
        "event_label": metadata.get("label") or event_type,
        "category": metadata.get("category") or "",
        "title": event.get("title") or metadata.get("label") or event_type,
        "summary": event.get("summary") or metadata.get("meaning") or "",
        "why_focus": _focus_reason(event, metadata, attention_score, preference_weight, global_quality_weight),
        "next_action": _focus_next_action(event, metadata),
        "score_breakdown": {
            "confidence_component": round(confidence * 0.55, 1),
            "event_type_weight": type_weight,
            "severity_weight": severity_weight,
            "recency_weight": recency_weight,
            "subject_weight": subject_weight,
            "user_relevance_weight": relevance_weight,
            "feedback_weight": feedback_weight,
            "preference_weight": preference_weight,
            "global_quality_weight": global_quality_weight,
        },
        "relevance": relevance,
        "feedback": feedback,
        "review": review,
        "related_event_count": 1,
        "related_event_types": [event_type],
        "primary_event": event,
    }


def _recency_attention_weight(snapshot_time: str, latest_snapshot: str) -> float:
    if snapshot_time and latest_snapshot and snapshot_time == latest_snapshot:
        return 8.0
    return 3.0


def _focus_reason(
    event: Dict[str, Any],
    metadata: Dict[str, Any],
    attention_score: float,
    preference_weight: float = 0.0,
    global_quality_weight: float = 0.0,
) -> str:
    if preference_weight >= 4.0:
        return "结合您过往反馈，这类焦点更符合您的关注偏好。"
    if preference_weight <= -4.0:
        return "该焦点客观分较高，但同类内容曾被您多次降权，建议快速确认即可。"
    if global_quality_weight >= 3.0:
        return "同类焦点在历史盘后验证中表现较好，值得优先确认。"
    if global_quality_weight <= -3.0:
        return "同类焦点历史验证质量偏弱，建议结合证据链谨慎判断。"
    event_type = event.get("event_type")
    if event_type == "COUNTER_TREND_SECTOR_STRENGTH":
        return "弱市里仍然逆势扩散，属于稀缺且更值得优先确认的方向。"
    if event_type == "EARLY_SECTOR_DIFFUSION":
        return "细分方向先于父级行业启动，可能是结构变化的早期线索。"
    if event_type == "SECTOR_FALSE_STRENGTH":
        return "表面强势和内部参与度不一致，适合优先排除假强噪音。"
    if event_type == "MARKET_BREADTH_DIVERGENCE":
        return "市场内部走弱会影响其他板块信号的可靠性，需要先校准大环境。"
    if attention_score >= 80:
        return "置信度、事件重要性和新鲜度共同靠前，适合作为今日优先观察项。"
    return metadata.get("why_it_matters") or "该事件对理解当前市场结构有参考价值。"


def _focus_next_action(event: Dict[str, Any], metadata: Dict[str, Any]) -> str:
    watch = event.get("watch_conditions") or metadata.get("next_watch") or []
    if watch:
        return str(watch[0])
    return "点开证据链，确认支持证据和反向证据是否匹配。"


def _focus_event_key(event: Dict[str, Any]) -> str:
    return "|".join(
        [
            str(event.get("trade_date") or ""),
            str(event.get("snapshot_time") or ""),
            str(event.get("event_type") or ""),
            str(event.get("subject_type") or ""),
            str(event.get("subject_code") or event.get("subject_name") or ""),
        ]
    )


def _preference_dimensions_from_focus_key(focus_key: str) -> Dict[str, str]:
    parts = str(focus_key or "").split("|")
    if len(parts) < 5:
        return {}
    event_type = parts[2]
    subject_type = parts[3]
    metadata = EVENT_TYPE_METADATA.get(event_type, {})
    dimensions = {
        "event_type": event_type,
        "subject_type": subject_type,
    }
    if metadata.get("category"):
        dimensions["category"] = str(metadata["category"])
    return {dimension: key for dimension, key in dimensions.items() if key}


def _preference_dimensions_for_event(event: Dict[str, Any]) -> Dict[str, str]:
    event_type = str(event.get("event_type") or "")
    subject_type = str(event.get("subject_type") or "")
    metadata = EVENT_TYPE_METADATA.get(event_type, {})
    dimensions = {
        "event_type": event_type,
        "subject_type": subject_type,
    }
    if metadata.get("category"):
        dimensions["category"] = str(metadata["category"])
    return {dimension: key for dimension, key in dimensions.items() if key}


def _calculate_user_preference_score(counts: Dict[str, Any]) -> float:
    return round(
        (_num(counts.get("valuable_count"), 0.0) or 0.0) * 2.0
        - (_num(counts.get("ignore_count"), 0.0) or 0.0) * 1.0
        - (_num(counts.get("false_positive_count"), 0.0) or 0.0) * 3.0
        + (_num(counts.get("confirmed_count"), 0.0) or 0.0) * 1.0
        - (_num(counts.get("faded_count"), 0.0) or 0.0) * 1.0,
        2,
    )


def _merge_preference_deltas(*deltas: Dict[str, int]) -> Dict[str, int]:
    merged = {field: 0 for field in PREFERENCE_COUNT_FIELDS}
    for delta in deltas:
        for field, value in delta.items():
            if field in merged:
                merged[field] += int(value)
    return {field: value for field, value in merged.items() if value}


def _reverse_preference_delta(action_or_status: Optional[str]) -> Dict[str, int]:
    delta = PREFERENCE_FEEDBACK_DELTAS.get(str(action_or_status or ""))
    if delta is None:
        delta = PREFERENCE_REVIEW_DELTAS.get(str(action_or_status or ""), {})
    return {field: -value for field, value in delta.items()}


def _clamp_preference_weight(value: float) -> float:
    return round(max(-MAX_PREFERENCE_WEIGHT, min(MAX_PREFERENCE_WEIGHT, value)), 1)


def _update_user_preference_counts(
    user_id: str,
    dimensions: Dict[str, str],
    deltas: Dict[str, int],
) -> List[Dict[str, Any]]:
    if not user_id or not dimensions or not deltas:
        return []
    coll = get_db_collections().get("market_insight_user_preferences")
    if coll is None:
        return []
    now = time.time()
    updated = []
    for dimension, key in dimensions.items():
        query = {"user_id": user_id, "dimension": dimension, "key": key}
        existing = coll.find_one(query) or {}
        counts = {field: int(_num(existing.get(field), 0.0) or 0) for field in PREFERENCE_COUNT_FIELDS}
        for field, value in deltas.items():
            if field in counts:
                counts[field] = max(0, counts[field] + int(value))
        doc = {
            **query,
            **counts,
            "score": _calculate_user_preference_score(counts),
            "updated_at": now,
        }
        coll.update_one(query, {"$set": doc, "$setOnInsert": {"created_at": now}}, upsert=True)
        updated.append(doc)
    coll.create_index(
        [("user_id", ASCENDING), ("dimension", ASCENDING), ("key", ASCENDING)],
        unique=True,
        name="user_dimension_key_unique",
        background=True,
    )
    return updated


def update_user_preference_from_feedback(
    user_id: str,
    focus_key: str,
    action: str,
    previous_action: Optional[str] = None,
) -> List[Dict[str, Any]]:
    if action not in FOCUS_FEEDBACK_ACTIONS:
        raise ValueError("unsupported feedback action")
    if previous_action == action:
        return []
    dimensions = _preference_dimensions_from_focus_key(focus_key)
    deltas = _merge_preference_deltas(
        _reverse_preference_delta(previous_action),
        PREFERENCE_FEEDBACK_DELTAS.get(action, {}),
    )
    return _update_user_preference_counts(user_id, dimensions, deltas)


def update_user_preference_from_review(focus_key: str, review_status: str) -> int:
    collections = get_db_collections()
    feedback_coll = collections.get("market_insight_focus_feedback")
    if feedback_coll is None:
        return 0
    dimensions = _preference_dimensions_from_focus_key(focus_key)
    if not dimensions:
        return 0
    applied = 0
    for feedback in feedback_coll.find({"focus_key": focus_key}):
        user_id = str(feedback.get("user_id") or "")
        if not user_id:
            continue
        previous_status = str(feedback.get("preference_review_status") or "")
        if previous_status == review_status:
            continue
        deltas = _merge_preference_deltas(
            _reverse_preference_delta(previous_status),
            PREFERENCE_REVIEW_DELTAS.get(review_status, {}),
        )
        if deltas:
            _update_user_preference_counts(user_id, dimensions, deltas)
            applied += 1
        feedback_coll.update_one(
            {"user_id": user_id, "focus_key": focus_key},
            {"$set": {"preference_review_status": review_status, "preference_review_updated_at": time.time()}},
            upsert=False,
        )
    return applied


def _apply_review_preference_to_feedback(review: Dict[str, Any]) -> int:
    return update_user_preference_from_review(
        focus_key=str(review.get("focus_key") or ""),
        review_status=str(review.get("status") or ""),
    )


def _build_user_preference_context(user_id: Optional[str]) -> Dict[str, Dict[str, Any]]:
    if not user_id:
        return {}
    coll = get_db_collections().get("market_insight_user_preferences")
    if coll is None:
        return {}
    context: Dict[str, Dict[str, Any]] = {}
    for doc in coll.find({"user_id": user_id}):
        dimension = str(doc.get("dimension") or "")
        key = str(doc.get("key") or "")
        if not dimension or not key:
            continue
        context[f"{dimension}:{key}"] = {
            "score": _num(doc.get("score"), 0.0) or 0.0,
            "dimension": dimension,
            "key": key,
        }
    return context


def _event_preference_weight(event: Dict[str, Any], preference_context: Dict[str, Dict[str, Any]]) -> float:
    if not preference_context:
        return 0.0
    weighted = 0.0
    for dimension, key in _preference_dimensions_for_event(event).items():
        pref = preference_context.get(f"{dimension}:{key}") or {}
        weighted += (_num(pref.get("score"), 0.0) or 0.0) * PREFERENCE_DIMENSION_WEIGHTS.get(dimension, 0.0)
    return _clamp_preference_weight(weighted)


def _quality_dimensions_for_review(review: Dict[str, Any]) -> Dict[str, str]:
    event_type = str(review.get("event_type") or "")
    subject_type = str(review.get("subject_type") or "")
    metadata = EVENT_TYPE_METADATA.get(event_type, {})
    dimensions = {
        "event_type": event_type,
        "subject_type": subject_type,
    }
    if metadata.get("category"):
        dimensions["category"] = str(metadata["category"])
    return {dimension: key for dimension, key in dimensions.items() if key}


def _calculate_global_quality_score(stats: Dict[str, int]) -> Dict[str, Any]:
    confirmed_count = int(stats.get("confirmed_count", 0))
    faded_count = int(stats.get("faded_count", 0))
    unconfirmed_count = int(stats.get("unconfirmed_count", 0))
    insufficient_data_count = int(stats.get("insufficient_data_count", 0))
    reviewed_count = confirmed_count + faded_count + unconfirmed_count + insufficient_data_count
    decisive_count = confirmed_count + faded_count
    confirmation_rate = round(confirmed_count / decisive_count, 4) if decisive_count else 0.0
    fade_rate = round(faded_count / decisive_count, 4) if decisive_count else 0.0
    reliability = min(1.0, decisive_count / 20.0)
    raw_score = ((confirmation_rate - 0.5) * 8.0 * reliability) if decisive_count else 0.0
    weight = 0.0
    if decisive_count >= GLOBAL_QUALITY_MIN_DECISIVE_COUNT:
        weight = round(max(-MAX_GLOBAL_QUALITY_WEIGHT, min(MAX_GLOBAL_QUALITY_WEIGHT, raw_score)), 1)
    return {
        "reviewed_count": reviewed_count,
        "decisive_count": decisive_count,
        "confirmation_rate": confirmation_rate,
        "fade_rate": fade_rate,
        "score": round(raw_score, 2),
        "weight": weight,
    }


def rebuild_market_insight_global_quality() -> Dict[str, Any]:
    collections = get_db_collections()
    review_coll = collections.get("market_insight_focus_reviews")
    quality_coll = collections.get("market_insight_global_quality")
    if review_coll is None or quality_coll is None:
        return {"success": True, "count": 0, "data": []}

    buckets: Dict[str, Dict[str, Any]] = {}
    for review in review_coll.find({}):
        status = str(review.get("status") or "")
        if status not in QUALITY_REVIEW_STATUSES:
            continue
        for dimension, key in _quality_dimensions_for_review(review).items():
            bucket_key = f"{dimension}:{key}"
            bucket = buckets.setdefault(
                bucket_key,
                {
                    "dimension": dimension,
                    "key": key,
                    "confirmed_count": 0,
                    "faded_count": 0,
                    "unconfirmed_count": 0,
                    "insufficient_data_count": 0,
                    "last_reviewed_at": None,
                },
            )
            count_field = f"{status}_count"
            bucket[count_field] += 1
            checked_at = review.get("checked_at")
            if checked_at is not None and (
                bucket.get("last_reviewed_at") is None or checked_at > bucket.get("last_reviewed_at")
            ):
                bucket["last_reviewed_at"] = checked_at

    now = time.time()
    docs = []
    for bucket in buckets.values():
        metrics = _calculate_global_quality_score(bucket)
        doc = {
            **bucket,
            **metrics,
            "updated_at": now,
        }
        quality_coll.update_one(
            {"dimension": doc["dimension"], "key": doc["key"]},
            {"$set": doc, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )
        docs.append(doc)
    quality_coll.create_index(
        [("dimension", ASCENDING), ("key", ASCENDING)],
        unique=True,
        name="dimension_key_unique",
        background=True,
    )
    quality_coll.create_index([("weight", DESCENDING), ("decisive_count", DESCENDING)], name="quality_rank_idx", background=True)
    docs.sort(key=lambda item: (item.get("weight", 0.0), item.get("decisive_count", 0)), reverse=True)
    return {"success": True, "count": len(docs), "data": docs}


def list_market_insight_global_quality(
    dimension: Optional[str] = None,
    limit: int = 100,
) -> Dict[str, Any]:
    coll = get_db_collections().get("market_insight_global_quality")
    if coll is None:
        return {"success": True, "count": 0, "data": []}
    query: Dict[str, Any] = {}
    if dimension:
        query["dimension"] = dimension
    cursor = coll.find(query).sort([("weight", DESCENDING), ("decisive_count", DESCENDING)]).limit(limit)
    rows = [serialize_doc(doc) for doc in cursor]
    return {"success": True, "count": len(rows), "data": rows}


def _build_global_quality_context() -> Dict[str, Dict[str, Any]]:
    coll = get_db_collections().get("market_insight_global_quality")
    if coll is None:
        return {}
    context: Dict[str, Dict[str, Any]] = {}
    for doc in coll.find({}):
        dimension = str(doc.get("dimension") or "")
        key = str(doc.get("key") or "")
        if not dimension or not key:
            continue
        context[f"{dimension}:{key}"] = {
            "weight": _num(doc.get("weight"), 0.0) or 0.0,
            "dimension": dimension,
            "key": key,
            "decisive_count": int(_num(doc.get("decisive_count"), 0.0) or 0),
        }
    return context


def _event_global_quality_weight(event: Dict[str, Any], quality_context: Dict[str, Dict[str, Any]]) -> float:
    if not quality_context:
        return 0.0
    weighted = 0.0
    for dimension, key in _preference_dimensions_for_event(event).items():
        quality = quality_context.get(f"{dimension}:{key}") or {}
        weighted += (_num(quality.get("weight"), 0.0) or 0.0) * QUALITY_DIMENSION_WEIGHTS.get(dimension, 0.0)
    return round(max(-MAX_GLOBAL_QUALITY_WEIGHT, min(MAX_GLOBAL_QUALITY_WEIGHT, weighted)), 1)


def _build_focus_feedback_context(user_id: Optional[str], trade_date: str) -> Dict[str, Dict[str, Any]]:
    if not user_id:
        return {}
    coll = get_db_collections().get("market_insight_focus_feedback")
    if coll is None:
        return {}
    cursor = coll.find({"user_id": user_id, "trade_date": trade_date})
    return {str(doc.get("focus_key") or ""): _serialize_focus_feedback(doc) for doc in cursor if doc.get("focus_key")}


def _build_focus_review_context(trade_date: str) -> Dict[str, Dict[str, Any]]:
    coll = get_db_collections().get("market_insight_focus_reviews")
    if coll is None:
        return {}
    cursor = coll.find({"trade_date": trade_date})
    return {str(doc.get("focus_key") or ""): _serialize_focus_review(doc) for doc in cursor if doc.get("focus_key")}


def _focus_feedback_for_key(focus_key: str, feedback_context: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    feedback = feedback_context.get(focus_key) or {}
    if not feedback:
        return {"action": None, "label": "", "note": "", "updated_at": None}
    return feedback


def _focus_review_for_key(focus_key: str, review_context: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    review = review_context.get(focus_key) or {}
    if not review:
        return {"status": None, "label": "", "summary": "", "checked_at": None}
    return review


def _serialize_focus_feedback(doc: Dict[str, Any]) -> Dict[str, Any]:
    action = str(doc.get("action") or "")
    return {
        "action": action,
        "label": FOCUS_FEEDBACK_LABELS.get(action, ""),
        "note": doc.get("note") or "",
        "updated_at": doc.get("updated_at"),
    }


def _serialize_focus_review(doc: Dict[str, Any]) -> Dict[str, Any]:
    status = str(doc.get("status") or "")
    return {
        "status": status,
        "label": FOCUS_REVIEW_LABELS.get(status, ""),
        "summary": doc.get("summary") or "",
        "metrics": doc.get("metrics") or {},
        "checked_at": doc.get("checked_at"),
    }


def save_market_insight_focus_feedback(
    *,
    user_id: str,
    focus_key: str,
    action: str,
    trade_date: Optional[str] = None,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    if action not in FOCUS_FEEDBACK_ACTIONS:
        raise ValueError("unsupported feedback action")
    td = _compact_trade_date(trade_date) if trade_date else str(focus_key).split("|", 1)[0]
    now = time.time()
    collections = get_db_collections()
    coll = collections["market_insight_focus_feedback"]
    previous = coll.find_one({"user_id": user_id, "focus_key": focus_key}) or {}
    doc = {
        "user_id": user_id,
        "trade_date": td,
        "focus_key": focus_key,
        "action": action,
        "note": note or "",
        "updated_at": now,
    }
    coll.update_one(
        {"user_id": user_id, "focus_key": focus_key},
        {"$set": doc, "$setOnInsert": {"created_at": now}},
        upsert=True,
    )
    update_user_preference_from_feedback(
        user_id=user_id,
        focus_key=focus_key,
        action=action,
        previous_action=previous.get("action"),
    )
    review_coll = collections.get("market_insight_focus_reviews")
    review = review_coll.find_one({"focus_key": focus_key}) if review_coll is not None else None
    if review:
        _apply_review_preference_to_feedback(review)
    coll.create_index([("user_id", ASCENDING), ("trade_date", DESCENDING)], name="user_trade_date_idx", background=True)
    coll.create_index([("user_id", ASCENDING), ("focus_key", ASCENDING)], unique=True, name="user_focus_unique", background=True)
    return {"success": True, "data": _serialize_focus_feedback(doc)}


def review_market_insight_focus(trade_date: Optional[str] = None, horizon_minutes: int = 60) -> Dict[str, Any]:
    td = _compact_trade_date(trade_date)
    rows = list_market_insight_events(trade_date=td, limit=500)
    reviews = []
    for event in rows:
        if event.get("subject_type") != "sector":
            continue
        review = _review_sector_focus_event(event, horizon_minutes)
        reviews.append(review)
    written = _upsert_focus_reviews(reviews)
    return {"success": True, "trade_date": td, "horizon_minutes": horizon_minutes, "written": written, "data": reviews}


def _review_sector_focus_event(event: Dict[str, Any], horizon_minutes: int) -> Dict[str, Any]:
    focus_key = _focus_event_key(event)
    trade_date = str(event.get("trade_date") or "")
    snapshot_time = str(event.get("snapshot_time") or "")
    level = str(event.get("level") or "")
    sector_code = str(event.get("subject_code") or "")
    baseline = _num((event.get("evidence") or {}).get("positive_ratio_now"))
    if not all([trade_date, snapshot_time, level, sector_code]) or baseline is None:
        return _focus_review_doc(
            event,
            focus_key,
            "insufficient_data",
            "缺少板块级别、代码或基准上涨比例，暂无法验证。",
            horizon_minutes,
            {},
        )

    end_time = _snapshot_plus(snapshot_time, horizon_minutes)
    rows = list(
        get_data_db()["realtime_sector_strength"]
        .find(
            {
                "trade_date": trade_date,
                "level": level,
                "sector_code": sector_code,
                "snapshot_time": {"$gt": snapshot_time, "$lte": end_time},
            },
            {"_id": 0},
        )
        .sort("snapshot_time", ASCENDING)
    )
    if not rows:
        return _focus_review_doc(
            event,
            focus_key,
            "insufficient_data",
            "后续窗口内没有足够的板块快照，暂不判断。",
            horizon_minutes,
            {"baseline_positive_ratio": baseline, "sample_count": 0},
        )

    values = [_num(row.get("positive_ratio")) for row in rows]
    values = [value for value in values if value is not None]
    if not values:
        return _focus_review_doc(
            event,
            focus_key,
            "insufficient_data",
            "后续快照缺少上涨比例，暂不判断。",
            horizon_minutes,
            {"baseline_positive_ratio": baseline, "sample_count": len(rows)},
        )
    latest = values[-1]
    peak = max(values)
    trough = min(values)
    delta_latest = latest - baseline
    delta_peak = peak - baseline
    metrics = {
        "baseline_positive_ratio": baseline,
        "latest_positive_ratio": latest,
        "peak_positive_ratio": peak,
        "trough_positive_ratio": trough,
        "delta_latest": delta_latest,
        "delta_peak": delta_peak,
        "sample_count": len(values),
        "first_snapshot_time": rows[0].get("snapshot_time"),
        "last_snapshot_time": rows[-1].get("snapshot_time"),
    }
    if delta_peak >= 0.08 or delta_latest >= 0.05:
        status = "confirmed"
        summary = f"后续 {horizon_minutes} 分钟内上涨比例最高提升至 {peak:.1%}，焦点得到验证。"
    elif delta_latest <= -0.08 or trough <= baseline - 0.10:
        status = "faded"
        summary = f"后续 {horizon_minutes} 分钟内上涨比例回落至 {latest:.1%}，焦点被反证。"
    else:
        status = "unconfirmed"
        summary = f"后续 {horizon_minutes} 分钟内上涨比例变化有限，暂未验证。"
    return _focus_review_doc(event, focus_key, status, summary, horizon_minutes, metrics)


def _focus_review_doc(
    event: Dict[str, Any],
    focus_key: str,
    status: str,
    summary: str,
    horizon_minutes: int,
    metrics: Dict[str, Any],
) -> Dict[str, Any]:
    now = time.time()
    return {
        "focus_key": focus_key,
        "trade_date": event.get("trade_date"),
        "snapshot_time": event.get("snapshot_time"),
        "event_type": event.get("event_type"),
        "subject_type": event.get("subject_type"),
        "subject_code": event.get("subject_code"),
        "subject_name": event.get("subject_name"),
        "status": status,
        "label": FOCUS_REVIEW_LABELS.get(status, ""),
        "summary": summary,
        "horizon_minutes": horizon_minutes,
        "metrics": metrics,
        "checked_at": now,
    }


def _upsert_focus_reviews(reviews: List[Dict[str, Any]]) -> int:
    if not reviews:
        return 0
    coll = get_db_collections()["market_insight_focus_reviews"]
    for review in reviews:
        coll.update_one({"focus_key": review["focus_key"]}, {"$set": review}, upsert=True)
        _apply_review_preference_to_feedback(review)
    coll.create_index([("trade_date", DESCENDING), ("status", ASCENDING)], name="trade_status_idx", background=True)
    coll.create_index([("focus_key", ASCENDING)], unique=True, name="focus_key_unique", background=True)
    rebuild_market_insight_global_quality()
    return len(reviews)


def _build_user_relevance_context(user_id: Optional[str]) -> Dict[str, Any]:
    if not user_id:
        return {"symbol_sources": {}, "sector_sources": {}}
    collections = get_db_collections()
    context: Dict[str, Any] = {"symbol_sources": {}, "sector_sources": {}}
    for doc in collections["trader_positions"].find({"user_id": user_id}):
        quantity = _num(doc.get("quantity", doc.get("qty", doc.get("volume", 0))), 0.0) or 0.0
        if quantity <= 0:
            continue
        _register_relevance_doc(context, doc, "real_position")
    for doc in collections["intraday_t0_mock_positions"].find({"user_id": user_id}):
        if doc.get("enabled", True) is False:
            continue
        _register_relevance_doc(context, doc, "mock_position")
    watch_doc = collections["user_watchlists"].find_one({"user_id": user_id}) or {}
    for symbol in watch_doc.get("symbols", []):
        _register_relevance_doc(context, {"symbol": symbol}, "watchlist")
    return context


def _register_relevance_doc(context: Dict[str, Any], doc: Dict[str, Any], source: str) -> None:
    symbol = normalize_symbol(str(doc.get("symbol") or doc.get("ts_code") or ""))
    industry = _industry_from_doc(doc)
    if symbol:
        context["symbol_sources"].setdefault(symbol, set()).add(source)
        if not any(industry.values()):
            industry = _lookup_sw_industry(symbol)
    for code in industry.values():
        if code:
            context["sector_sources"].setdefault(code, set()).add(source)


def _industry_from_doc(doc: Dict[str, Any]) -> Dict[str, str]:
    return {
        "L1": str(doc.get("sw_l1_code") or doc.get("l1_code") or ""),
        "L2": str(doc.get("sw_l2_code") or doc.get("l2_code") or ""),
        "L3": str(doc.get("sw_l3_code") or doc.get("l3_code") or ""),
    }


def _lookup_sw_industry(symbol: str) -> Dict[str, str]:
    member = get_data_db()["sw_index_member"]
    projection = {"_id": 0, "l1_code": 1, "l2_code": 1, "l3_code": 1}
    for candidate in _symbol_candidates(symbol):
        doc = member.find_one({"con_code": candidate, "src": "SW2021", "is_new": "Y"}, projection)
        if doc:
            return {
                "L1": str(doc.get("l1_code") or ""),
                "L2": str(doc.get("l2_code") or ""),
                "L3": str(doc.get("l3_code") or ""),
            }
    return {}


def _symbol_candidates(symbol: str) -> List[str]:
    normalized = normalize_symbol(symbol)
    bare = normalized.split(".")[0] if normalized else str(symbol or "").strip()
    candidates = [normalized, bare, str(symbol or "").strip().upper()]
    return list(dict.fromkeys([candidate for candidate in candidates if candidate]))


def _event_user_relevance(event: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    subject_type = str(event.get("subject_type") or "")
    subject_code = str(event.get("subject_code") or "")
    sources = set()
    if subject_type == "sector" and subject_code:
        sources.update(context.get("sector_sources", {}).get(subject_code, set()))
    elif subject_type == "stock" and subject_code:
        sources.update(context.get("symbol_sources", {}).get(normalize_symbol(subject_code), set()))
    score = max([RELEVANCE_SOURCE_WEIGHTS.get(source, 0.0) for source in sources] or [0.0])
    labels = [RELEVANCE_SOURCE_LABELS[source] for source in sorted(sources) if source in RELEVANCE_SOURCE_LABELS]
    return {
        "score": score,
        "labels": labels,
        "matched": bool(labels),
    }


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
