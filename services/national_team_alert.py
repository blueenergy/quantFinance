"""DingTalk alerting for the national-team ETF monitor.

``services.etf_national_team`` answers "is the national team buying right
now?" and exposes a four-level verdict (calm / watch / suspect / high).  This
module turns level changes into DingTalk messages.

Alert rules (confirmed with the user on 2026-11-04):

- only ``suspect`` and above are pushed; ``watch`` stays dashboard-only;
- a level is announced at most once per trade date and only on upgrades;
- the closing run at 15:05 re-evaluates the final snapshot and sends a
  "close confirmation" only if the final level is higher than anything
  already announced that day.

Per-trade-date state lives in ``finance.national_team_alert_state`` so the
scheduler can restart mid-day without replaying alerts.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from services.etf_national_team import LEVEL_LABELS
from services.ops_notify import send_ops_alert

log = logging.getLogger(__name__)

LEVEL_ORDER = {"calm": 0, "watch": 1, "suspect": 2, "high": 3}
MIN_ALERT_LEVEL = "suspect"
TITLE_PREFIX = "【国家队】"


def _rank(level: Any) -> int:
    return LEVEL_ORDER.get(str(level or "calm"), 0)


def _fmt_amount(value: Any) -> str:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "-"
    if abs(amount) >= 1e8:
        return f"{amount / 1e8:.1f} 亿"
    if abs(amount) >= 1e4:
        return f"{amount / 1e4:.0f} 万"
    return f"{amount:.0f}"


def _fmt_ratio(value: Any) -> str:
    try:
        ratio = float(value)
    except (TypeError, ValueError):
        return "-"
    return f"{ratio:.2f}x"


def _fmt_hm(value: Any) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) >= 4:
        return f"{digits[:2]}:{digits[2:4]}"
    return "-"


def _fmt_share(value: Any) -> str:
    try:
        return f"{float(value):+.2f}%"
    except (TypeError, ValueError):
        return "-"


def build_alert(snapshot: Mapping[str, Any], close: bool = False) -> Tuple[str, List[str]]:
    """Return ``(title, lines)`` for a DingTalk markdown message."""
    level = str(snapshot.get("level") or "calm")
    label = LEVEL_LABELS.get(level, level)
    trade_date = str(snapshot.get("trade_date") or "-")
    as_of = _fmt_hm(snapshot.get("as_of"))
    header = "收盘确认" if close else "盘中信号"

    breadth = snapshot.get("breadth") or {}
    pool = snapshot.get("pool") or {}
    tail = snapshot.get("tail") or {}
    share = snapshot.get("share") or {}
    first = snapshot.get("first_signal") or {}
    direction = str(share.get("direction") or "")
    direction_label = str(share.get("direction_label") or "")
    headline = label
    if direction in ("inflow", "outflow") and direction_label:
        headline = f"{label}·{direction_label}"

    title = f"{TITLE_PREFIX}{header} · {headline} ({trade_date} {as_of})"

    lines: List[str] = [
        f"- 交易日 {trade_date} · 数据截至 {as_of}",
        f"- 级别 **{label}**（{header}）",
        "- 广度 量比≥2: %s 只 / ≥2.5: %s 只（池 %s 只）"
        % (
            breadth.get("ratio2", 0),
            breadth.get("ratio25", 0),
            breadth.get("pool_size", 0),
        ),
        "- 池成交额 %s vs 同时刻基线 %s（%s）"
        % (
            _fmt_amount(pool.get("amount_yuan")),
            _fmt_amount(pool.get("baseline_yuan")),
            _fmt_ratio(pool.get("ratio")),
        ),
    ]

    if tail.get("share") is not None:
        try:
            share_pct = float(tail["share"]) * 100
            base_pct = float(tail["baseline"]) * 100 if tail.get("baseline") else None
            tail_text = "%.1f%%" % share_pct
            if base_pct is not None:
                tail_text += " vs 20 日基线 %.1f%%（%s）" % (
                    base_pct,
                    _fmt_ratio(tail.get("ratio")),
                )
            lines.append(f"- 尾盘集中度 {tail_text}")
        except (TypeError, ValueError):
            lines.append("- 尾盘集中度 -")
    else:
        lines.append("- 尾盘集中度 未到 14:30，暂无")

    share1 = share.get("avg_chg_1d")
    if share1 is not None:
        lines.append(
            "- 份额确认(T+1) 日 %s / 5日 %s（数据日 %s）"
            % (_fmt_share(share1), _fmt_share(share.get("avg_chg_5d")), share.get("date") or "-")
        )
    pool_flow = share.get("pool_flow")
    if pool_flow is not None:
        rotation = share.get("rotation")
        rot_text = ""
        try:
            if rotation is not None:
                rot_text = " · 相消比 %.2f" % float(rotation)
        except (TypeError, ValueError):
            rot_text = ""
        lines.append(
            "- 池子净申赎(AUM加权) %s%s" % (_fmt_share(pool_flow), rot_text)
        )

    moves = [
        "%s %s" % (item.get("name") or item.get("ts_code"), _fmt_share(item.get("pct_chg")))
        for item in (snapshot.get("index_moves") or [])
        if item.get("pct_chg") is not None
    ]
    if moves:
        lines.append("- 指数 " + " | ".join(moves))

    top = sorted(
        (item for item in (snapshot.get("etfs") or []) if item.get("ratio") is not None),
        key=lambda item: float(item.get("ratio") or 0),
        reverse=True,
    )[:3]
    if top:
        lines.append(
            "- 量比前三 "
            + " | ".join(
                "%s %s %s（%s）"
                % (
                    item.get("ts_code"),
                    item.get("name"),
                    _fmt_ratio(item.get("ratio")),
                    item.get("signal_label") or item.get("signal") or "-",
                )
                for item in top
            )
        )

    first_parts = []
    if first.get("watch"):
        first_parts.append("首次观察 %s" % _fmt_hm(first["watch"]))
    if first.get("suspect"):
        first_parts.append("首次疑似 %s" % _fmt_hm(first["suspect"]))
    if first_parts:
        lines.append("- " + " · ".join(first_parts))

    lines.append("")
    lines.append(
        "> 国家队不可直接观测；「%s」为量价 + 份额证据，不构成确认。" % headline
    )
    return title, lines


def _mark_close_evaluated(
    state_coll: Any, trade_date: str, level: Any, as_of: Any
) -> None:
    state_coll.update_one(
        {"_id": trade_date},
        {
            "$set": {
                "trade_date": trade_date,
                "close_evaluated": True,
                "close_level": level,
                "last_as_of": as_of,
                "updated_at": datetime.now(),
            }
        },
        upsert=True,
    )


def evaluate_snapshot(
    snapshot: Mapping[str, Any],
    state_coll: Any,
    close: bool = False,
    sender: Optional[Callable[[str, List[str]], bool]] = None,
) -> Dict[str, Any]:
    """Announce a level upgrade for this snapshot.

    Returns ``{"sent": bool, "reason": str, "level": str}``.  Send failures do
    not advance the state, so the next scheduler tick retries.
    """
    sender = sender or send_ops_alert
    trade_date = str(snapshot.get("trade_date") or "")
    level = str(snapshot.get("level") or "calm")
    as_of = snapshot.get("as_of")
    if not trade_date:
        return {"sent": False, "reason": "no trade_date", "level": level}

    state = state_coll.find_one({"_id": trade_date}) or {}

    if state.get("close_evaluated"):
        return {"sent": False, "reason": "close already evaluated", "level": level}

    sent_level = state.get("sent_level")
    if _rank(level) < _rank(MIN_ALERT_LEVEL) or _rank(level) <= _rank(sent_level):
        if close:
            _mark_close_evaluated(state_coll, trade_date, level, as_of)
            return {"sent": False, "reason": "no upgrade at close", "level": level}
        return {"sent": False, "reason": "no upgrade", "level": level}

    title, lines = build_alert(snapshot, close=close)
    if not sender(title, lines):
        log.warning(
            "[NationalTeamAlert] send failed for %s %s; will retry",
            trade_date,
            level,
        )
        return {"sent": False, "reason": "send failed", "level": level}

    now = datetime.now()
    payload = {
        "trade_date": trade_date,
        "sent_level": level,
        "sent_at": now,
        "last_as_of": as_of,
        "updated_at": now,
    }
    if close:
        payload.update({"close_evaluated": True, "close_level": level})
    state_coll.update_one({"_id": trade_date}, {"$set": payload}, upsert=True)
    log.info("[NationalTeamAlert] sent %s %s", trade_date, level)
    return {"sent": True, "reason": "upgrade", "level": level}
