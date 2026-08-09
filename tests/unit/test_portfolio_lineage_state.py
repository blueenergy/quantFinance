from services.portfolio_lineage_state import (
    build_summary_from_lineage_plans,
    mark_lineage_live,
    resolve_execution_venue,
    upsert_lineage_summary,
)


class _FakeColl:
    def __init__(self):
        self.docs = {}

    def find_one(self, query, projection=None):
        key = (query["user_id"], query["strategy_template_id"], query["params_hash"])
        return self.docs.get(key)

    def update_one(self, query, update, upsert=False):
        key = (query["user_id"], query["strategy_template_id"], query["params_hash"])
        doc = dict(self.docs.get(key, {}))
        doc.update(query)
        doc.update(update.get("$set", {}))
        if "$setOnInsert" in update and key not in self.docs:
            doc.update(update["$setOnInsert"])
        self.docs[key] = doc


def test_resolve_execution_venue_prefers_stored_value():
    assert resolve_execution_venue({"execution_venue": "live"}, has_live_signals=False) == "live"
    assert resolve_execution_venue({"execution_venue": "paper"}, has_live_signals=True) == "paper"
    assert resolve_execution_venue(None, has_live_signals=True) == "live"
    assert resolve_execution_venue(None, has_live_signals=False) == "live"
    assert resolve_execution_venue(None, has_live_signals=False, has_paper_activity=True) == "paper"


def test_mark_lineage_live_upserts_execution_venue():
    coll = _FakeColl()
    mark_lineage_live(
        coll,
        user_id="u1",
        strategy_template_id="st1",
        params_hash="hash1",
        securities_account_id="acct-9",
    )
    doc = coll.docs[("u1", "st1", "hash1")]
    assert doc["execution_venue"] == "live"
    assert doc["securities_account_id"] == "acct-9"


def test_upsert_lineage_summary_does_not_downgrade_live_to_paper():
    coll = _FakeColl()
    coll.docs[("u1", "st1", "hash1")] = {
        "user_id": "u1",
        "strategy_template_id": "st1",
        "params_hash": "hash1",
        "execution_venue": "live",
    }
    upsert_lineage_summary(
        coll,
        user_id="u1",
        strategy_template_id="st1",
        params_hash="hash1",
        summary={"execution_venue": "paper", "latest_plan_id": "p1"},
    )
    doc = coll.docs[("u1", "st1", "hash1")]
    assert doc["execution_venue"] == "live"
    assert doc["latest_plan_id"] == "p1"


def _fake_param_summary(params):
    parts = []
    if params.get("universe_index"):
        parts.append(str(params["universe_index"]))
    if params.get("top_n") is not None:
        parts.append(f"Top{int(params['top_n'])}")
    return " · ".join(parts)


def _fake_record_kind(row):
    return row.get("record_kind")


def test_build_summary_from_lineage_plans_aggregates_and_counts_cycles():
    all_lineage = [
        {
            "plan_id": "p-old",
            "base_date": "20250101",
            "status": "executed_paper",
            "plan_type": "rebalance",
            "record_kind": "trade_plan",
            "strategy_name": "ComboA",
            "params_snapshot": {"universe_index": "csi1000", "top_n": 20, "initial_capital": 1_000_000},
        },
        {
            "plan_id": "p-latest",
            "base_date": "20250201",
            "status": "executed_paper",
            "plan_type": "monitor",
            # Observation snapshots are not rebalance cycles and must not inflate plan_count.
            "record_kind": "observation",
            "strategy_name": "ComboA",
            "params_snapshot": {"universe_index": "csi1000", "top_n": 20, "initial_capital": 1_000_000},
        },
    ]
    summary = build_summary_from_lineage_plans(
        all_lineage,
        strategy_template_id="st1",
        params_hash="hash1",
        param_summary_fn=_fake_param_summary,
        record_kind_fn=_fake_record_kind,
        trade_plan_kind="trade_plan",
        has_live_signals=False,
    )
    assert summary["execution_venue"] == "paper"
    assert summary["latest_plan_id"] == "p-latest"
    assert summary["first_base_date"] == "20250101"
    assert summary["last_base_date"] == "20250201"
    assert summary["plan_count"] == 1
    assert summary["param_summary"] == "csi1000 · Top20"
    assert summary["initial_capital"] == 1_000_000


def test_build_summary_from_lineage_plans_prefers_stored_live_venue():
    all_lineage = [
        {
            "plan_id": "p1",
            "base_date": "20250101",
            "status": "executed_paper",
            "plan_type": "rebalance",
            "record_kind": "trade_plan",
            "params_snapshot": {},
        }
    ]
    summary = build_summary_from_lineage_plans(
        all_lineage,
        strategy_template_id="st1",
        params_hash="hash1",
        param_summary_fn=_fake_param_summary,
        record_kind_fn=_fake_record_kind,
        trade_plan_kind="trade_plan",
        has_live_signals=False,
        securities_account_id="",
        lineage_control={"execution_venue": "live", "securities_account_id": "acct-7"},
    )
    assert summary["execution_venue"] == "live"
    assert summary["securities_account_id"] == "acct-7"


def test_build_summary_from_lineage_plans_empty_returns_empty():
    assert build_summary_from_lineage_plans(
        [],
        strategy_template_id="st1",
        params_hash="hash1",
        param_summary_fn=_fake_param_summary,
        record_kind_fn=_fake_record_kind,
        trade_plan_kind="trade_plan",
        has_live_signals=False,
    ) == {}

