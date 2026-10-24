"""Shenwan (SW2021) industry membership helpers for ranking views.

``sw_index_member`` is a stint table: one stock can carry several historical
assignments and ``out_date`` is the last day *in* that industry (inclusive).
The ranking page must compare stocks against the industry they belonged to on
the score date, not the one they belong to today, so the loader is PIT-aware
when ``as_of`` is supplied.

The helpers accept either a ``{name: collection}`` mapping (``get_data_db()``)
or any ``__getitem__`` container so routers and tests can inject fakes.
Missing collections or query failures degrade to an empty mapping instead of
breaking the ranking endpoint.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence

SW_SRC_DEFAULT = "SW2021"
SW_MEMBER_COLLECTION = "sw_index_member"
SW_CLASSIFY_COLLECTION = "sw_index_classify"

_QUERY_CHUNK = 1000


def _get_collection(container: Any, name: str) -> Any:
    if container is None:
        return None
    if isinstance(container, dict):
        return container.get(name)
    try:
        return container[name]
    except Exception:
        return getattr(container, name, None)


def _chunks(items: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _normalize_date(value: Any) -> str:
    text = str(value or "").strip()
    digits = text.replace("-", "").replace("/", "")[:8]
    return digits if len(digits) == 8 and digits.isdigit() else ""


def _symbol_variants(symbol: Any) -> List[str]:
    text = str(symbol or "").strip()
    if not text:
        return []
    variants = [text]
    base = text.split(".", 1)[0]
    if base and base != text:
        variants.append(base)
    elif base:
        variants.extend([f"{base}.SH", f"{base}.SZ", f"{base}.BJ"])
    return list(dict.fromkeys(variants))


def _membership_row(doc: Dict[str, Any]) -> Dict[str, str]:
    return {
        "l1_code": str(doc.get("l1_code") or "").strip(),
        "l1_name": str(doc.get("l1_name") or "").strip(),
        "l2_code": str(doc.get("l2_code") or "").strip(),
        "l2_name": str(doc.get("l2_name") or "").strip(),
    }


def load_sw_membership(
    data_collections: Any,
    symbols: Sequence[str],
    *,
    as_of: Any = None,
    src: str = SW_SRC_DEFAULT,
) -> Dict[str, Dict[str, str]]:
    """Map requested symbol -> SW L1/L2 membership.

    ``as_of`` (YYYYMMDD or YYYY-MM-DD) switches to point-in-time resolution;
    without it the current snapshot (``is_new='Y'``) is used.
    """
    requested = [str(s).strip() for s in (symbols or []) if str(s).strip()]
    if not requested:
        return {}

    member_coll = _get_collection(data_collections, SW_MEMBER_COLLECTION)
    if member_coll is None:
        return {}

    variants: List[str] = []
    variant_to_symbol: Dict[str, str] = {}
    for symbol in requested:
        for variant in _symbol_variants(symbol):
            variant_to_symbol.setdefault(variant, symbol)
            variants.append(variant)
    variants = list(dict.fromkeys(variants))

    date_key = _normalize_date(as_of)
    best: Dict[str, Dict[str, Any]] = {}

    projection = {
        "_id": 0,
        "con_code": 1,
        "in_date": 1,
        "out_date": 1,
        "l1_code": 1,
        "l1_name": 1,
        "l2_code": 1,
        "l2_name": 1,
    }

    for chunk in _chunks(variants, _QUERY_CHUNK):
        query: Dict[str, Any] = {"src": src, "con_code": {"$in": list(chunk)}}
        if date_key:
            query["in_date"] = {"$lte": date_key}
            query["$or"] = [
                {"out_date": None},
                {"out_date": ""},
                {"out_date": {"$gte": date_key}},
            ]
        else:
            query["is_new"] = "Y"
        try:
            docs = list(member_coll.find(query, projection))
        except Exception:
            continue
        for doc in docs:
            symbol = variant_to_symbol.get(str(doc.get("con_code") or "").strip())
            if not symbol:
                continue
            previous = best.get(symbol)
            in_date = str(doc.get("in_date") or "")
            if previous is not None and str(previous.get("_in_date") or "") >= in_date:
                continue
            row = _membership_row(doc)
            row["_in_date"] = in_date
            best[symbol] = row

    return {
        symbol: {key: value for key, value in row.items() if key != "_in_date"}
        for symbol, row in best.items()
    }


def sw_catalog(
    data_collections: Any,
    level: str,
    *,
    src: str = SW_SRC_DEFAULT,
) -> List[Dict[str, str]]:
    """Return the SW catalog for ``level`` (L1/L2/L3), sorted by index code.

    L1 rows expose ``industry_code`` so the UI can match an L2 row's
    ``parent_code``; L2 rows expose that same ``parent_code``.
    """
    classify_coll = _get_collection(data_collections, SW_CLASSIFY_COLLECTION)
    if classify_coll is None:
        return []

    normalized_level = str(level or "").strip().upper()
    if normalized_level not in {"L1", "L2", "L3"}:
        return []

    try:
        docs = list(
            classify_coll.find(
                {"src": src, "level": normalized_level},
                {
                    "_id": 0,
                    "index_code": 1,
                    "industry_code": 1,
                    "industry_name": 1,
                    "name": 1,
                    "parent_code": 1,
                },
            )
        )
    except Exception:
        return []

    rows: List[Dict[str, str]] = []
    for doc in docs:
        code = str(doc.get("index_code") or "").strip()
        if not code:
            continue
        rows.append(
            {
                "code": code,
                "name": str(doc.get("industry_name") or doc.get("name") or code).strip(),
                "industry_code": str(doc.get("industry_code") or "").strip(),
                "parent_code": str(doc.get("parent_code") or "").strip(),
                "level": normalized_level,
            }
        )
    rows.sort(key=lambda item: item["code"])
    return rows
