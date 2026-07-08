"""Unit tests for materialized live lineage snapshots."""

from __future__ import annotations

import time

from pymongo.errors import DuplicateKeyError

from services.portfolio_lineage_live import build_lineage_live_positions, build_lineage_live_trades
from services.portfolio_live_snapshot import (
    overlay_realtime,
    probe_stale,
    rebuild_live_snapshot,
    rebuild_snapshots_for_recent_terminal_executions,
    reconcile_live_snapshots_with_broker_positions,
)


class _Collection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])
        self.writes = []

    def find(self, query=None, projection=None):
        query = query or {}

        def _match(doc):
            for key, value in query.items():
                if key == "$or":
                    if not any(_match({**doc, **branch}) for branch in value):
                        return False
                    continue
                if isinstance(value, dict) and "$in" in value:
                    if doc.get(key) not in value["$in"]:
                        return False
                elif isinstance(value, dict):
                    actual = doc.get(key)
                    if "$exists" in value and (key in doc) != bool(value["$exists"]):
                        return False
                    if "$ne" in value and actual == value["$ne"]:
                        return False
                    if "$gte" in value and (actual is None or actual < value["$gte"]):
                        return False
                    if "$lte" in value and (actual is None or actual > value["$lte"]):
                        return False
                elif doc.get(key) != value:
                    return False
            return True

        rows = [doc for doc in self.docs if _match(doc)]
        return _Cursor(rows)

    def find_one(self, query=None, projection=None, sort=None):
        rows = list(self.find(query, projection))
        if sort:
            key = sort[0][0]
            reverse = sort[0][1] == -1
            rows.sort(key=lambda row: str(row.get(key) or ""), reverse=reverse)
        return rows[0] if rows else None

    def count_documents(self, query=None):
        return len(list(self.find(query or {})))

    def update_one(self, query, update, upsert=False):
        self.writes.append((query, update, upsert))
        set_fields = (update or {}).get("$set", {})
        for idx, doc in enumerate(self.docs):
            if all(doc.get(k) == v for k, v in query.items()):
                self.docs[idx] = {**doc, **set_fields}
                return
        if upsert:
            self.docs.append({**query, **set_fields})


class _Cursor:
    def __init__(self, rows):
        self._rows = rows

    def sort(self, key, direction=1):
        reverse = direction == -1
        self._rows.sort(key=lambda row: row.get(key) or 0, reverse=reverse)
        return self

    def limit(self, n):
        self._rows = self._rows[:n]
        return self

    def __iter__(self):
        return iter(self._rows)

    def __list__(self):
        return list(self._rows)


def _trading_collections():
    return {
        "portfolio_trade_plans": _Collection(
            [
                {
                    "plan_id": "pla",
                    "strategy_template_id": "stx",
                    "user_id": "user-1",
                    "base_date": "20250101",
                    "params_hash": "hx",
                    "params_snapshot": {"initial_capital": 1_000_000},
                }
            ]
        ),
        "portfolio_live_snapshots": _Collection(),
        "portfolio_lineage_controls": _Collection(),
    }


def _finance_collections():
    return {
        "trade_signals": _Collection(
            [
                {
                    "order_id": "oa",
                    "user_id": "user-1",
                    "mode": "live",
                    "plan_id": "pla",
                    "symbol": "000001.SZ",
                    "name": "Ping",
                    "securities_account_id": "acct-1",
                    "created_at": 1,
                }
            ]
        ),
        "trade_executions": _Collection(
            [
                {
                    "order_id": "oa",
                    "user_id": "user-1",
                    "securities_account_id": "acct-1",
                    "symbol": "000001.SZ",
                    "side": "BUY",
                    "filled_size": 100,
                    "filled_price": 10.0,
                    "timestamp": 1.0,
                    "updated_at": "2025-01-01T10:00:00",
                }
            ]
        ),
    }


def test_rebuild_live_snapshot_writes_structural_positions_and_trades():
    trading = _trading_collections()
    finance = _finance_collections()
    plan = trading["portfolio_trade_plans"].docs[0]

    doc = rebuild_live_snapshot(
        trading,
        finance,
        plan,
        "user-1",
        anchor_plan_id="pla",
        reason="execution",
        trigger_order_id="oa",
    )

    assert doc is not None
    assert doc["positions"][0]["symbol"] == "000001.SZ"
    assert doc["positions"][0]["shares"] == 100
    assert doc["trades"]
    assert doc["watermark"]["execution_count"] == 1
    assert doc["last_rebuild_reason"] == "execution"
    assert doc["last_rebuild_trigger_order_id"] == "oa"
    assert trading["portfolio_live_snapshots"].docs


def test_terminal_execution_backfill_rebuilds_direct_mongo_snapshot(monkeypatch):
    from services import portfolio_live_snapshot as pls

    trading = _trading_collections()
    finance = _finance_collections()
    now = 1_800_000_000.0
    finance["trade_executions"].docs[0].update(
        {
            "status": "filled",
            "ingest_source": "mongo_direct",
            "timestamp": now - 1,
            "ingested_at": now - 1,
        }
    )
    finance["job_status"] = _Collection()
    calls = []

    def _fake_rebuild(trading_collections, finance_collections, plan, user_id, *, reason="", trigger_order_id=""):
        calls.append(
            {
                "plan_id": plan.get("plan_id"),
                "user_id": user_id,
                "reason": reason,
                "trigger_order_id": trigger_order_id,
            }
        )
        return {"ok": True}

    monkeypatch.setattr(pls, "rebuild_live_snapshot_for_plan_guarded", _fake_rebuild)

    result = rebuild_snapshots_for_recent_terminal_executions(
        trading,
        finance,
        now=now,
        lookback_seconds=60,
    )

    assert result["scanned"] == 1
    assert result["candidates"] == 1
    assert result["rebuilt"] == 1
    assert calls == [
        {
            "plan_id": "pla",
            "user_id": "user-1",
            "reason": "execution_backfill",
            "trigger_order_id": "oa",
        }
    ]
    assert finance["job_status"].docs[0]["status"] == "ok"


def test_terminal_execution_backfill_skips_fresh_snapshot(monkeypatch):
    from services import portfolio_live_snapshot as pls

    trading = _trading_collections()
    finance = _finance_collections()
    now = 1_800_000_000.0
    finance["trade_executions"].docs[0].update(
        {
            "status": "filled",
            "ingest_source": "mongo_direct",
            "timestamp": now - 1,
            "ingested_at": now - 1,
        }
    )
    trading["portfolio_live_snapshots"].docs.append(
        {
            "user_id": "user-1",
            "strategy_template_id": "stx",
            "params_hash": "hx",
            "securities_account_id": "acct-1",
            "watermark": {
                "execution_count": 1,
                "max_execution_ts": now - 1,
                "last_execution_updated_at": "2025-01-01T10:00:00",
            },
        }
    )
    finance["job_status"] = _Collection()
    calls = []
    monkeypatch.setattr(
        pls,
        "rebuild_live_snapshot_for_plan_guarded",
        lambda *args, **kwargs: calls.append(kwargs) or {"ok": True},
    )

    result = rebuild_snapshots_for_recent_terminal_executions(
        trading,
        finance,
        now=now,
        lookback_seconds=60,
    )

    assert result["scanned"] == 1
    assert result["candidates"] == 1
    assert result["rebuilt"] == 0
    assert result["skipped_fresh"] == 1
    assert calls == []


def test_overlay_realtime_matches_direct_position_builder():
    executions = [
        {
            "order_id": "oa",
            "symbol": "000001.SZ",
            "name": "Ping",
            "side": "BUY",
            "filled_size": 100,
            "filled_price": 10.0,
            "timestamp": 1.0,
        }
    ]
    price_map = {"000001.SZ": 12.0}
    direct = build_lineage_live_positions(executions, price_map, name_map={"000001.SZ": "Ping"})
    snapshot = {
        "positions": [
            {
                "symbol": "000001.SZ",
                "name": "Ping",
                "shares": 100,
                "avg_cost": 10.0,
                "cost_basis": 1000.0,
                "realized_pnl": 0.0,
            }
        ],
        "trades": build_lineage_live_trades(executions, price_map, name_map={"000001.SZ": "Ping"}),
        "book_equity": {
            "initial_capital": 1_000_000,
            "cash": 999_000,
            "realized_pnl": 0.0,
            "execution_count": 1,
        },
        "lineage_plan_ids": ["pla"],
    }
    overlay = overlay_realtime(snapshot, price_map)

    assert overlay["positions"][0]["last_price"] == direct["positions"][0]["last_price"]
    assert overlay["positions"][0]["market_value"] == direct["positions"][0]["market_value"]
    assert overlay["positions"][0]["unrealized_pnl"] == direct["positions"][0]["unrealized_pnl"]
    assert overlay["current_book_equity"]["market_value"] == direct["summary"]["total_market_value"]


def test_probe_stale_detects_newer_execution():
    snapshot = {
        "user_id": "user-1",
        "securities_account_id": "acct-1",
        "watermark": {
            "execution_count": 1,
            "last_execution_updated_at": "2025-01-01T10:00:00",
            "max_execution_ts": 1.0,
        },
    }
    finance = {
        "trade_executions": _Collection(
            [
                {
                    "user_id": "user-1",
                    "securities_account_id": "acct-1",
                    "updated_at": "2025-01-02T10:00:00",
                    "timestamp": 2.0,
                }
            ]
        )
    }
    assert probe_stale(finance, snapshot) is True

    finance["trade_executions"].docs[0]["updated_at"] = "2025-01-01T10:00:00"
    finance["trade_executions"].docs[0]["timestamp"] = 1.0
    assert probe_stale(finance, snapshot) is False


def test_probe_stale_detects_newer_execution_without_updated_at():
    # Regression: executions written by the trader upload historically carried
    # no updated_at. Sorting by updated_at surfaced an arbitrary old doc, so a
    # fresh fill (larger timestamp) was missed and holdings never refreshed.
    snapshot = {
        "user_id": "user-1",
        "securities_account_id": "acct-1",
        "watermark": {
            "execution_count": 1,
            "last_execution_updated_at": "",
            "max_execution_ts": 1780000000.0,
        },
    }
    finance = {
        "trade_executions": _Collection(
            [
                {"user_id": "user-1", "securities_account_id": "acct-1", "timestamp": 1770000000.0},
                {"user_id": "user-1", "securities_account_id": "acct-1", "timestamp": 1783319619.0},
            ]
        )
    }
    assert probe_stale(finance, snapshot) is True


def test_reconcile_live_snapshots_rebuilds_and_resolves_stale_snapshot():
    trading = _trading_collections()
    finance = _finance_collections()
    trading["portfolio_live_snapshots"].docs.append(
        {
            "user_id": "user-1",
            "strategy_template_id": "stx",
            "params_hash": "hx",
            "anchor_plan_id": "pla",
            "securities_account_id": "acct-1",
            "account_positions": [],
        }
    )
    finance["trader_positions"] = _Collection(
        [
            {
                "user_id": "user-1",
                "securities_account_id": "acct-1",
                "symbol": "000001.SZ",
                "qty": 100,
            }
        ]
    )
    finance["job_status"] = _Collection()

    result = reconcile_live_snapshots_with_broker_positions(trading, finance)

    assert result["checked"] == 1
    assert result["rebuilt"] == 1
    assert result["resolved"] == 1
    assert result["mismatched"] == 0
    assert finance["job_status"].docs[0]["status"] == "ok"


def test_reconcile_live_snapshots_records_residual_broker_mismatch():
    trading = _trading_collections()
    finance = _finance_collections()
    trading["portfolio_live_snapshots"].docs.append(
        {
            "user_id": "user-1",
            "strategy_template_id": "stx",
            "params_hash": "hx",
            "anchor_plan_id": "pla",
            "securities_account_id": "acct-1",
            "account_positions": [{"symbol": "000001.SZ", "shares": 100}],
        }
    )
    finance["trader_positions"] = _Collection(
        [
            {
                "user_id": "user-1",
                "securities_account_id": "acct-1",
                "symbol": "000001.SZ",
                "qty": 200,
            }
        ]
    )
    finance["job_status"] = _Collection()

    result = reconcile_live_snapshots_with_broker_positions(trading, finance)

    assert result["checked"] == 1
    assert result["rebuilt"] == 1
    assert result["resolved"] == 0
    assert result["mismatched"] == 1
    assert result["diffs"][0]["diffs"][0]["broker_shares"] == 200
    assert finance["job_status"].docs[0]["status"] == "warning"


def test_probe_stale_ignores_execution_count_when_watermark_current():
    # Count differs from the watermark (e.g. a rare deletion) but the latest
    # execution is not newer: we intentionally skip the expensive full-account
    # count_documents and treat the snapshot as fresh (EOD rebuild reconciles).
    snapshot = {
        "user_id": "user-1",
        "securities_account_id": "acct-1",
        "watermark": {
            "execution_count": 5,
            "last_execution_updated_at": "2025-01-01T10:00:00",
            "max_execution_ts": 1.0,
        },
    }

    class _NoCountCollection(_Collection):
        def count_documents(self, query=None):
            raise AssertionError("probe_stale must not run a full-account count")

    finance = {
        "trade_executions": _NoCountCollection(
            [
                {
                    "user_id": "user-1",
                    "securities_account_id": "acct-1",
                    "updated_at": "2025-01-01T10:00:00",
                    "timestamp": 1.0,
                }
            ]
        )
    }
    assert probe_stale(finance, snapshot) is False


# --- S4-B: lease-guarded snapshot rebuild -----------------------------------


class _UpdateResult:
    def __init__(self, upserted_id=None, modified_count=0):
        self.upserted_id = upserted_id
        self.modified_count = modified_count


class _LeaseCollection:
    """Minimal job_status fake honoring the $or/upsert lease semantics."""

    def __init__(self):
        self.docs = {}

    def update_one(self, filt, update, upsert=False):
        import time as _time

        lock_id = filt["_id"]
        existing = self.docs.get(lock_id)
        now = _time.time()
        # Acquire path carries a $or freshness guard; release path filters on
        # locked_by. Honor both so the fake matches real Mongo semantics.
        if existing is not None:
            if "$or" in filt and existing.get("locked_until", 0) > now:
                return _UpdateResult(upserted_id=None, modified_count=0)
            if "locked_by" in filt and existing.get("locked_by") != filt["locked_by"]:
                return _UpdateResult(upserted_id=None, modified_count=0)
            doc = dict(existing)
            doc.update(update.get("$set", {}))
            self.docs[lock_id] = doc
            return _UpdateResult(upserted_id=None, modified_count=1)
        if not upsert:
            return _UpdateResult(upserted_id=None, modified_count=0)
        doc = {"_id": lock_id}
        doc.update(update.get("$setOnInsert", {}))
        doc.update(update.get("$set", {}))
        self.docs[lock_id] = doc
        return _UpdateResult(upserted_id=lock_id, modified_count=0)


def _plan_stub():
    return {
        "plan_id": "plan-1",
        "strategy_template_id": "tmpl-1",
        "params_hash": "abcd1234",
    }


def test_guarded_rebuild_runs_and_releases_lease(monkeypatch):
    from services import portfolio_live_snapshot as pls

    calls = []
    monkeypatch.setattr(
        pls,
        "rebuild_live_snapshot_for_plan",
        lambda tc, fc, plan, uid, **kwargs: calls.append(plan["plan_id"]) or {"ok": True},
    )
    lease_coll = _LeaseCollection()
    finance = {"job_status": lease_coll}

    result = pls.rebuild_live_snapshot_for_plan_guarded({}, finance, _plan_stub(), "user-1")

    assert result == {"ok": True}
    assert calls == ["plan-1"]
    lock_doc = next(iter(lease_coll.docs.values()))
    assert lock_doc["job_type"] == "snapshot_rebuild_lock"
    assert lock_doc["date"].startswith("snapshot_rebuild_lock:")
    assert lock_doc["locked_until"] <= time.time() + 1


def test_guarded_rebuild_skips_when_lease_held(monkeypatch):
    from services import portfolio_live_snapshot as pls

    calls = []
    monkeypatch.setattr(
        pls,
        "rebuild_live_snapshot_for_plan",
        lambda tc, fc, plan, uid, **kwargs: calls.append(plan["plan_id"]) or {"ok": True},
    )
    lease_coll = _LeaseCollection()
    finance = {"job_status": lease_coll}
    lock_key = pls._snapshot_rebuild_lock_key(_plan_stub(), "user-1")
    assert pls._acquire_snapshot_rebuild_lease(finance, lock_key, ttl_seconds=300)

    result = pls.rebuild_live_snapshot_for_plan_guarded({}, finance, _plan_stub(), "user-1")

    assert result is None
    assert calls == []


def test_guarded_rebuild_skips_duplicate_key_race(monkeypatch):
    from services import portfolio_live_snapshot as pls

    class _DuplicateKeyLeaseCollection:
        def update_one(self, *args, **kwargs):
            raise DuplicateKeyError("duplicate lock")

    calls = []
    monkeypatch.setattr(
        pls,
        "rebuild_live_snapshot_for_plan",
        lambda tc, fc, plan, uid, **kwargs: calls.append(plan["plan_id"]) or {"ok": True},
    )
    finance = {"job_status": _DuplicateKeyLeaseCollection()}

    result = pls.rebuild_live_snapshot_for_plan_guarded({}, finance, _plan_stub(), "user-1")

    assert result is None
    assert calls == []


def test_guarded_rebuild_runs_without_lease_backend(monkeypatch):
    from services import portfolio_live_snapshot as pls

    calls = []
    monkeypatch.setattr(
        pls,
        "rebuild_live_snapshot_for_plan",
        lambda tc, fc, plan, uid, **kwargs: calls.append(plan["plan_id"]) or {"ok": True},
    )
    result = pls.rebuild_live_snapshot_for_plan_guarded({}, {}, _plan_stub(), "user-1")

    assert result == {"ok": True}
    assert calls == ["plan-1"]


def test_schedule_snapshot_rebuild_runs_in_background(monkeypatch):
    from services import portfolio_live_snapshot as pls

    calls = []
    monkeypatch.setattr(
        pls,
        "rebuild_live_snapshot_for_plan",
        lambda tc, fc, plan, uid, **kwargs: calls.append(plan["plan_id"]),
    )
    finance = {"job_status": _LeaseCollection()}

    thread = pls.schedule_snapshot_rebuild({}, finance, _plan_stub(), "user-1", reason="test")
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert calls == ["plan-1"]
