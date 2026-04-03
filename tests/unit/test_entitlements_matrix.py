"""
Unit tests for entitlements matrix registry, deep-analysis resolution, and feature weekly quota.
"""
import pytest
from datetime import datetime

from routers import entitlements_matrix as em
from routers.entitlements_matrix import FEATURE_WATCHLIST_DEEP_ANALYSIS
from services.quota_manager import QuotaManager


@pytest.mark.unit
def test_build_categories_watchlist_has_deep_analysis_child():
    cats = em.build_categories()
    main = next(c for c in cats if c["category_id"] == "main_nav")
    wl_sec = next(s for s in main["sections"] if s["section_id"] == "mod_watchlist")
    keys = [r["key"] for r in wl_sec["rows"]]
    assert "nav.watchlist" in keys
    assert FEATURE_WATCHLIST_DEEP_ANALYSIS in keys
    child = next(r for r in wl_sec["rows"] if r["key"] == FEATURE_WATCHLIST_DEEP_ANALYSIS)
    assert child["row_type"] == "action"
    assert child["parent_nav_key"] == "nav.watchlist"
    assert child.get("inherit_note")


@pytest.mark.unit
def test_build_feature_registry_includes_deep_analysis():
    flat = em.build_feature_registry()
    keys = [r["key"] for r in flat]
    assert FEATURE_WATCHLIST_DEEP_ANALYSIS in keys


@pytest.mark.unit
def test_user_tier_for_matrix():
    assert em.user_tier_for_matrix({"is_admin": True}) == "premium"
    assert em.user_tier_for_matrix({"service_level": "vip"}) == "vip"
    assert em.user_tier_for_matrix({"service_level": "unknown"}) == "free"
    assert em.user_tier_for_matrix({}) == "free"


@pytest.mark.unit
def test_resolve_deep_analysis_admin_always_inherit(monkeypatch):
    monkeypatch.setattr(em, "get_merged_matrix_cells", lambda: {})
    out = em.resolve_deep_analysis_entitlement({"is_admin": True, "service_level": "free"})
    assert out["type"] == "inherit"


@pytest.mark.unit
def test_resolve_deep_analysis_blocked(monkeypatch):
    cells = {
        FEATURE_WATCHLIST_DEEP_ANALYSIS: {
            "free": {"click": "blocked", "weekly_limit": None},
        }
    }
    monkeypatch.setattr(em, "get_merged_matrix_cells", lambda: cells)
    out = em.resolve_deep_analysis_entitlement(
        {"service_level": "free", "id": "u1"}
    )
    assert out["type"] == "blocked"


@pytest.mark.unit
def test_resolve_deep_analysis_matrix_with_weekly_limit(monkeypatch):
    cells = {
        FEATURE_WATCHLIST_DEEP_ANALYSIS: {
            "free": {"click": "allowed", "weekly_limit": 2},
        }
    }
    monkeypatch.setattr(em, "get_merged_matrix_cells", lambda: cells)
    out = em.resolve_deep_analysis_entitlement(
        {"service_level": "free", "id": "u1"}
    )
    assert out["type"] == "matrix"
    assert out["weekly_limit"] == 2
    assert out["feature_key"] == FEATURE_WATCHLIST_DEEP_ANALYSIS


@pytest.mark.unit
def test_resolve_deep_analysis_allowed_without_limit_is_inherit(monkeypatch):
    cells = {
        FEATURE_WATCHLIST_DEEP_ANALYSIS: {
            "free": {"click": "allowed", "weekly_limit": None},
        }
    }
    monkeypatch.setattr(em, "get_merged_matrix_cells", lambda: cells)
    out = em.resolve_deep_analysis_entitlement(
        {"service_level": "free", "id": "u1"}
    )
    assert out["type"] == "inherit"


@pytest.mark.unit
def test_merge_cells_preserves_new_action_defaults():
    merged = em._merge_cells(None)
    assert FEATURE_WATCHLIST_DEEP_ANALYSIS in merged
    assert merged[FEATURE_WATCHLIST_DEEP_ANALYSIS]["free"]["click"] == "inherit"


@pytest.mark.unit
def test_validate_normalize_cells_roundtrip():
    raw = em._default_cells()
    normalized = em._validate_and_normalize_cells(raw)
    assert normalized[FEATURE_WATCHLIST_DEEP_ANALYSIS]["free"]["click"] == "inherit"


class _FakeQuotaUsage:
    """Minimal in-memory quota_usage for QuotaManager tests."""

    def __init__(self):
        self.docs = []

    def _match(self, doc, query):
        for k, v in query.items():
            if doc.get(k) != v:
                return False
        return True

    def find_one(self, query, sort=None):
        for d in self.docs:
            if self._match(d, query):
                return dict(d)
        return None

    def update_one(self, filter_q, update, upsert=False):
        inc = update.get("$inc", {})
        soi = update.get("$setOnInsert", {})
        for i, d in enumerate(self.docs):
            if self._match(d, filter_q):
                nd = dict(d)
                for k, v in inc.items():
                    nd[k] = nd.get(k, 0) + v
                nd["updated_at"] = datetime.now()
                self.docs[i] = nd
                return
        if upsert:
            base = {k: v for k, v in filter_q.items()}
            for k, v in soi.items():
                base.setdefault(k, v)
            for k, v in inc.items():
                base[k] = base.get(k, 0) + v
            self.docs.append(base)


class _FakeDB:
    def __init__(self):
        self._quota_usage = _FakeQuotaUsage()

    def __getitem__(self, name):
        if name == "quota_usage":
            return self._quota_usage
        raise KeyError(name)


@pytest.mark.unit
def test_quota_manager_feature_weekly_check_consume_remaining():
    db = _FakeDB()
    qm = QuotaManager(db)
    uid = "user-1"
    fk = "action.watchlist.deep_analysis"

    assert qm.check_feature_weekly_quota(uid, fk, 2) is True
    assert qm.get_remaining_feature_weekly_quota(uid, fk, 2) == 2

    qm.consume_feature_weekly_quota(uid, fk)
    assert qm.check_feature_weekly_quota(uid, fk, 2) is True
    assert qm.get_remaining_feature_weekly_quota(uid, fk, 2) == 1

    qm.consume_feature_weekly_quota(uid, fk)
    assert qm.check_feature_weekly_quota(uid, fk, 2) is False
    assert qm.get_remaining_feature_weekly_quota(uid, fk, 2) == 0


@pytest.mark.unit
def test_quota_manager_feature_weekly_zero_limit_always_denied():
    db = _FakeDB()
    qm = QuotaManager(db)
    assert qm.check_feature_weekly_quota("u", "f", 0) is False
