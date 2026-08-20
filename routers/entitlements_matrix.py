"""
Admin-configurable entitlements matrix (navigation + page actions × service tiers).

Hierarchy: categories → sections (功能模块) → rows (Tab 或页内操作).
Child actions inherit from parent nav tab: if the tab is not visible for a tier,
the action is ineffective for that user (documented in API + UI).

Phase 1: persistence + admin UI; runtime not yet wired.
"""
import time
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Body
from pydantic import BaseModel, Field, field_validator

from auth import get_current_admin_user, log_admin_action
from db import get_db_collections

MATRIX_DOC_ID = "entitlements_matrix"
TIER_KEYS = ("free", "basic", "vip", "premium")

TabVisibility = Literal["inherit", "hidden", "visible"]
ActionClick = Literal["inherit", "blocked", "allowed"]


class TabCell(BaseModel):
    visibility: TabVisibility = "inherit"


class ActionCell(BaseModel):
    click: ActionClick = "inherit"
    weekly_limit: Optional[int] = None

    @field_validator("weekly_limit")
    @classmethod
    def limit_non_negative(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v < 0:
            raise ValueError("weekly_limit must be >= 0 or null")
        return v


# --- 主导航 Tab（顺序即模块排序）---
_NAV_ROWS: List[Tuple[str, str, int]] = [
    ("market-events", "📅 交易日历", 1),
    ("global-market-brief", "🌍 全球市场简报", 5),
    ("data-pulse", "🛰️ 数据脉搏", 8),
    ("market-analysis", "🤖 AI大盘分析", 9),
    ("limit-up-ladder", "📊 连板天梯", 10),
    ("x-influencer-voices", "🐦 X大V情报", 12),
    ("theme-lag-recommend", "📌 主题补涨", 15),
    ("daily-gold-picks", "⭐ 每日金股", 16),
    ("market-stats", "📉 市场统计", 17),
    ("china-macro", "🇨🇳 中国宏观", 18),
    ("us-rates", "🇺🇸 美国利率", 19),
    ("sector-concept", "📈 概念板块", 20),
    ("hot-stock", "🔥 热股分析", 30),
    ("etf", "💰 ETF淘金", 40),
    ("earnings-hunter", "🎯 财报猎手", 50),
    ("shenwan-index", "📊 申万行业指数", 55),
    ("strategy-pool", "🎯 策略股池", 60),
    ("ranking", "金榜", 70),
    ("watchlist", "自选股", 80),
    ("backtest", "📊 回测管理", 90),
    ("strategy-lab", "🧪 策略实验室", 95),
    ("factor-backtest", "🧬 因子回测", 96),
    ("portfolio-research", "组合研究", 97),
    ("portfolio-plans", "📋 组合交易计划", 98),
    ("portfolio-overview", "📊 组合总览(实盘)", 99),
    ("strategies", "策略配置", 100),
    ("strategy-workers", "🚀 实盘交易", 110),
    ("trade-executions", "交易记录", 120),
    ("trading-manual", "人工干预", 130),
    ("history", "AI分析回溯", 140),
    ("analysis-task-queue", "分析任务队列", 145),
    ("spectrum", "阴阳谱", 160),
    ("realtime-spectrum", "实时阴阳谱", 162),
    ("intraday-t0", "日内T+0", 165),
    ("market-insights", "火眼金睛", 168),
    ("securities", "账户工作台", 170),
    ("user-profile", "用户配置", 180),
    ("chat", "🤖 AI助手", 190),
    ("kline-comparison", "📈 走势对比", 195),
]

# 某主导航 Tab 下的页内操作 (action_key, 展示名)
_CHILD_ACTIONS_BY_NAV_SUFFIX: Dict[str, List[Tuple[str, str]]] = {
    "global-market-brief": [
        ("action.global_brief.refresh", "刷新全球市场简报"),
    ],
    "market-analysis": [
        ("action.market_bulletin.trigger", "触发大盘 AI 分析"),
    ],
    "limit-up-ladder": [
        ("action.ladder.feedback", "涨停归因 · 提交反馈并重算"),
    ],
    "etf": [
        ("action.etf.analyze", "触发智能分析"),
    ],
    "watchlist": [
        ("action.watchlist.deep_analysis", "深度分析（自选股）"),
        ("action.watchlist.opportunities_analysis", "机会分析（自选股/组合）"),
    ],
}

# Runtime: deep analysis quota / allow checks (analysis router)
FEATURE_WATCHLIST_DEEP_ANALYSIS = "action.watchlist.deep_analysis"
FEATURE_WATCHLIST_OPPORTUNITIES = "action.watchlist.opportunities_analysis"

_TOP_SECTIONS: List[Tuple[str, str, str, List[Tuple[str, str]]]] = []


def _row_dict(
    key: str,
    label: str,
    row_type: str,
    *,
    parent_nav_key: Optional[str] = None,
    inherit_note: Optional[str] = None,
    order: int = 0,
) -> Dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "row_type": row_type,
        "parent_nav_key": parent_nav_key,
        "inherit_note": inherit_note,
        "order": order,
    }


def build_categories() -> List[Dict[str, Any]]:
    """大类 → 模块(section) → 行；子行带 parent_nav_key。"""
    top_sections: List[Dict[str, Any]] = []
    for sec_id, title, description, actions in _TOP_SECTIONS:
        rows = []
        for i, (akey, alabel) in enumerate(actions):
            rows.append(
                _row_dict(
                    akey,
                    alabel,
                    "action",
                    parent_nav_key=None,
                    inherit_note=None,
                    order=(i + 1) * 10,
                )
            )
        top_sections.append(
            {
                "section_id": sec_id,
                "title": title,
                "description": description,
                "rows": rows,
            }
        )

    nav_sections: List[Dict[str, Any]] = []
    for suffix, label, order in _NAV_ROWS:
        nav_key = f"nav.{suffix}"
        rows: List[Dict[str, Any]] = [
            _row_dict(
                nav_key,
                label,
                "tab",
                parent_nav_key=None,
                inherit_note=None,
                order=order,
            )
        ]
        for j, (akey, alabel) in enumerate(_CHILD_ACTIONS_BY_NAV_SUFFIX.get(suffix, [])):
            rows.append(
                _row_dict(
                    akey,
                    alabel,
                    "action",
                    parent_nav_key=nav_key,
                    inherit_note="依赖本模块第一行「导航 Tab」：该 Tab 对某等级不可见时，此项对该等级不生效（与下方矩阵配置无关）。",
                    order=order + j + 1,
                )
            )
        nav_sections.append(
            {
                "section_id": f"mod_{suffix}",
                "title": label,
                "description": (
                    "主导航入口及其页内能力。"
                    if len(rows) > 1
                    else "主导航 Tab（暂无页内矩阵子项）。"
                ),
                "rows": rows,
            }
        )

    return [
        {
            "category_id": "top_banner",
            "title": "顶部资讯区",
            "hint": "与顶栏卡片相关，无独立主导航 Tab。",
            "sections": top_sections,
        },
        {
            "category_id": "main_nav",
            "title": "主导航模块",
            "hint": "每个模块第一行为导航 Tab；其后为页内操作。子项继承父级 Tab：若某等级用户看不到该 Tab，则子操作对该用户不生效（与矩阵中子项配置无关）。",
            "sections": nav_sections,
        },
    ]


def build_feature_registry() -> List[Dict[str, Any]]:
    """扁平列表，供校验与默认 cells（顺序与 categories 一致）。"""
    flat: List[Dict[str, Any]] = []
    for cat in build_categories():
        for sec in cat["sections"]:
            for row in sec["rows"]:
                flat.append(
                    {
                        **row,
                        "group": cat["title"],
                        "section_title": sec["title"],
                    }
                )
    return flat


def _registry_keys() -> List[str]:
    return [r["key"] for r in build_feature_registry()]


def _settings_coll():
    c = get_db_collections().get("app_settings")
    if c is None:
        raise HTTPException(status_code=500, detail="app_settings collection unavailable")
    return c


def _default_cells() -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for row in build_feature_registry():
        key = row["key"]
        out[key] = {}
        for tier in TIER_KEYS:
            if row["row_type"] == "tab":
                out[key][tier] = {"visibility": "inherit"}
            else:
                out[key][tier] = {"click": "inherit", "weekly_limit": None}
    return out


def _row_type_for_key(feat_key: str) -> str:
    for r in build_feature_registry():
        if r["key"] == feat_key:
            return r["row_type"]
    return "tab"


def _merge_cells(stored: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    base = _default_cells()
    if not stored:
        return base
    for feat_key, tier_map in stored.items():
        if feat_key not in base:
            continue
        if not isinstance(tier_map, dict):
            continue
        rtype = _row_type_for_key(feat_key)
        for tier, cell in tier_map.items():
            if tier not in TIER_KEYS or not isinstance(cell, dict):
                continue
            if rtype == "tab":
                v = cell.get("visibility", "inherit")
                if v in ("inherit", "hidden", "visible"):
                    base[feat_key][tier] = {"visibility": v}
            else:
                ck = cell.get("click", "inherit")
                if ck not in ("inherit", "blocked", "allowed"):
                    ck = "inherit"
                wl = cell.get("weekly_limit")
                if wl is not None:
                    try:
                        wl = int(wl)
                        if wl < 0:
                            wl = None
                    except (TypeError, ValueError):
                        wl = None
                base[feat_key][tier] = {"click": ck, "weekly_limit": wl}
    return base


def _validate_and_normalize_cells(raw: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    valid_keys = _registry_keys()
    registry = build_feature_registry()
    key_to_row = {r["key"]: r for r in registry}
    out: Dict[str, Dict[str, Any]] = {}
    for feat_key in valid_keys:
        tier_map = raw.get(feat_key)
        if tier_map is not None and not isinstance(tier_map, dict):
            raise HTTPException(status_code=400, detail=f"Invalid tier map for {feat_key}")
        tier_map = tier_map or {}
        row_meta = key_to_row[feat_key]
        out[feat_key] = {}
        for tier in TIER_KEYS:
            cell = tier_map.get(tier, {})
            if not isinstance(cell, dict):
                cell = {}
            if row_meta["row_type"] == "tab":
                t = TabCell(**{k: v for k, v in cell.items() if k == "visibility"})
                out[feat_key][tier] = t.model_dump()
            else:
                a = ActionCell(**{k: v for k, v in cell.items() if k in ("click", "weekly_limit")})
                if a.click != "allowed":
                    a = ActionCell(click=a.click, weekly_limit=None)
                out[feat_key][tier] = a.model_dump()
    _validate_monotonic_matrix(out)
    return out


def _tab_visibility_rank(v: str) -> int:
    # hidden < inherit < visible
    if v == "hidden":
        return 0
    if v == "visible":
        return 2
    return 1


def _action_click_rank(v: str) -> int:
    # blocked < inherit < allowed
    if v == "blocked":
        return 0
    if v == "allowed":
        return 2
    return 1


def _limit_rank(v: Optional[int]) -> float:
    # None means unlimited, considered greater than any finite limit.
    return float("inf") if v is None else float(v)


def _validate_monotonic_matrix(cells: Dict[str, Dict[str, Any]]) -> None:
    """
    Enforce monotonic permissions across tiers:
    free <= basic <= vip <= premium
    """
    tier_order = list(TIER_KEYS)
    registry = build_feature_registry()
    key_to_row = {r["key"]: r for r in registry}

    for feat_key, tier_map in cells.items():
        row_meta = key_to_row.get(feat_key)
        if not row_meta:
            continue

        if row_meta["row_type"] == "tab":
            prev_rank = None
            prev_tier = None
            for tier in tier_order:
                cell = tier_map.get(tier, {"visibility": "inherit"})
                rank = _tab_visibility_rank(cell.get("visibility", "inherit"))
                if prev_rank is not None and rank < prev_rank:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Monotonicity violated for {feat_key}: "
                            f"{prev_tier} -> {tier} tab visibility decreases."
                        ),
                    )
                prev_rank = rank
                prev_tier = tier
            continue

        # action row
        prev_click_rank = None
        prev_tier = None
        prev_limit_rank = None
        prev_limit_tier = None

        for tier in tier_order:
            cell = tier_map.get(tier, {"click": "inherit", "weekly_limit": None})
            click = cell.get("click", "inherit")
            click_rank = _action_click_rank(click)

            if prev_click_rank is not None and click_rank < prev_click_rank:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Monotonicity violated for {feat_key}: "
                        f"{prev_tier} -> {tier} action click decreases."
                    ),
                )
            prev_click_rank = click_rank
            prev_tier = tier

            # When both tiers are explicitly allowed, weekly_limit must be non-decreasing.
            if click == "allowed":
                limit_rank = _limit_rank(cell.get("weekly_limit"))
                if prev_limit_rank is not None and limit_rank < prev_limit_rank:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Monotonicity violated for {feat_key}: "
                            f"{prev_limit_tier} -> {tier} weekly_limit decreases."
                        ),
                    )
                prev_limit_rank = limit_rank
                prev_limit_tier = tier


router = APIRouter(prefix="/entitlements-matrix", tags=["权限与导航配置"])


class EntitlementsMatrixPut(BaseModel):
    cells: Dict[str, Dict[str, Any]] = Field(default_factory=dict)


@router.get("")
def admin_get_entitlements_matrix(admin_user: dict = Depends(get_current_admin_user)):
    coll = _settings_coll()
    doc = coll.find_one({"_id": MATRIX_DOC_ID})
    stored_cells = doc.get("cells") if doc else None
    merged = _merge_cells(stored_cells)
    categories = build_categories()
    return {
        "success": True,
        "data": {
            "categories": categories,
            "registry": build_feature_registry(),
            "tiers": list(TIER_KEYS),
            "cells": merged,
            "updated_at": doc.get("updated_at").isoformat() if doc and doc.get("updated_at") else None,
            "help": {
                "inheritance": "页内操作的生效条件 = 父级导航 Tab 对该用户可见 AND 本格配置允许。",
                "tab": "inherit=沿用后端主导航规则(各 Tab 的 req_vip + 用户等级); hidden=隐藏; visible=强制显示",
                "action": "inherit=待接入; blocked=禁止点击; allowed=允许, weekly_limit 空=不限次",
            },
        },
    }


@router.put("")
def admin_put_entitlements_matrix(
    payload: EntitlementsMatrixPut = Body(...),
    admin_user: dict = Depends(get_current_admin_user),
):
    normalized = _validate_and_normalize_cells(payload.cells)
    coll = _settings_coll()
    now = datetime.utcnow()
    coll.update_one(
        {"_id": MATRIX_DOC_ID},
        {"$set": {"cells": normalized, "updated_at": now}},
        upsert=True,
    )
    log_admin_action(
        admin_user=admin_user,
        action="UPDATE_ENTITLEMENTS_MATRIX",
        details=f"keys={len(normalized)}",
    )
    # Invalidate in-process cache so the new matrix takes effect immediately.
    _invalidate_matrix_cache()
    return {"success": True, "data": {"cells": normalized, "updated_at": now.isoformat()}}


# ---------------------------------------------------------------------------
# In-process cache for entitlements matrix (read on every authenticated request)
# ---------------------------------------------------------------------------
_MATRIX_CACHE_TTL = 60  # seconds
_matrix_cache: Optional[Dict[str, Dict[str, Any]]] = None
_matrix_cache_ts: float = 0.0


def _invalidate_matrix_cache() -> None:
    global _matrix_cache, _matrix_cache_ts
    _matrix_cache = None
    _matrix_cache_ts = 0.0


def get_merged_matrix_cells() -> Dict[str, Dict[str, Any]]:
    """Read merged cells from DB, with a 60-second in-process TTL cache."""
    global _matrix_cache, _matrix_cache_ts
    now = time.monotonic()
    if _matrix_cache is not None and now - _matrix_cache_ts < _MATRIX_CACHE_TTL:
        return _matrix_cache
    coll = _settings_coll()
    doc = coll.find_one({"_id": MATRIX_DOC_ID})
    _matrix_cache = _merge_cells(doc.get("cells") if doc else None)
    _matrix_cache_ts = now
    return _matrix_cache


def user_tier_for_matrix(user: Dict[str, Any]) -> str:
    """Map JWT user to matrix tier keys (admin uses premium slot for matrix lookup only)."""
    if user.get("is_admin"):
        return "premium"
    sl = (user.get("service_level") or "free").lower()
    if sl not in TIER_KEYS:
        return "free"
    return sl


def resolve_action_entitlement(
    user: Dict[str, Any],
    feature_key: str,
    blocked_detail: str,
) -> Dict[str, Any]:
    """
    Generic matrix entitlement resolver for runtime action checks:
    - blocked: deny
    - matrix: use weekly feature quota with given limit (quota_usage + feature key)
    - inherit: use existing QuotaManager (VIP 月 / 其他周等)
    Admins always inherit (VIP 配额路径由 analysis 路由原有逻辑决定).
    """
    if user.get("is_admin"):
        return {"type": "inherit"}

    tier = user_tier_for_matrix(user)
    cells = get_merged_matrix_cells()
    row = cells.get(feature_key, {})
    cell = row.get(tier) or {"click": "inherit", "weekly_limit": None}
    click = cell.get("click", "inherit")
    wl = cell.get("weekly_limit")

    if click == "blocked":
        return {
            "type": "blocked",
            "detail": blocked_detail,
        }
    if click == "allowed" and wl is not None:
        try:
            n = int(wl)
            if n < 0:
                n = 0
        except (TypeError, ValueError):
            n = 0
        return {"type": "matrix", "weekly_limit": n, "feature_key": feature_key}
    return {"type": "inherit"}


def resolve_deep_analysis_entitlement(user: Dict[str, Any]) -> Dict[str, Any]:
    """
    How POST /analyze/deep-analysis should enforce quota.
    """
    return resolve_action_entitlement(
        user=user,
        feature_key=FEATURE_WATCHLIST_DEEP_ANALYSIS,
        blocked_detail="当前等级未开放自选股深度分析（权益矩阵已关闭）。",
    )
