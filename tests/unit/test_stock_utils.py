import json
from datetime import date, datetime

from utils.stock_utils import serialize_doc


def test_serialize_doc_marks_naive_datetime_as_utc():
    doc = {
        "created_at": datetime(2026, 7, 8, 2, 28, 22, 184000),
        "trade_date": date(2026, 7, 8),
    }

    out = serialize_doc(doc)

    assert out["created_at"] == "2026-07-08T02:28:22.184000+00:00"
    assert out["trade_date"] == "2026-07-08"


def test_serialize_doc_replaces_non_finite_floats():
    doc = {"nan": float("nan"), "inf": float("inf"), "neg_inf": float("-inf"), "nested": [float("nan"), 2]}

    out = serialize_doc(doc)

    assert out["nan"] is None
    assert out["inf"] is None
    assert out["neg_inf"] is None
    assert out["nested"] == [None, 2]
    # Starlette's JSONResponse uses allow_nan=False.
    json.dumps(out, allow_nan=False)
