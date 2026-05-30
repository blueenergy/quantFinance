import pytest

from services import data_monitoring


class _FakeCollection:
    def __init__(self, name, docs):
        self.name = name
        self.docs = list(docs)
        self.lookup_collection = None

    def count_documents(self, query):
        return sum(1 for doc in self.docs if _matches(doc, query))

    def aggregate(self, pipeline):
        if self.name == "financial_income":
            return self._aggregate_data_coverage(pipeline)
        if self.name == "stock_info":
            return self._aggregate_universe_gaps(pipeline)
        raise AssertionError(f"unexpected aggregate on {self.name}")

    def _aggregate_data_coverage(self, pipeline):
        data_match = pipeline[0]["$match"]
        universe_match = pipeline[1]["$lookup"]["pipeline"][0]["$match"]
        distinct_codes = set()
        for doc in self.docs:
            if not _matches(doc, data_match):
                continue
            code = doc.get("ts_code")
            if any(_matches({**u, "ts_code": u.get("ts_code")}, {**universe_match, "ts_code": code}) for u in self.lookup_collection.docs):
                distinct_codes.add(code)
        return [{"n": len(distinct_codes)}] if distinct_codes else []

    def _aggregate_universe_gaps(self, pipeline):
        universe_match = pipeline[0]["$match"]
        lookup_expr = pipeline[1]["$lookup"]["pipeline"][0]["$match"]["$expr"]
        facet = pipeline[3]["$facet"]
        skip = facet["items"][0]["$skip"]
        limit = facet["items"][1]["$limit"]
        missing = []
        for doc in self.docs:
            if not _matches(doc, universe_match):
                continue
            variables = {"mkv": doc.get("ts_code")}
            has_hit = any(_eval_expr(data_doc, lookup_expr, variables) for data_doc in self.lookup_collection.docs)
            if not has_hit:
                missing.append({"ts_code": doc.get("ts_code"), "name": doc.get("name")})
        return [{"items": missing[skip : skip + limit], "cnt": [{"n": len(missing)}]}]


def _matches(doc, query):
    for key, expected in (query or {}).items():
        if key == "$and":
            if not all(_matches(doc, item) for item in expected):
                return False
            continue
        if key == "$or":
            if not any(_matches(doc, item) for item in expected):
                return False
            continue
        value = doc.get(key)
        if isinstance(expected, dict):
            if "$exists" in expected and (key in doc) != expected["$exists"]:
                return False
            if "$nin" in expected and value in expected["$nin"]:
                return False
            if "$lte" in expected:
                try:
                    if value is None or value > expected["$lte"]:
                        return False
                except TypeError:
                    return False
            continue
        if value != expected:
            return False
    return True


def _eval_expr(doc, expr, variables):
    if "$and" in expr:
        return all(_eval_expr(doc, item, variables) for item in expr["$and"])
    if "$or" in expr:
        return any(_eval_expr(doc, item, variables) for item in expr["$or"])
    if "$eq" in expr:
        left, right = expr["$eq"]
        return _resolve_expr_value(doc, left, variables) == _resolve_expr_value(doc, right, variables)
    raise AssertionError(f"unsupported expr: {expr}")


def _resolve_expr_value(doc, value, variables):
    if isinstance(value, str) and value.startswith("$$"):
        return variables.get(value[2:])
    if isinstance(value, str) and value.startswith("$"):
        return doc.get(value[1:])
    return value


@pytest.fixture
def financial_coverage_collections():
    universe = _FakeCollection(
        "stock_info",
        [
            {"ts_code": "000001.SZ", "name": "old-covered", "list_date": "20200101"},
            {"ts_code": "000002.SZ", "name": "old-missing", "list_date": "20200101"},
            {"ts_code": "000003.SZ", "name": "new-after-period", "list_date": "20260401"},
        ],
    )
    data = _FakeCollection(
        "financial_income",
        [
            {"ts_code": "000001.SZ", "end_date": "20260331"},
            {"ts_code": "000003.SZ", "end_date": "20260331"},
        ],
    )
    data.lookup_collection = universe
    universe.lookup_collection = data
    return data, universe


@pytest.mark.unit
def test_financial_coverage_excludes_stocks_listed_after_report_period(financial_coverage_collections):
    data, universe = financial_coverage_collections

    coverage = data_monitoring._coverage_latest_ratio(
        data,
        "end_date",
        "20260331",
        universe,
        "ts_code",
        {"field": "list_date", "op": "lte_period"},
    )

    assert coverage["coverage_denominator"] == 2
    assert coverage["coverage_numerator"] == 1
    assert coverage["coverage_pct"] == 50.0


@pytest.mark.unit
def test_financial_coverage_gaps_exclude_stocks_listed_after_report_period(monkeypatch, financial_coverage_collections):
    data, universe = financial_coverage_collections

    monkeypatch.setattr(
        data_monitoring,
        "resolve_coverage_gap_config",
        lambda database, collection: {
            "database": "quant_data",
            "collection": "financial_income",
            "field": "end_date",
            "field_kind": "date_string",
            "universe_database": "quant_data",
            "universe_collection": "stock_info",
            "match_key": "ts_code",
            "universe_period_filter": {"field": "list_date", "op": "lte_period"},
        },
    )
    monkeypatch.setattr(
        data_monitoring,
        "_get_collection",
        lambda database, collection: data if collection == "financial_income" else universe,
    )

    gaps = data_monitoring.query_coverage_gaps(
        database="quant_data",
        collection="financial_income",
        period="20260331",
        limit=200,
    )

    assert gaps["coverage_denominator"] == 2
    assert gaps["coverage_numerator"] == 1
    assert gaps["missing_total"] == 1
    assert [row["ts_code"] for row in gaps["items"]] == ["000002.SZ"]
