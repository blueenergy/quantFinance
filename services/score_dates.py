"""Pick the score date a ranking should use for one index scope.

Scores are written per run, so an index only has a full cross-section on the
days that run scored it. The nightly scoring universe was narrowed to csi1000
on 2026-11-23: the other five indices stopped being written, except for the
members that happen to sit in the surviving universe — a500 shares 41 members
with csi1000, and those 41 are re-scored every day.

That makes "the latest date that carries this index tag" a partial
cross-section. Ranking on it silently drops every member that was not
re-scored, so the date has to be the newest one that still covers the index.
"""

from typing import Any, Dict, Optional

# A date counts as a usable cross-section when it holds at least this many
# rows for the index. Same thresholds the scorer uses to decide whether a
# trading day was actually scored for an index (quant-scorer's
# INDEX_MIN_SYMBOLS).
SCORE_DATE_MIN_ROWS: Dict[str, int] = {
    "hs300": 250,
    "a500": 400,
    "csi500": 400,
    "csi1000": 800,
    "csi2000": 1500,
    "star50": 40,
}


def pinned_index_score_date(stock_scores: Any, index_code: str) -> Optional[str]:
    """Newest score date holding a full cross-section of ``index_code``.

    Walks back from the newest tagged date until one has enough rows. Falls
    back to the newest tagged date if no day ever did — answering with a
    slightly thin ranking beats refusing to answer.
    """
    floor = SCORE_DATE_MIN_ROWS.get(index_code, 1)
    fallback: Optional[str] = None
    date: Optional[str] = None
    while True:
        query: Dict[str, Any] = {"index_codes": index_code}
        if date:
            query["score_date"] = {"$lt": date}
        doc = stock_scores.find_one(
            query,
            sort=[("score_date", -1)],
            projection={"_id": 0, "score_date": 1},
        )
        if not doc or not doc.get("score_date"):
            break
        date = str(doc["score_date"])
        if fallback is None:
            fallback = date
        rows = stock_scores.count_documents({"index_codes": index_code, "score_date": date})
        if rows >= floor:
            return date
    return fallback
