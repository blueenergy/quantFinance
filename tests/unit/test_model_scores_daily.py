"""Unit tests for the daily model-score panel service."""

from __future__ import annotations

from datetime import datetime

import mongomock
import pytest

from services.model_scores_daily import (
    MAX_MODELS,
    build_daily_model_scores,
    list_model_catalog,
    normalize_model_ids,
    resolve_trade_date,
)

_DATE = "20261113"
_PREV = "20261112"


def _db():
    return mongomock.MongoClient()["quant_ml"]


def _seed_predictions(db, model_id, rows, *, trade_date=_DATE, index_code="csi1000", created_at=None):
    ts = created_at or datetime(2026, 11, 13, 14, 30)
    db["model_predictions"].insert_many(
        [
            {
                "model_id": model_id,
                "trade_date": trade_date,
                "ts_code": symbol,
                "score": score,
                "index_code": index_code,
                "created_at": ts,
                "shadow": True,
                "research": False,
                "source": "shadow",
            }
            for symbol, score in rows
        ]
    )


def _names(db):
    db["stock_info"].insert_many(
        [
            {"symbol": symbol, "name": f"名{symbol[:6]}", "industry": "电子"}
            for symbol in ("000001.SZ", "000002.SZ", "000003.SZ", "000004.SZ", "000005.SZ")
        ]
    )


def test_normalize_model_ids_dedupes_and_caps():
    assert normalize_model_ids("a, b,a ,") == ["a", "b"]
    assert normalize_model_ids(["a", " a "]) == ["a"]
    with pytest.raises(ValueError, match="at most"):
        normalize_model_ids(",".join(f"m{i}" for i in range(MAX_MODELS + 1)))


def test_rank_and_percentile_single_model():
    db = _db()
    _seed_predictions(
        db,
        "m1",
        [("000001.SZ", 0.9), ("000002.SZ", 0.7), ("000003.SZ", 0.7), ("000004.SZ", 0.1)],
    )
    payload = build_daily_model_scores(db["model_predictions"], db["stock_info"], model_ids=["m1"])

    models = {model["model_id"]: model for model in payload["models"]}
    assert models["m1"]["available"] is True
    assert models["m1"]["rows"] == 4
    assert models["m1"]["index_code"] == "csi1000"

    by_symbol = {row["symbol"]: row for row in payload["rows"]}
    assert by_symbol["000001.SZ"]["models"]["m1"]["rank"] == 1
    assert by_symbol["000001.SZ"]["models"]["m1"]["pct"] == 100.0
    # Tied scores share the better rank (competition ranking).
    assert by_symbol["000002.SZ"]["models"]["m1"]["rank"] == 2
    assert by_symbol["000003.SZ"]["models"]["m1"]["rank"] == 2
    assert by_symbol["000004.SZ"]["models"]["m1"]["rank"] == 4
    assert payload["rows"][0]["symbol"] == "000001.SZ"
    assert payload["total_symbols"] == 4


def test_metadata_and_watchlist_positions_marks():
    db = _db()
    _names(db)
    _seed_predictions(db, "m1", [("000001.SZ", 0.9), ("000002.SZ", 0.5)])
    payload = build_daily_model_scores(
        db["model_predictions"],
        db["stock_info"],
        model_ids=["m1"],
        watchlist=["000001.SZ"],
        positions=["000002.SZ"],
    )
    rows = {row["symbol"]: row for row in payload["rows"]}
    assert rows["000001.SZ"]["in_watchlist"] is True
    assert rows["000001.SZ"]["in_positions"] is False
    assert rows["000002.SZ"]["in_positions"] is True
    assert rows["000002.SZ"]["name"] == "名000002"


def test_compare_two_models_correlation_and_disagreements():
    db = _db()
    left = [("000001.SZ", 5.0), ("000002.SZ", 4.0), ("000003.SZ", 3.0), ("000004.SZ", 2.0), ("000005.SZ", 1.0)]
    right = [("000002.SZ", 5.0), ("000003.SZ", 4.0), ("000004.SZ", 3.0), ("000005.SZ", 2.0), ("000001.SZ", 1.0)]
    _seed_predictions(db, "left", left)
    _seed_predictions(db, "right", right)

    payload = build_daily_model_scores(
        db["model_predictions"], db["stock_info"], model_ids=["left", "right"], top_n=2
    )

    pair = payload["correlation"][0]
    assert pair["common_symbols"] == 5
    assert pair["spearman"] is not None and pair["spearman"] < 1
    assert pair["top_n_overlap"] == 1  # only 000002 in both top-2

    disagreement = payload["disagreements"][0]
    assert [row["symbol"] for row in disagreement["only_left"]] == ["000001.SZ"]
    assert [row["symbol"] for row in disagreement["only_right"]] == ["000003.SZ"]

    first = payload["rows"][0]
    assert set(first["models"]) == {"left", "right"}
    assert first["avg_rank"] is not None


def test_delta_rank_against_previous_trade_date():
    db = _db()
    _seed_predictions(db, "m1", [("000001.SZ", 0.9), ("000002.SZ", 0.1)], trade_date=_PREV)
    _seed_predictions(db, "m1", [("000001.SZ", 0.1), ("000002.SZ", 0.9)])

    payload = build_daily_model_scores(
        db["model_predictions"], db["stock_info"], model_ids=["m1"], trade_date=_DATE
    )
    models = {model["model_id"]: model for model in payload["models"]}
    assert models["m1"]["prev_trade_date"] == _PREV

    rows = {row["symbol"]: row for row in payload["rows"]}
    # 000002 moved from rank 2 to rank 1 -> +1; 000001 fell 1 -> -1.
    assert rows["000002.SZ"]["models"]["m1"]["delta_rank"] == 1
    assert rows["000001.SZ"]["models"]["m1"]["delta_rank"] == -1


def test_cross_universe_is_rejected():
    db = _db()
    _seed_predictions(db, "csi", [("000001.SZ", 0.9)], index_code="csi1000")
    _seed_predictions(db, "hs", [("000002.SZ", 0.9)], index_code="hs300")

    with pytest.raises(ValueError, match="index universes"):
        build_daily_model_scores(db["model_predictions"], db["stock_info"], model_ids=["csi", "hs"])


def test_missing_model_on_date_is_greyed_not_fatal():
    db = _db()
    _seed_predictions(db, "live", [("000001.SZ", 0.9)])
    _seed_predictions(db, "old", [("000001.SZ", 0.5)], trade_date="20240102")

    payload = build_daily_model_scores(
        db["model_predictions"], db["stock_info"], model_ids=["live", "old"], trade_date=_DATE
    )

    models = {model["model_id"]: model for model in payload["models"]}
    assert models["live"]["available"] is True
    assert models["old"]["available"] is False
    assert models["old"]["rows"] == 0
    # No comparison when only one model has rows.
    assert payload["correlation"] == []


def test_default_date_anchors_on_first_model():
    db = _db()
    _seed_predictions(db, "live", [("000001.SZ", 0.9)], trade_date="20261113")
    _seed_predictions(db, "old", [("000001.SZ", 0.5)], trade_date="20240102")

    assert resolve_trade_date(db["model_predictions"], ["live", "old"]) == "20261113"
    payload = build_daily_model_scores(
        db["model_predictions"], db["stock_info"], model_ids=["live", "old"]
    )
    assert payload["trade_date"] == "20261113"


def test_catalog_reports_coverage_and_recent_flag():
    db = _db()
    _seed_predictions(db, "live-model", [("000001.SZ", 0.9), ("000002.SZ", 0.8)])
    _seed_predictions(db, "live-model", [("000001.SZ", 0.7)], trade_date=_PREV)
    _seed_predictions(db, "old-model", [("000001.SZ", 0.5)], trade_date="20240102")

    catalog = {row["model_id"]: row for row in list_model_catalog(db["model_predictions"])}

    assert catalog["live-model"]["date_count"] == 2
    assert catalog["live-model"]["last_trade_date"] == _DATE
    assert catalog["live-model"]["recent"] is True
    assert catalog["live-model"]["recent_trade_dates"] == [_DATE, _PREV]
    assert catalog["old-model"]["recent"] is False
    # Recent models sort first.
    assert list(catalog)[0] == "live-model"


def test_limit_caps_rows_but_not_comparison_set():
    db = _db()
    _seed_predictions(db, "m1", [(f"00000{i}.SZ", float(i)) for i in range(1, 6)])

    payload = build_daily_model_scores(
        db["model_predictions"], db["stock_info"], model_ids=["m1"], limit=2
    )
    assert len(payload["rows"]) == 2
    assert payload["total_symbols"] == 5
