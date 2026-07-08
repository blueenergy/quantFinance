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
