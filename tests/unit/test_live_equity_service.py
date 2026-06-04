from services.live_equity import compute_live_plan_equity, snapshot_account_equity


class _Cursor(list):
    def sort(self, field, direction=None):
        if isinstance(field, list):
            field, direction = field[0]
        return _Cursor(sorted(self, key=lambda row: row.get(field) or "", reverse=direction == -1))


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

    def update_one(self, query, update, upsert=False):
        for doc in self.docs:
            if self._matches(doc, query):
                doc.update(update.get("$set", {}))
                return
        if upsert:
            row = dict(query)
            row.update(update.get("$set", {}))
            self.docs.append(row)

    def create_index(self, *args, **kwargs):
        return None

    def _matches(self, doc, query):
        for key, expected in query.items():
            value = doc.get(key)
            if isinstance(expected, dict):
                if "$in" in expected and value not in expected["$in"]:
                    return False
                if "$exists" in expected and (key in doc) is not bool(expected["$exists"]):
                    return False
                continue
            if value != expected:
                return False
        return True


def test_snapshot_account_equity_upserts_latest_account_point():
    collections = {
        "trader_accounts": _Collection(
            [
                {
                    "user_id": "user-1",
                    "securities_account_id": "acct-1",
                    "total_asset": 100000,
                    "cash": 20000,
                    "available_cash": 19000,
                    "market_value": 80000,
                    "synced_at": 1,
                }
            ]
        ),
        "trader_account_equity": _Collection(),
    }

    first = snapshot_account_equity(collections, date="20250604", stale_seconds=0)
    second = snapshot_account_equity(collections, date="20250604", stale_seconds=0)

    assert first["written"] == 1
    assert second["written"] == 1
    assert len(collections["trader_account_equity"].docs) == 1
    row = collections["trader_account_equity"].docs[0]
    assert row["date"] == "20250604"
    assert row["total_asset"] == 100000
    assert row["market_value"] == 80000
    assert row["is_stale"] is True


def test_compute_live_plan_equity_uses_plan_executions_and_fees():
    plan = {
        "user_id": "user-1",
        "plan_id": "plan-1",
        "strategy_template_id": "s1",
        "params_snapshot": {"initial_capital": 100000},
    }
    executions = [
        {
            "order_id": "buy-1",
            "symbol": "000001",
            "action": "buy",
            "filled_size": 100,
            "filled_price": 10,
            "total_fee": 5,
            "timestamp": 1,
        },
        {
            "order_id": "sell-1",
            "symbol": "000001",
            "action": "sell",
            "filled_size": 40,
            "filled_price": 12,
            "total_fee": 3,
            "timestamp": 2,
        },
    ]

    point = compute_live_plan_equity(plan, executions, {"000001": 11}, date="20250604")

    assert point["date"] == "20250604"
    assert point["initial_capital"] == 100000
    assert point["position_count"] == 1
    assert point["market_value"] == 660
    assert round(point["realized_pnl"], 2) == 75.0
    assert round(point["unrealized_pnl"], 2) == 57.0
    assert round(point["equity"], 2) == 100132.0
    assert round(point["cash"], 2) == 99472.0
