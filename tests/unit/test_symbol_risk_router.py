import asyncio

import pytest

from routers import symbol_risk


class _Collection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    def find(self, query=None, projection=None):
        return _Cursor([dict(doc) for doc in self.docs if self._matches(doc, query or {})])

    def find_one(self, query=None, projection=None, sort=None):
        rows = self.find(query, projection)
        if sort:
            rows = rows.sort(sort)
        return rows[0] if rows else None

    def insert_one(self, doc):
        self.docs.append(dict(doc))

    def update_one(self, query, update, upsert=False):
        for doc in self.docs:
            if self._matches(doc, query):
                doc.update(update.get("$set", {}))
                for key in update.get("$unset", {}):
                    doc.pop(key, None)
                return _UpdateResult(1)
        if upsert:
            new_doc = dict(query)
            new_doc.update(update.get("$setOnInsert", {}))
            new_doc.update(update.get("$set", {}))
            self.docs.append(new_doc)
            return _UpdateResult(1)
        return _UpdateResult(0)

    def _get_value(self, doc, key):
        value = doc
        for part in str(key).split("."):
            if not isinstance(value, dict) or part not in value:
                return None
            value = value.get(part)
        return value

    def _matches(self, doc, query):
        for key, expected in query.items():
            value = self._get_value(doc, key)
            if isinstance(expected, dict):
                if "$in" in expected and value not in expected["$in"]:
                    return False
                continue
            if value != expected:
                return False
        return True


class _Cursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, spec):
        if not spec:
            return self
        key, direction = spec[0]
        self._docs.sort(key=lambda row: row.get(key), reverse=direction < 0)
        return self

    def __iter__(self):
        return iter(self._docs)

    def __getitem__(self, index):
        return self._docs[index]


class _UpdateResult:
    def __init__(self, modified_count):
        self.modified_count = modified_count


def _analyzer_db():
    return {
        "symbol_risk_findings": _Collection(),
        "industry_risk_findings": _Collection(),
    }


def _trading_collections():
    return {"symbol_risk_operation_logs": _Collection()}


def test_add_manual_symbol_risk(monkeypatch):
    analyzer = _analyzer_db()
    trading = _trading_collections()
    monkeypatch.setattr(symbol_risk, "get_analyzer_db", lambda: analyzer)
    monkeypatch.setattr(symbol_risk, "get_trading_db_collections", lambda: trading)

    response = symbol_risk.add_manual_symbol_risk(
        "000001.SZ",
        symbol_risk.ManualRiskFindingRequest(summary="人工录入诉讼风险", severity="high"),
        current_user={"id": "user-1"},
    )

    assert response["success"] is True
    assert analyzer["symbol_risk_findings"].docs[0]["status"] == "active"
    assert analyzer["symbol_risk_findings"].docs[0]["discovered_by"] == "manual"
    assert trading["symbol_risk_operation_logs"].docs[0]["operation_type"] == "manual_add"


def test_confirm_symbol_risk_resolution(monkeypatch):
    analyzer = _analyzer_db()
    trading = _trading_collections()
    analyzer["symbol_risk_findings"].insert_one(
        {
            "symbol": "000001.SZ",
            "finding_key": "legal_compliance:诉讼",
            "status": "active",
            "category": "legal_compliance",
            "severity": "medium",
            "summary": "诉讼",
            "resolution_mode": "event",
            "suggested_resolution": {"reason": "已和解", "by": "llm"},
        }
    )
    monkeypatch.setattr(symbol_risk, "get_analyzer_db", lambda: analyzer)
    monkeypatch.setattr(symbol_risk, "get_trading_db_collections", lambda: trading)

    response = symbol_risk.confirm_symbol_risk_resolution(
        "000001.SZ",
        "legal_compliance:诉讼",
        symbol_risk.ResolveRiskFindingRequest(reason="同意解除"),
        current_user={"id": "user-1"},
    )

    assert response["success"] is True
    row = analyzer["symbol_risk_findings"].docs[0]
    assert row["status"] == "resolved"
    assert row.get("suggested_resolution") is None
    assert trading["symbol_risk_operation_logs"].docs[0]["operation_type"] == "confirm_resolution"


def test_confirm_symbol_risk_resolution_not_found(monkeypatch):
    analyzer = _analyzer_db()
    trading = _trading_collections()
    monkeypatch.setattr(symbol_risk, "get_analyzer_db", lambda: analyzer)
    monkeypatch.setattr(symbol_risk, "get_trading_db_collections", lambda: trading)

    with pytest.raises(symbol_risk.HTTPException) as exc:
        symbol_risk.confirm_symbol_risk_resolution(
            "000001.SZ",
            "missing:key",
            symbol_risk.ResolveRiskFindingRequest(),
            current_user={"id": "user-1"},
        )

    assert exc.value.status_code == 404
