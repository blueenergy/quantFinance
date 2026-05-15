import pytest

from services import market_insight_engine as engine


class FakeCursor(list):
    def sort(self, key_or_list, direction=None):
        if isinstance(key_or_list, list):
            keys = key_or_list
        else:
            keys = [(key_or_list, direction)]
        rows = list(self)
        for key, direction in reversed(keys):
            rows.sort(key=lambda item: item.get(key) or "", reverse=direction == -1)
        return FakeCursor(rows)

    def limit(self, n):
        return FakeCursor(list(self)[:n])


class FakeCollection:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.upserts = []

    def find_one(self, query=None, projection=None, sort=None):
        rows = self.find(query or {}, projection)
        if sort:
            rows = rows.sort(sort)
        return rows[0] if rows else None

    def find(self, query=None, projection=None):
        query = query or {}
        rows = []
        for row in self.rows:
            if self._match(row, query):
                rows.append({k: v for k, v in row.items() if not projection or projection.get(k, 1)})
        return FakeCursor(rows)

    def update_one(self, query, update, upsert=False):
        self.upserts.append((query, update, upsert))

    def create_index(self, *args, **kwargs):
        return None

    def count_documents(self, query):
        return len(self.find(query))

    def _match(self, row, query):
        for key, value in query.items():
            current = row.get(key)
            if isinstance(value, dict):
                if "$lte" in value and not (current <= value["$lte"]):
                    return False
                if "$in" in value and current not in value["$in"]:
                    return False
            elif current != value:
                return False
        return True


@pytest.mark.unit
def test_generate_market_insight_events_detects_breadth_and_sector_diffusion(monkeypatch):
    realtime = {
        "market_spectrum_realtime_daily": FakeCollection(
            [
                {"snapshot_trade_date": "20260515", "snapshot_time": "202605151000", "yang_spectrum": 0.40},
                {"snapshot_trade_date": "20260515", "snapshot_time": "202605151020", "yang_spectrum": 0.51},
            ]
        ),
        "realtime_sector_strength": FakeCollection(
            [
                {
                    "trade_date": "20260515",
                    "snapshot_time": "202605151000",
                    "level": "L2",
                    "sector_code": "801011.SI",
                    "sector_name": "国有大型银行",
                    "positive_ratio": 0.35,
                    "relative_positive_ratio": -0.05,
                },
                {
                    "trade_date": "20260515",
                    "snapshot_time": "202605151020",
                    "level": "L2",
                    "sector_code": "801011.SI",
                    "sector_name": "国有大型银行",
                    "positive_ratio": 0.65,
                    "relative_positive_ratio": 0.14,
                    "avg_trade_size_yuan": 12000,
                    "stock_count": 20,
                    "market_positive_ratio": 0.51,
                },
            ]
        ),
    }
    finance = {"market_insight_events": FakeCollection()}
    monkeypatch.setattr(engine, "get_data_db", lambda: realtime)
    monkeypatch.setattr(engine, "get_db_collections", lambda: finance)

    result = engine.generate_market_insight_events("20260515")

    assert result["success"] is True
    event_types = {event["event_type"] for event in result["events"]}
    assert "MARKET_BREADTH_RECOVERY" in event_types
    assert "SECTOR_DIFFUSION" in event_types
    assert len(finance["market_insight_events"].upserts) == len(result["events"])
