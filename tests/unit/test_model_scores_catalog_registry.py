from services.model_scores_daily import list_model_catalog


class _Predictions:
    def __init__(self, rows):
        self.rows = list(rows)

    def distinct(self, field, query=None):
        rows = [row for row in self.rows if _matches(row, query)]
        return sorted({row.get(field) for row in rows if row.get(field)})

    def find_one(self, query, projection=None, sort=None):
        rows = [row for row in self.rows if _matches(row, query)]
        if not rows:
            return None
        if sort:
            key, direction = sort[0]
            rows = sorted(rows, key=lambda row: row.get(key) or "", reverse=direction < 0)
        return rows[0]

    def count_documents(self, query):
        return sum(1 for row in self.rows if _matches(row, query))


class _Registry:
    def __init__(self, docs):
        self.docs = list(docs)

    def find(self, query, projection=None):
        return [doc for doc in self.docs if _matches(doc, query)]


def _matches(doc, query):
    for key, value in (query or {}).items():
        if key == "$or":
            if not any(_matches(doc, sub) for sub in value):
                return False
            continue
        if isinstance(value, dict) and "$ne" in value:
            if doc.get(key) == value["$ne"]:
                return False
            continue
        if doc.get(key) != value:
            return False
    return True


def _rows(model_id, dates):
    return [
        {"model_id": model_id, "trade_date": date, "ts_code": f"{index:06d}.SZ", "index_code": "csi1000", "research": True, "shadow": False}
        for index, date in enumerate(dates)
    ]


def test_catalog_prefers_registry_state_and_validation():
    predictions = _Predictions(
        _rows("live-model", ["20261113", "20261116"])
        + _rows("validated-model", ["20261113"])
        + _rows("raw-model", ["20260101"])
    )
    registry = _Registry(
        [
            {
                "model_id": "live-model",
                "state": "live",
                "accepted": True,
                "accepted_reasons": ["live", "shadow"],
                "latest_validation": {"protocol": "portfolio_oos", "verdict": "weak"},
            },
            {
                "model_id": "validated-model",
                "state": "validated",
                "accepted": False,
                "accepted_reasons": [],
                "latest_validation": {"protocol": "portfolio_oos", "verdict": "weak"},
            },
            {
                "model_id": "raw-model",
                "state": "research",
                "accepted": False,
                "accepted_reasons": [],
                "latest_validation": None,
            },
        ]
    )

    rows = {row["model_id"]: row for row in list_model_catalog(predictions, registry=registry)}

    assert rows["live-model"]["accepted"] is True
    assert rows["live-model"]["accepted_reasons"] == ["live", "shadow"]
    assert rows["live-model"]["state"] == "live"
    assert rows["live-model"]["validated"] is False

    assert rows["validated-model"]["accepted"] is False
    assert rows["validated-model"]["state"] == "validated"
    assert rows["validated-model"]["validated"] is True
    assert rows["validated-model"]["validation"] == {"protocol": "portfolio_oos", "verdict": "weak"}

    assert rows["raw-model"]["accepted"] is False
    assert rows["raw-model"]["state"] == "research"
    assert rows["raw-model"]["validated"] is False


def _shadow_rows(model_id, dates):
    return [
        {"model_id": model_id, "trade_date": date, "ts_code": f"{index:06d}.SZ", "index_code": "csi1000", "research": False, "shadow": True, "source": "shadow_loop"}
        for index, date in enumerate(dates)
    ]


def test_catalog_exposes_research_and_shadow_sets_separately():
    predictions = _Predictions(
        _rows("mixed-model", ["20160413", "20160923"])
        + _shadow_rows("mixed-model", ["20261119", "20261120"])
    )
    row = next(row for row in list_model_catalog(predictions) if row["model_id"] == "mixed-model")
    sets = {item["kind"]: item for item in row["prediction_sets"]}
    assert set(sets) == {"research", "shadow"}
    assert sets["research"]["first_trade_date"] == "20160413"
    assert sets["research"]["last_trade_date"] == "20160923"
    assert sets["shadow"]["first_trade_date"] == "20261119"
    assert sets["shadow"]["last_trade_date"] == "20261120"
