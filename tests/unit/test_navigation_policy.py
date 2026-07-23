"""Unit tests for main navigation visibility (entitlements matrix nav.* + req_vip inherit)."""
import pytest

from routers import navigation_policy as navp
from routers.entitlements_matrix import TIER_KEYS


def _all_inherit_cells() -> dict:
    cells = {}
    for tab_id, _ in navp.KNOWN_MAIN_TABS:
        key = f"nav.{tab_id}"
        cells[key] = {t: {"visibility": "inherit"} for t in TIER_KEYS}
    return cells


@pytest.mark.unit
def test_compute_visible_tab_ids_admin_sees_all():
    user = {"is_admin": True, "service_level": "free"}
    ids = navp.compute_visible_tab_ids(user, _all_inherit_cells())
    assert len(ids) == len(navp.KNOWN_MAIN_TABS)
    assert "etf" in ids
    assert ids[0] == "market-events"
    assert "market-events" in ids
    assert "global-market-brief" in ids
    assert "data-pulse" in ids
    assert "market-analysis" in ids
    assert "limit-up-ladder" in ids
    assert "market-regime" in ids


@pytest.mark.unit
def test_compute_visible_tab_ids_free_inherit_excludes_req_vip_tabs():
    user = {"service_level": "free", "is_admin": False}
    ids = navp.compute_visible_tab_ids(user, _all_inherit_cells())
    assert "etf" not in ids
    assert "sector-concept" not in ids
    assert ids[0] == "market-events"
    assert ids.index("market-events") < ids.index("global-market-brief")
    assert ids.index("global-market-brief") < ids.index("limit-up-ladder")
    assert ids.index("market-risk") < ids.index("market-regime") < ids.index("china-macro")
    assert "data-pulse" in ids
    assert "market-analysis" in ids
    assert "limit-up-ladder" in ids
    assert "market-regime" in ids


@pytest.mark.unit
def test_compute_visible_tab_ids_free_visible_overrides_req_vip():
    cells = _all_inherit_cells()
    cells["nav.etf"]["free"] = {"visibility": "visible"}
    user = {"service_level": "free", "is_admin": False}
    ids = navp.compute_visible_tab_ids(user, cells)
    assert "etf" in ids


@pytest.mark.unit
def test_compute_visible_tab_ids_free_hidden_hides_tab():
    cells = _all_inherit_cells()
    cells["nav.limit-up-ladder"]["free"] = {"visibility": "hidden"}
    user = {"service_level": "free", "is_admin": False}
    ids = navp.compute_visible_tab_ids(user, cells)
    assert "limit-up-ladder" not in ids
    assert "watchlist" in ids


@pytest.mark.unit
def test_compute_visible_tab_ids_vip_inherit_gets_req_vip_tabs():
    user = {"service_level": "vip", "is_admin": False}
    ids = navp.compute_visible_tab_ids(user, _all_inherit_cells())
    assert "etf" in ids
    assert "limit-up-ladder" in ids
