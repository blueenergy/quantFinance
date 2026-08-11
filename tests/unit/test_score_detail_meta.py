"""Tests for score detail provenance helpers."""

from services.score_detail_meta import (
    SCORE_DIMENSION_KEYS,
    SCORE_HISTORY_PROJECTION,
    build_score_detail_payload,
    build_score_meta,
    extract_express_flags,
    get_strategy_recommendations,
    submodule_weights_from_details,
)


def test_submodule_weights_read_from_formula_steps():
    details = {
        "_formula": {
            "base": 0,
            "steps": [
                {"rule": "短期周期(5-10日)", "delta": 33.2, "weight": 0.4},
                {"rule": "中期周期(20-60日)", "delta": 30.8, "weight": 0.35},
                {"rule": "长期周期(120-250日)", "delta": 21.25, "weight": 0.25},
            ],
        }
    }
    weights = submodule_weights_from_details(details)
    assert weights == {
        "短期周期(5-10日)": 0.4,
        "中期周期(20-60日)": 0.35,
        "长期周期(120-250日)": 0.25,
    }


def test_submodule_weights_absent_lets_caller_fall_back():
    """Details from scorers without a ledger must return None, not an empty dict."""
    assert submodule_weights_from_details({}) is None
    assert submodule_weights_from_details(None) is None
    assert submodule_weights_from_details({"_formula": {"steps": []}}) is None
    # Flat bonuses carry no weight, so they cannot pose as a weight table.
    assert submodule_weights_from_details(
        {"_formula": {"steps": [{"rule": "5日趋势", "delta": 10}]}}
    ) is None
    # Booleans are ints in Python; they must not be mistaken for a weight.
    assert submodule_weights_from_details(
        {"_formula": {"steps": [{"rule": "x", "delta": 1, "weight": True}]}}
    ) is None


def test_score_history_projection_excludes_detail_blobs():
    """History listings must stay lean: scores only, never the *_details payloads."""
    assert SCORE_HISTORY_PROJECTION["_id"] == 0
    for key in SCORE_DIMENSION_KEYS:
        assert SCORE_HISTORY_PROJECTION[f"{key}_score"] == 1
    assert SCORE_HISTORY_PROJECTION["score_date"] == 1
    assert SCORE_HISTORY_PROJECTION["composite_score"] == 1
    assert not any(field.endswith("_details") for field in SCORE_HISTORY_PROJECTION)


def test_build_score_meta_from_stored_block():
    doc = {
        "score_date": "20240809",
        "algorithm_version": "v0.2",
        "score_meta": {
            "details_schema_version": 1,
            "lookahead_rule": "custom rule",
            "reduced_mode": False,
            "dimensions_computed": ["growth", "cycle"],
        },
        "growth_score": 72,
        "cycle_score": 65,
    }
    meta = build_score_meta(doc)
    assert meta["details_schema_version"] == 1
    assert meta["lookahead_rule"] == "custom rule"
    assert meta["score_date"] == "20240809"


def test_build_score_meta_fallback_for_legacy_doc():
    doc = {
        "score_date": "20231231",
        "algorithm_version": "v0.1",
        "growth_score": 70,
        "fundamental_score": 60,
        "composite_score": None,
    }
    meta = build_score_meta(doc)
    assert meta["details_schema_version"] == 0
    assert meta["reduced_mode"] is True
    assert "growth" in meta["dimensions_computed"]
    assert "fundamental" in meta["dimensions_computed"]


def test_extract_express_flags():
    details = {
        "express_source": "express",
        "express_discount": 0.85,
        "数据来源": "express",
    }
    flags = extract_express_flags(details)
    assert flags["express_source"] == "express"
    assert flags["express_discount"] == 0.85
    assert flags["data_source"] == "express"


def test_get_strategy_recommendations():
    doc = {
        "strategy_scores": {
            "balanced": {
                "comp_score": 75.5,
                "weight": {"growth": 0.15},
                "recommendations": {
                    "overall_rating": "推荐",
                    "strengths": ["growth: 80.0分"],
                    "weaknesses": [],
                    "risk_level": "中等风险",
                },
            }
        }
    }
    rec = get_strategy_recommendations(doc, strategy="balanced")
    assert rec is not None
    assert rec["overall_rating"] == "推荐"
    assert rec["comp_score"] == 75.5


def test_build_score_detail_payload_includes_meta_and_recommendations():
    doc = {
        "score_date": "20240809",
        "algorithm_version": "v0.1",
        "growth_score": 80,
        "strategy_scores": {
            "balanced": {
                "comp_score": 75,
                "recommendations": {"overall_rating": "推荐"},
            }
        },
    }
    payload = build_score_detail_payload(
        doc,
        symbol="000001.SZ",
        category="composite",
        details={"综合得分(当前策略)": 75},
        dimensions=[],
        strategy="balanced",
    )
    assert payload["meta"]["details_schema_version"] == 0
    assert payload["recommendations"]["overall_rating"] == "推荐"
