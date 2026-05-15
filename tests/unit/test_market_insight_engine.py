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
                if "$gte" in value and not (current >= value["$gte"]):
                    return False
                if "$in" in value and current not in value["$in"]:
                    return False
            elif current != value:
                return False
        return True


@pytest.mark.unit
def test_list_market_insight_events_filters_by_category_and_confidence(monkeypatch):
    finance = {
        "market_insight_events": FakeCollection(
            [
                {
                    "_id": "a",
                    "trade_date": "20260515",
                    "snapshot_time": "202605151030",
                    "event_type": "SECTOR_FALSE_STRENGTH",
                    "severity": "medium",
                    "confidence": 82.0,
                    "level": "L2",
                },
                {
                    "_id": "b",
                    "trade_date": "20260515",
                    "snapshot_time": "202605151031",
                    "event_type": "SECTOR_DIFFUSION",
                    "severity": "high",
                    "confidence": 92.0,
                    "level": "L2",
                },
                {
                    "_id": "c",
                    "trade_date": "20260515",
                    "snapshot_time": "202605151032",
                    "event_type": "SECTOR_FALSE_STRENGTH",
                    "severity": "medium",
                    "confidence": 60.0,
                    "level": "L2",
                },
            ]
        )
    }
    monkeypatch.setattr(engine, "get_db_collections", lambda: finance)

    rows = engine.list_market_insight_events(
        trade_date="2026-05-15",
        category="去妄存真类",
        severity="medium",
        min_confidence=80,
        level="L2",
    )

    assert [row["event_type"] for row in rows] == ["SECTOR_FALSE_STRENGTH"]
    assert rows[0]["confidence"] == 82.0


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


@pytest.mark.unit
def test_generate_market_insight_events_detects_early_l3_diffusion(monkeypatch):
    realtime = {
        "market_spectrum_realtime_daily": FakeCollection(
            [
                {"snapshot_trade_date": "20260515", "snapshot_time": "202605151020", "yang_spectrum": 0.48},
            ]
        ),
        "realtime_sector_strength": FakeCollection(
            [
                {
                    "trade_date": "20260515",
                    "snapshot_time": "202605151000",
                    "level": "L3",
                    "sector_code": "851011.SI",
                    "sector_name": "大型银行",
                    "positive_ratio": 0.38,
                    "relative_positive_ratio": -0.02,
                },
                {
                    "trade_date": "20260515",
                    "snapshot_time": "202605151020",
                    "level": "L3",
                    "sector_code": "851011.SI",
                    "sector_name": "大型银行",
                    "positive_ratio": 0.68,
                    "relative_positive_ratio": 0.20,
                    "avg_trade_size_yuan": 18000,
                    "stock_count": 8,
                    "market_positive_ratio": 0.48,
                },
                {
                    "trade_date": "20260515",
                    "snapshot_time": "202605151000",
                    "level": "L2",
                    "sector_code": "801011.SI",
                    "sector_name": "国有大型银行",
                    "positive_ratio": 0.43,
                    "relative_positive_ratio": -0.04,
                },
                {
                    "trade_date": "20260515",
                    "snapshot_time": "202605151020",
                    "level": "L2",
                    "sector_code": "801011.SI",
                    "sector_name": "国有大型银行",
                    "positive_ratio": 0.49,
                    "relative_positive_ratio": 0.01,
                },
                {
                    "trade_date": "20260515",
                    "snapshot_time": "202605151020",
                    "level": "L1",
                    "sector_code": "801010.SI",
                    "sector_name": "银行",
                    "positive_ratio": 0.50,
                    "relative_positive_ratio": 0.02,
                },
            ]
        ),
        "sw_index_member": FakeCollection(
            [
                {
                    "src": "SW2021",
                    "is_new": "Y",
                    "l1_code": "801010.SI",
                    "l1_name": "银行",
                    "l2_code": "801011.SI",
                    "l2_name": "国有大型银行",
                    "l3_code": "851011.SI",
                    "l3_name": "大型银行",
                }
            ]
        ),
    }
    finance = {"market_insight_events": FakeCollection()}
    monkeypatch.setattr(engine, "get_data_db", lambda: realtime)
    monkeypatch.setattr(engine, "get_db_collections", lambda: finance)

    result = engine.generate_market_insight_events("20260515")

    early_events = [event for event in result["events"] if event["event_type"] == "EARLY_SECTOR_DIFFUSION"]
    assert len(early_events) == 1
    assert early_events[0]["subject_name"] == "大型银行"
    assert early_events[0]["evidence"]["parent_l2_code"] == "801011.SI"
    assert early_events[0]["evidence"]["parent_confirmed"] is False
    assert len(finance["market_insight_events"].upserts) == len(result["events"])


@pytest.mark.unit
def test_generate_market_insight_events_detects_sector_false_strength(monkeypatch):
    realtime = {
        "market_spectrum_realtime_daily": FakeCollection(
            [
                {"snapshot_trade_date": "20260515", "snapshot_time": "202605151020", "yang_spectrum": 0.32},
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
                    "positive_ratio": 0.36,
                    "relative_positive_ratio": 0.05,
                },
                {
                    "trade_date": "20260515",
                    "snapshot_time": "202605151020",
                    "level": "L2",
                    "sector_code": "801011.SI",
                    "sector_name": "国有大型银行",
                    "positive_ratio": 0.44,
                    "relative_positive_ratio": 0.12,
                    "avg_pct_chg": 1.1,
                    "avg_trade_size_yuan": 18000,
                    "stock_count": 20,
                    "market_positive_ratio": 0.32,
                },
            ]
        ),
        "sw_index_member": FakeCollection([]),
    }
    finance = {"market_insight_events": FakeCollection()}
    monkeypatch.setattr(engine, "get_data_db", lambda: realtime)
    monkeypatch.setattr(engine, "get_db_collections", lambda: finance)

    result = engine.generate_market_insight_events("20260515")

    false_strength_events = [event for event in result["events"] if event["event_type"] == "SECTOR_FALSE_STRENGTH"]
    assert len(false_strength_events) == 1
    assert false_strength_events[0]["subject_name"] == "国有大型银行"
    assert false_strength_events[0]["evidence"]["positive_ratio_now"] == pytest.approx(0.44)
    assert false_strength_events[0]["evidence"]["avg_pct_chg"] == pytest.approx(1.1)


@pytest.mark.unit
def test_generate_market_insight_events_detects_counter_trend_sector_strength(monkeypatch):
    realtime = {
        "market_spectrum_realtime_daily": FakeCollection(
            [
                {"snapshot_trade_date": "20260515", "snapshot_time": "202605151000", "yang_spectrum": 0.46},
                {"snapshot_trade_date": "20260515", "snapshot_time": "202605151020", "yang_spectrum": 0.38},
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
                    "positive_ratio": 0.42,
                    "relative_positive_ratio": -0.04,
                },
                {
                    "trade_date": "20260515",
                    "snapshot_time": "202605151020",
                    "level": "L2",
                    "sector_code": "801011.SI",
                    "sector_name": "国有大型银行",
                    "positive_ratio": 0.62,
                    "relative_positive_ratio": 0.24,
                    "avg_trade_size_yuan": 18000,
                    "stock_count": 20,
                    "market_positive_ratio": 0.38,
                },
            ]
        ),
        "sw_index_member": FakeCollection([]),
    }
    finance = {"market_insight_events": FakeCollection()}
    monkeypatch.setattr(engine, "get_data_db", lambda: realtime)
    monkeypatch.setattr(engine, "get_db_collections", lambda: finance)

    result = engine.generate_market_insight_events("20260515")

    counter_events = [event for event in result["events"] if event["event_type"] == "COUNTER_TREND_SECTOR_STRENGTH"]
    assert len(counter_events) == 1
    assert counter_events[0]["subject_name"] == "国有大型银行"
    assert counter_events[0]["evidence"]["market_weak"] is True
    assert counter_events[0]["evidence"]["market_yang_spectrum"] == pytest.approx(0.38)


@pytest.mark.unit
def test_get_sector_strength_series_returns_chronological_rows(monkeypatch):
    realtime = {
        "realtime_sector_strength": FakeCollection(
            [
                {
                    "trade_date": "20260515",
                    "snapshot_time": "202605151020",
                    "level": "L2",
                    "sector_code": "801011.SI",
                    "sector_name": "国有大型银行",
                    "positive_ratio": 0.65,
                },
                {
                    "trade_date": "20260515",
                    "snapshot_time": "202605151000",
                    "level": "L2",
                    "sector_code": "801011.SI",
                    "sector_name": "国有大型银行",
                    "positive_ratio": 0.35,
                },
                {
                    "trade_date": "20260515",
                    "snapshot_time": "202605151010",
                    "level": "L2",
                    "sector_code": "801011.SI",
                    "sector_name": "国有大型银行",
                    "positive_ratio": 0.50,
                },
            ]
        )
    }
    monkeypatch.setattr(engine, "get_data_db", lambda: realtime)

    result = engine.get_sector_strength_series(
        level="L2",
        sector_code="801011.SI",
        trade_date="20260515",
        limit=10,
    )

    assert result["success"] is True
    assert [row["snapshot_time"] for row in result["data"]] == [
        "202605151000",
        "202605151010",
        "202605151020",
    ]
    assert result["data"][0]["delta_from_previous"] is None
    assert result["data"][1]["delta_from_previous"] == pytest.approx(0.15)
    assert result["data"][2]["delta_from_previous"] == pytest.approx(0.15)


@pytest.mark.unit
def test_get_sector_strength_series_can_simulate_sparse_rows(monkeypatch):
    realtime = {
        "realtime_sector_strength": FakeCollection(
            [
                {
                    "trade_date": "20260515",
                    "snapshot_time": "202605151020",
                    "level": "L2",
                    "sector_code": "801011.SI",
                    "sector_name": "国有大型银行",
                    "positive_ratio": 0.65,
                    "relative_positive_ratio": 0.12,
                }
            ]
        )
    }
    monkeypatch.setattr(engine, "get_data_db", lambda: realtime)

    result = engine.get_sector_strength_series(
        level="L2",
        sector_code="801011.SI",
        trade_date="20260515",
        limit=8,
        simulate_if_sparse=True,
    )

    assert result["success"] is True
    assert result["is_simulated"] is True
    assert result["count"] == 8
    assert len(result["data"]) == 8
    assert all(row["simulated"] for row in result["data"])
    assert result["data"][-1]["positive_ratio"] == pytest.approx(0.65)
    assert result["data"][0]["snapshot_time"] < result["data"][-1]["snapshot_time"]
