"""Build a frozen evidence pack for deterministic fund-inventory period review."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Mapping, Optional, Sequence

from services.fund_inventory_comparison import STATE_ORDER

_STATE_LABELS = {
    "incumbent_strengthen": "存量强化",
    "new_inventory": "新建库存",
    "inventory_turnover": "库存换手",
    "inventory_exit": "库存退出",
}
_TOP_KEYS = (
    "net_inflow",
    "net_outflow",
    "new_inventory",
    "inventory_exit",
    "incumbent_strengthen",
    "inventory_turnover",
)


def _add(evidence: List[Dict[str, Any]], evidence_id: str, label: str, value: Any) -> None:
    evidence.append({"id": evidence_id, "label": label, "value": value})


def build_period_review_evidence_pack(
    comparison: Mapping[str, Any],
    *,
    top_n: int = 10,
) -> Dict[str, Any]:
    """Convert a ``build_period_comparison`` result into a frozen, auditable pack.

    Every numeric fact the LLM may mention is listed under ``evidence`` with a
    stable id. The model's output is validated against ``available_evidence_ids``
    and the server renders the final narrative from this pack.
    """
    top_n = max(1, min(int(top_n or 10), 50))
    totals = dict(comparison.get("totals") or {})
    state_counts = dict(comparison.get("state_counts") or {})
    state_flow = dict(comparison.get("state_flow") or {})
    quality = dict(comparison.get("quality") or {})
    caveats = list(comparison.get("caveats") or [])

    evidence: List[Dict[str, Any]] = []
    _add(evidence, "overview.net_flow", "本期净增减（亿元）", totals.get("net_ex_price_yi"))
    _add(evidence, "overview.new_inflow", "本期新进（亿元）", totals.get("new_inflow_yi"))
    _add(evidence, "overview.incumbent_delta", "本期存量变化（亿元）", totals.get("incumbent_delta_yi"))
    _add(evidence, "overview.price_effect", "本期股价贡献（亿元）", totals.get("mv_price_effect_yi"))
    _add(evidence, "overview.surface_change", "本期表面市值变化（亿元）", totals.get("surface_mv_chg_yi"))
    _add(evidence, "overview.stocks", "本期股票数", totals.get("stocks"))

    for state in STATE_ORDER:
        label = _STATE_LABELS[state]
        _add(evidence, f"state.{state}.count", f"{label}股票数", state_counts.get(state))
        _add(
            evidence,
            f"state.{state}.net_flow",
            f"{label}净增减（亿元）",
            (state_flow.get(state) or {}).get("net_ex_price_yi"),
        )

    industries = list(comparison.get("industries") or [])
    positive = sorted(
        [row for row in industries if (row.get("net_ex_price_yi") or 0) > 0],
        key=lambda row: row.get("net_ex_price_yi") or 0.0,
        reverse=True,
    )[:top_n]
    negative = sorted(
        [row for row in industries if (row.get("net_ex_price_yi") or 0) < 0],
        key=lambda row: row.get("net_ex_price_yi") or 0.0,
    )[:top_n]
    for index, row in enumerate(positive, 1):
        _add(
            evidence,
            f"industry.inflow.{index}",
            f"净流入行业 #{index} {row.get('industry')}（亿元）",
            row.get("net_ex_price_yi"),
        )
    for index, row in enumerate(negative, 1):
        _add(
            evidence,
            f"industry.outflow.{index}",
            f"净流出行业 #{index} {row.get('industry')}（亿元）",
            row.get("net_ex_price_yi"),
        )

    top = dict(comparison.get("top") or {})
    top_sections: Dict[str, List[Dict[str, Any]]] = {}
    for key in _TOP_KEYS:
        rows = list(top.get(key) or [])[:top_n]
        top_sections[key] = rows
        for index, row in enumerate(rows, 1):
            label = {
                "net_inflow": "净流入个股",
                "net_outflow": "净流出个股",
                "new_inventory": "新建库存个股",
                "inventory_exit": "库存退出个股",
                "incumbent_strengthen": "存量强化个股",
                "inventory_turnover": "库存换手个股",
            }[key]
            _add(
                evidence,
                f"stock.{key}.{index}",
                f"{label} #{index} {row.get('name') or row.get('ts_code')}（亿元）",
                row.get("net_ex_price_yi"),
            )

    _add(evidence, "quality.scope", "对比口径", comparison.get("comparison_scope"))
    _add(evidence, "quality.coverage", "策略覆盖（%）", quality.get("strategy_coverage_pct"))
    _add(evidence, "quality.depth", "持仓深度（%）", quality.get("holding_depth_pct"))
    _add(evidence, "quality.warning", "质量提示", quality.get("warning"))
    for index, caveat in enumerate(caveats, 1):
        _add(evidence, f"caveat.{index}", "口径提示", caveat)

    return {
        "period_curr": comparison.get("period_curr"),
        "period_prev": comparison.get("period_prev"),
        "compare_mode": comparison.get("compare_mode"),
        "comparison_scope": comparison.get("comparison_scope"),
        "quality_status": comparison.get("quality_status"),
        "quality": quality,
        "totals": totals,
        "state_counts": state_counts,
        "state_flow": state_flow,
        "industries": {"inflow": positive, "outflow": negative},
        "top_stocks": top_sections,
        "caveats": caveats,
        "available_evidence_ids": [item["id"] for item in evidence],
        "evidence": evidence,
    }


def build_evidence_fingerprint(pack: Mapping[str, Any]) -> str:
    """Stable fingerprint used for task idempotency and report reuse."""
    canonical = json.dumps(
        pack,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
