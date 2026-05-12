"""
Main navigation tab visibility from the entitlements matrix (nav.* rows × tier).

- visibility inherit: apply legacy req_vip gating (non-VIP cannot see VIP-only tabs).
- visibility visible: force show for that tier (overrides req_vip).
- visibility hidden: force hide for that tier.

Admins see all tabs regardless of matrix.
"""
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Response

from auth import get_current_active_user
from routers.entitlements_matrix import get_merged_matrix_cells, user_tier_for_matrix

# (tab_id, requires_vip_or_premium) — keep in sync with dashboard App.vue baseTabs
KNOWN_MAIN_TABS: List[tuple] = [
    ("global-market-brief", False),
    ("data-pulse", False),
    ("market-analysis", False),
    ("limit-up-ladder", False),
    ("market-risk", False),
    ("china-macro", False),
    ("us-rates", False),
    ("x-influencer-voices", False),
    ("theme-lag-recommend", False),
    ("sector-concept", True),
    ("hot-stock", True),
    ("etf", True),
    ("earnings-hunter", True),
    ("shenwan-index", True),
    ("history", False),
    ("strategy-pool", False),
    ("ranking", False),
    ("watchlist", False),
    ("backtest", False),
    ("strategies", False),
    ("strategy-workers", False),
    ("trade-executions", False),
    ("trading-manual", False),
    ("task-center", False),
    ("strategy-execution-analysis", False),
    ("spectrum", False),
    ("securities", False),
    ("user-profile", False),
    ("chat", False),
    ("kline-comparison", False),
]


def _known_tab_ids() -> List[str]:
    return [t[0] for t in KNOWN_MAIN_TABS]


def compute_visible_tab_ids(
    user: Dict[str, Any],
    cells: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[str]:
    """
    Ordered list of main tab ids the user may see.
    If ``cells`` is None, loads merged entitlements matrix from DB.
    """
    if cells is None:
        cells = get_merged_matrix_cells()

    if user.get("is_admin"):
        return [tid for tid, _ in KNOWN_MAIN_TABS]

    tier = user_tier_for_matrix(user)
    is_vip = tier in ("vip", "premium")

    out: List[str] = []
    for tab_id, req_vip in KNOWN_MAIN_TABS:
        nav_key = f"nav.{tab_id}"
        row = cells.get(nav_key, {})
        cell = row.get(tier) or {}
        vis = cell.get("visibility", "inherit")

        if vis == "hidden":
            continue
        if vis == "visible":
            out.append(tab_id)
            continue
        # inherit
        if req_vip and not is_vip:
            continue
        out.append(tab_id)

    return out


user_router = APIRouter(tags=["主导航"])


@user_router.get("/navigation-tabs")
def get_my_navigation_tabs(
    response: Response,
    current_user: dict = Depends(get_current_active_user),
):
    """Return visible main tab ids for the current user (from entitlements matrix)."""
    # Per-user payload; avoid CDN/proxy/browser serving a stale list for another session.
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    visible = compute_visible_tab_ids(current_user)
    return {"success": True, "data": {"visible_tab_ids": visible}}
