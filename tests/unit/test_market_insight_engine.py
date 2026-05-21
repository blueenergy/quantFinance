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
        self.deleted = []

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

    def delete_many(self, query):
        self.deleted.append(query)

    def create_index(self, *args, **kwargs):
        return None

    def count_documents(self, query):
        return len(self.find(query))

    def _match(self, row, query):
        for key, value in query.items():
            current = row.get(key)
            if isinstance(value, dict):
                if "$gt" in value and not (current > value["$gt"]):
                    return False
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
def test_get_market_insight_focus_prioritizes_high_value_events(monkeypatch):
    finance = {
        "market_insight_events": FakeCollection(
            [
                {
                    "_id": "a",
                    "trade_date": "20260515",
                    "snapshot_time": "202605151030",
                    "event_type": "SECTOR_DIFFUSION",
                    "severity": "medium",
                    "confidence": 88.0,
                    "subject_type": "sector",
                    "subject_code": "801011.SI",
                    "subject_name": "银行",
                    "title": "银行扩散",
                    "summary": "银行上涨家数扩散。",
                    "watch_conditions": ["观察银行是否维持扩散"],
                },
                {
                    "_id": "b",
                    "trade_date": "20260515",
                    "snapshot_time": "202605151035",
                    "event_type": "COUNTER_TREND_SECTOR_STRENGTH",
                    "severity": "high",
                    "confidence": 84.0,
                    "subject_type": "sector",
                    "subject_code": "801012.SI",
                    "subject_name": "机器人",
                    "title": "机器人逆势扩散",
                    "summary": "机器人弱市逆势扩散。",
                    "watch_conditions": ["观察机器人能否继续扩散"],
                },
                {
                    "_id": "c",
                    "trade_date": "20260515",
                    "snapshot_time": "202605151036",
                    "event_type": "MARKET_BREADTH_RECOVERY",
                    "severity": "medium",
                    "confidence": 90.0,
                    "subject_type": "market",
                    "subject_code": "all",
                    "subject_name": "全市场",
                    "title": "广度修复",
                    "summary": "全市场阳谱修复。",
                },
            ]
        )
    }
    monkeypatch.setattr(engine, "get_db_collections", lambda: finance)

    result = engine.get_market_insight_focus("2026-05-15", limit=2)

    assert result["success"] is True
    assert result["count"] == 2
    assert result["data"][0]["primary_event"]["event_type"] == "COUNTER_TREND_SECTOR_STRENGTH"
    assert result["data"][0]["attention_score"] > result["data"][1]["attention_score"]
    assert "为什么先看" not in result["data"][0]["why_focus"]
    assert result["data"][0]["next_action"] == "观察机器人能否继续扩散"


@pytest.mark.unit
def test_get_market_insight_focus_boosts_user_relevant_sectors(monkeypatch):
    finance = {
        "market_insight_events": FakeCollection(
            [
                {
                    "_id": "a",
                    "trade_date": "20260515",
                    "snapshot_time": "202605151030",
                    "event_type": "SECTOR_DIFFUSION",
                    "severity": "medium",
                    "confidence": 76.0,
                    "subject_type": "sector",
                    "subject_code": "801011.SI",
                    "subject_name": "银行",
                    "title": "银行扩散",
                    "summary": "银行上涨家数扩散。",
                },
                {
                    "_id": "b",
                    "trade_date": "20260515",
                    "snapshot_time": "202605151030",
                    "event_type": "SECTOR_DIFFUSION",
                    "severity": "medium",
                    "confidence": 78.0,
                    "subject_type": "sector",
                    "subject_code": "801012.SI",
                    "subject_name": "地产",
                    "title": "地产扩散",
                    "summary": "地产上涨家数扩散。",
                },
            ]
        ),
        "trader_positions": FakeCollection(
            [
                {
                    "user_id": "u1",
                    "symbol": "600000.SH",
                    "quantity": 100,
                    "sw_l2_code": "801011.SI",
                }
            ]
        ),
        "intraday_t0_mock_positions": FakeCollection([]),
        "user_watchlists": FakeCollection([]),
        "market_insight_focus_feedback": FakeCollection([]),
    }
    monkeypatch.setattr(engine, "get_db_collections", lambda: finance)

    result = engine.get_market_insight_focus("2026-05-15", limit=2, user_id="u1")

    assert result["data"][0]["subject_name"] == "银行"
    assert result["data"][0]["relevance"]["matched"] is True
    assert "命中真实持仓" in result["data"][0]["relevance"]["labels"]
    assert result["data"][0]["score_breakdown"]["user_relevance_weight"] == 18.0


@pytest.mark.unit
def test_get_market_insight_focus_includes_existing_feedback(monkeypatch):
    focus_key = "20260515|202605151030|SECTOR_DIFFUSION|sector|801011.SI"
    finance = {
        "market_insight_events": FakeCollection(
            [
                {
                    "_id": "a",
                    "trade_date": "20260515",
                    "snapshot_time": "202605151030",
                    "event_type": "SECTOR_DIFFUSION",
                    "severity": "medium",
                    "confidence": 76.0,
                    "subject_type": "sector",
                    "subject_code": "801011.SI",
                    "subject_name": "银行",
                    "title": "银行扩散",
                    "summary": "银行上涨家数扩散。",
                },
            ]
        ),
        "trader_positions": FakeCollection([]),
        "intraday_t0_mock_positions": FakeCollection([]),
        "user_watchlists": FakeCollection([]),
        "market_insight_focus_feedback": FakeCollection(
            [
                {
                    "user_id": "u1",
                    "trade_date": "20260515",
                    "focus_key": focus_key,
                    "action": "ignore",
                    "note": "暂不关注",
                    "updated_at": 123.0,
                }
            ]
        ),
    }
    monkeypatch.setattr(engine, "get_db_collections", lambda: finance)

    result = engine.get_market_insight_focus("2026-05-15", limit=1, user_id="u1")

    assert result["data"][0]["focus_key"] == focus_key
    assert result["data"][0]["feedback"]["action"] == "ignore"
    assert result["data"][0]["feedback"]["label"] == "忽略"
    assert result["data"][0]["score_breakdown"]["feedback_weight"] == -12.0


@pytest.mark.unit
def test_save_market_insight_focus_feedback_upserts(monkeypatch):
    feedback = FakeCollection([])
    finance = {"market_insight_focus_feedback": feedback}
    monkeypatch.setattr(engine, "get_db_collections", lambda: finance)

    result = engine.save_market_insight_focus_feedback(
        user_id="u1",
        focus_key="20260515|202605151030|SECTOR_DIFFUSION|sector|801011.SI",
        action="valuable",
        note="值得复盘",
    )

    assert result["success"] is True
    assert result["data"]["action"] == "valuable"
    assert result["data"]["label"] == "有价值"
    query, update, upsert = feedback.upserts[0]
    assert query["user_id"] == "u1"
    assert update["$set"]["trade_date"] == "20260515"
    assert update["$set"]["note"] == "值得复盘"
    assert upsert is True


@pytest.mark.unit
def test_update_user_preference_from_feedback_updates_dimensions(monkeypatch):
    preferences = FakeCollection([])
    finance = {"market_insight_user_preferences": preferences}
    monkeypatch.setattr(engine, "get_db_collections", lambda: finance)

    result = engine.update_user_preference_from_feedback(
        user_id="u1",
        focus_key="20260515|202605151030|SECTOR_DIFFUSION|sector|801011.SI",
        action="valuable",
    )

    assert len(result) == 3
    written = {doc["dimension"]: doc for doc in result}
    assert written["event_type"]["key"] == "SECTOR_DIFFUSION"
    assert written["event_type"]["valuable_count"] == 1
    assert written["event_type"]["score"] == 2.0
    assert written["category"]["key"] == "板块扩散类"
    assert written["subject_type"]["key"] == "sector"
    assert preferences.upserts[0][0] == {"user_id": "u1", "dimension": "event_type", "key": "SECTOR_DIFFUSION"}


@pytest.mark.unit
def test_update_user_preference_from_feedback_replaces_previous_action(monkeypatch):
    preferences = FakeCollection(
        [
            {
                "user_id": "u1",
                "dimension": "event_type",
                "key": "SECTOR_DIFFUSION",
                "valuable_count": 0,
                "ignore_count": 1,
                "false_positive_count": 0,
                "confirmed_count": 0,
                "faded_count": 0,
                "score": -1.0,
            }
        ]
    )
    finance = {"market_insight_user_preferences": preferences}
    monkeypatch.setattr(engine, "get_db_collections", lambda: finance)

    result = engine.update_user_preference_from_feedback(
        user_id="u1",
        focus_key="20260515|202605151030|SECTOR_DIFFUSION|sector|801011.SI",
        action="valuable",
        previous_action="ignore",
    )

    event_type_pref = next(doc for doc in result if doc["dimension"] == "event_type")
    assert event_type_pref["valuable_count"] == 1
    assert event_type_pref["ignore_count"] == 0
    assert event_type_pref["score"] == 2.0


@pytest.mark.unit
def test_rebuild_market_insight_user_preferences_from_history(monkeypatch):
    preferences = FakeCollection(
        [
            {
                "user_id": "u1",
                "dimension": "event_type",
                "key": "OLD",
                "valuable_count": 99,
            }
        ]
    )
    finance = {
        "market_insight_focus_feedback": FakeCollection(
            [
                {
                    "user_id": "u1",
                    "focus_key": "20260515|202605151030|SECTOR_DIFFUSION|sector|801011.SI",
                    "action": "valuable",
                },
                {
                    "user_id": "u1",
                    "focus_key": "20260515|202605151035|SECTOR_FALSE_STRENGTH|sector|801012.SI",
                    "action": "false_positive",
                },
                {
                    "user_id": "u2",
                    "focus_key": "20260515|202605151040|SECTOR_DIFFUSION|sector|801013.SI",
                    "action": "ignore",
                },
            ]
        ),
        "market_insight_focus_reviews": FakeCollection(
            [
                {
                    "focus_key": "20260515|202605151030|SECTOR_DIFFUSION|sector|801011.SI",
                    "status": "confirmed",
                },
                {
                    "focus_key": "20260515|202605151035|SECTOR_FALSE_STRENGTH|sector|801012.SI",
                    "status": "faded",
                },
            ]
        ),
        "market_insight_user_preferences": preferences,
    }
    monkeypatch.setattr(engine, "get_db_collections", lambda: finance)

    result = engine.rebuild_market_insight_user_preferences()

    assert result["success"] is True
    assert result["dry_run"] is False
    assert result["processed_feedback_count"] == 3
    assert result["count"] == 8
    assert preferences.deleted == [{}]
    event_type_updates = {
        (update["$set"]["user_id"], update["$set"]["key"]): update["$set"]
        for _, update, _ in preferences.upserts
        if update["$set"]["dimension"] == "event_type"
    }
    subject_updates = {
        update["$set"]["user_id"]: update["$set"]
        for _, update, _ in preferences.upserts
        if update["$set"]["dimension"] == "subject_type" and update["$set"]["key"] == "sector"
    }
    assert event_type_updates[("u1", "SECTOR_DIFFUSION")]["valuable_count"] == 1
    assert event_type_updates[("u1", "SECTOR_DIFFUSION")]["confirmed_count"] == 1
    assert event_type_updates[("u1", "SECTOR_DIFFUSION")]["score"] == 3.0
    assert event_type_updates[("u1", "SECTOR_FALSE_STRENGTH")]["false_positive_count"] == 1
    assert event_type_updates[("u1", "SECTOR_FALSE_STRENGTH")]["faded_count"] == 1
    assert event_type_updates[("u1", "SECTOR_FALSE_STRENGTH")]["score"] == -4.0
    assert event_type_updates[("u2", "SECTOR_DIFFUSION")]["ignore_count"] == 1
    assert subject_updates["u1"]["valuable_count"] == 1
    assert subject_updates["u1"]["false_positive_count"] == 1
    assert subject_updates["u1"]["confirmed_count"] == 1
    assert subject_updates["u1"]["faded_count"] == 1


@pytest.mark.unit
def test_rebuild_market_insight_user_preferences_filters_user_and_dry_run(monkeypatch):
    preferences = FakeCollection([])
    finance = {
        "market_insight_focus_feedback": FakeCollection(
            [
                {
                    "user_id": "u1",
                    "focus_key": "20260515|202605151030|SECTOR_DIFFUSION|sector|801011.SI",
                    "action": "valuable",
                },
                {
                    "user_id": "u2",
                    "focus_key": "20260515|202605151040|SECTOR_FALSE_STRENGTH|sector|801012.SI",
                    "action": "ignore",
                },
            ]
        ),
        "market_insight_focus_reviews": FakeCollection([]),
        "market_insight_user_preferences": preferences,
    }
    monkeypatch.setattr(engine, "get_db_collections", lambda: finance)

    result = engine.rebuild_market_insight_user_preferences(user_id="u1", dry_run=True)

    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["user_id"] == "u1"
    assert result["processed_feedback_count"] == 1
    assert result["count"] == 3
    assert preferences.deleted == []
    assert preferences.upserts == []


@pytest.mark.unit
def test_get_market_insight_focus_applies_user_preference_weight(monkeypatch):
    finance = {
        "market_insight_events": FakeCollection(
            [
                {
                    "_id": "a",
                    "trade_date": "20260515",
                    "snapshot_time": "202605151030",
                    "event_type": "SECTOR_DIFFUSION",
                    "severity": "medium",
                    "confidence": 77.0,
                    "subject_type": "sector",
                    "subject_code": "801011.SI",
                    "subject_name": "银行",
                    "title": "银行扩散",
                    "summary": "银行上涨家数扩散。",
                },
                {
                    "_id": "b",
                    "trade_date": "20260515",
                    "snapshot_time": "202605151030",
                    "event_type": "SECTOR_FALSE_STRENGTH",
                    "severity": "medium",
                    "confidence": 78.0,
                    "subject_type": "sector",
                    "subject_code": "801012.SI",
                    "subject_name": "地产",
                    "title": "地产假强",
                    "summary": "地产表面强势。",
                },
            ]
        ),
        "trader_positions": FakeCollection([]),
        "intraday_t0_mock_positions": FakeCollection([]),
        "user_watchlists": FakeCollection([]),
        "market_insight_focus_feedback": FakeCollection([]),
        "market_insight_user_preferences": FakeCollection(
            [
                {
                    "user_id": "u1",
                    "dimension": "event_type",
                    "key": "SECTOR_DIFFUSION",
                    "score": 20.0,
                },
                {
                    "user_id": "u1",
                    "dimension": "category",
                    "key": "去妄存真类",
                    "score": -20.0,
                },
            ]
        ),
    }
    monkeypatch.setattr(engine, "get_db_collections", lambda: finance)

    result = engine.get_market_insight_focus("2026-05-15", limit=2, user_id="u1")

    assert result["data"][0]["primary_event"]["event_type"] == "SECTOR_DIFFUSION"
    assert result["data"][0]["score_breakdown"]["preference_weight"] == 5.0
    assert result["data"][1]["score_breakdown"]["preference_weight"] == -5.0
    assert "过往反馈" in result["data"][0]["why_focus"]


@pytest.mark.unit
def test_rebuild_market_insight_global_quality_aggregates_reviews(monkeypatch):
    quality = FakeCollection([])
    finance = {
        "market_insight_focus_reviews": FakeCollection(
            [
                {
                    "focus_key": "a",
                    "event_type": "SECTOR_DIFFUSION",
                    "subject_type": "sector",
                    "status": "confirmed",
                    "checked_at": 10.0,
                },
                {
                    "focus_key": "b",
                    "event_type": "SECTOR_DIFFUSION",
                    "subject_type": "sector",
                    "status": "confirmed",
                    "checked_at": 20.0,
                },
                {
                    "focus_key": "c",
                    "event_type": "SECTOR_DIFFUSION",
                    "subject_type": "sector",
                    "status": "faded",
                    "checked_at": 30.0,
                },
                {
                    "focus_key": "d",
                    "event_type": "SECTOR_FALSE_STRENGTH",
                    "subject_type": "sector",
                    "status": "insufficient_data",
                    "checked_at": 40.0,
                },
            ]
        ),
        "market_insight_global_quality": quality,
    }
    monkeypatch.setattr(engine, "get_db_collections", lambda: finance)

    result = engine.rebuild_market_insight_global_quality()

    assert result["success"] is True
    assert result["count"] == 5
    event_type_updates = [
        update["$set"]
        for query, update, _ in quality.upserts
        if query == {"dimension": "event_type", "key": "SECTOR_DIFFUSION"}
    ]
    assert event_type_updates[0]["confirmed_count"] == 2
    assert event_type_updates[0]["faded_count"] == 1
    assert event_type_updates[0]["decisive_count"] == 3
    assert event_type_updates[0]["confirmation_rate"] == pytest.approx(0.6667)
    assert event_type_updates[0]["weight"] == pytest.approx(0.2)


@pytest.mark.unit
def test_get_market_insight_focus_applies_global_quality_weight(monkeypatch):
    finance = {
        "market_insight_events": FakeCollection(
            [
                {
                    "_id": "a",
                    "trade_date": "20260515",
                    "snapshot_time": "202605151030",
                    "event_type": "SECTOR_DIFFUSION",
                    "severity": "medium",
                    "confidence": 77.0,
                    "subject_type": "sector",
                    "subject_code": "801011.SI",
                    "subject_name": "银行",
                    "title": "银行扩散",
                    "summary": "银行上涨家数扩散。",
                },
                {
                    "_id": "b",
                    "trade_date": "20260515",
                    "snapshot_time": "202605151030",
                    "event_type": "SECTOR_FALSE_STRENGTH",
                    "severity": "medium",
                    "confidence": 78.0,
                    "subject_type": "sector",
                    "subject_code": "801012.SI",
                    "subject_name": "地产",
                    "title": "地产假强",
                    "summary": "地产表面强势。",
                },
            ]
        ),
        "trader_positions": FakeCollection([]),
        "intraday_t0_mock_positions": FakeCollection([]),
        "user_watchlists": FakeCollection([]),
        "market_insight_focus_feedback": FakeCollection([]),
        "market_insight_user_preferences": FakeCollection([]),
        "market_insight_global_quality": FakeCollection(
            [
                {
                    "dimension": "event_type",
                    "key": "SECTOR_DIFFUSION",
                    "weight": 4.0,
                    "decisive_count": 30,
                },
                {
                    "dimension": "category",
                    "key": "去妄存真类",
                    "weight": -4.0,
                    "decisive_count": 30,
                },
            ]
        ),
    }
    monkeypatch.setattr(engine, "get_db_collections", lambda: finance)

    result = engine.get_market_insight_focus("2026-05-15", limit=2, user_id="u1")

    assert result["data"][0]["primary_event"]["event_type"] == "SECTOR_DIFFUSION"
    assert result["data"][0]["score_breakdown"]["global_quality_weight"] == 4.0
    assert result["data"][1]["score_breakdown"]["global_quality_weight"] == -2.4
    assert "历史盘后验证" in result["data"][0]["why_focus"]


@pytest.mark.unit
def test_review_market_insight_focus_confirms_sector_diffusion(monkeypatch):
    reviews = FakeCollection([])
    finance = {
        "market_insight_events": FakeCollection(
            [
                {
                    "_id": "a",
                    "trade_date": "20260515",
                    "snapshot_time": "202605151030",
                    "event_type": "SECTOR_DIFFUSION",
                    "severity": "medium",
                    "confidence": 80.0,
                    "subject_type": "sector",
                    "subject_code": "801011.SI",
                    "subject_name": "银行",
                    "level": "L2",
                    "evidence": {"positive_ratio_now": 0.55},
                }
            ]
        ),
        "market_insight_focus_reviews": reviews,
    }
    realtime = {
        "realtime_sector_strength": FakeCollection(
            [
                {
                    "trade_date": "20260515",
                    "snapshot_time": "202605151045",
                    "level": "L2",
                    "sector_code": "801011.SI",
                    "positive_ratio": 0.64,
                },
                {
                    "trade_date": "20260515",
                    "snapshot_time": "202605151100",
                    "level": "L2",
                    "sector_code": "801011.SI",
                    "positive_ratio": 0.62,
                },
            ]
        )
    }
    monkeypatch.setattr(engine, "get_db_collections", lambda: finance)
    monkeypatch.setattr(engine, "get_data_db", lambda: realtime)

    result = engine.review_market_insight_focus("2026-05-15", horizon_minutes=60)

    assert result["success"] is True
    assert result["written"] == 1
    assert result["data"][0]["status"] == "confirmed"
    assert result["data"][0]["label"] == "已验证"
    assert result["data"][0]["metrics"]["delta_peak"] == pytest.approx(0.09)
    assert reviews.upserts[0][1]["$set"]["status"] == "confirmed"


@pytest.mark.unit
def test_review_sector_diffusion_confirms_when_strength_holds(monkeypatch):
    finance = {
        "market_insight_events": FakeCollection(
            [
                {
                    "_id": "a",
                    "trade_date": "20260515",
                    "snapshot_time": "202605151030",
                    "event_type": "SECTOR_DIFFUSION",
                    "severity": "medium",
                    "confidence": 80.0,
                    "subject_type": "sector",
                    "subject_code": "801011.SI",
                    "subject_name": "银行",
                    "level": "L2",
                    "evidence": {"positive_ratio_now": 0.62},
                }
            ]
        ),
        "market_insight_focus_reviews": FakeCollection([]),
    }
    realtime = {
        "realtime_sector_strength": FakeCollection(
            [
                {
                    "trade_date": "20260515",
                    "snapshot_time": "202605151045",
                    "level": "L2",
                    "sector_code": "801011.SI",
                    "positive_ratio": 0.60,
                },
                {
                    "trade_date": "20260515",
                    "snapshot_time": "202605151100",
                    "level": "L2",
                    "sector_code": "801011.SI",
                    "positive_ratio": 0.59,
                },
            ]
        )
    }
    monkeypatch.setattr(engine, "get_db_collections", lambda: finance)
    monkeypatch.setattr(engine, "get_data_db", lambda: realtime)

    result = engine.review_market_insight_focus("2026-05-15", horizon_minutes=60)

    assert result["data"][0]["status"] == "confirmed"
    assert "维持" in result["data"][0]["summary"]


@pytest.mark.unit
def test_review_early_diffusion_confirms_when_parent_follows(monkeypatch):
    finance = {
        "market_insight_events": FakeCollection(
            [
                {
                    "_id": "a",
                    "trade_date": "20260515",
                    "snapshot_time": "202605151030",
                    "event_type": "EARLY_SECTOR_DIFFUSION",
                    "severity": "medium",
                    "confidence": 82.0,
                    "subject_type": "sector",
                    "subject_code": "851011.SI",
                    "subject_name": "大型银行",
                    "level": "L3",
                    "evidence": {
                        "positive_ratio_now": 0.62,
                        "parent_l2_code": "801011.SI",
                        "parent_l1_code": "801010.SI",
                    },
                }
            ]
        ),
        "market_insight_focus_reviews": FakeCollection([]),
    }
    realtime = {
        "realtime_sector_strength": FakeCollection(
            [
                {
                    "trade_date": "20260515",
                    "snapshot_time": "202605151045",
                    "level": "L3",
                    "sector_code": "851011.SI",
                    "positive_ratio": 0.60,
                    "relative_positive_ratio": 0.08,
                },
                {
                    "trade_date": "20260515",
                    "snapshot_time": "202605151100",
                    "level": "L2",
                    "sector_code": "801011.SI",
                    "positive_ratio": 0.56,
                    "relative_positive_ratio": 0.07,
                },
            ]
        )
    }
    monkeypatch.setattr(engine, "get_db_collections", lambda: finance)
    monkeypatch.setattr(engine, "get_data_db", lambda: realtime)

    result = engine.review_market_insight_focus("2026-05-15", horizon_minutes=60)

    assert result["data"][0]["status"] == "confirmed"
    assert result["data"][0]["metrics"]["parent_follow_through_confirmed"] is True
    assert "上级行业" in result["data"][0]["summary"]


@pytest.mark.unit
def test_review_counter_trend_confirms_when_relative_strength_persists(monkeypatch):
    finance = {
        "market_insight_events": FakeCollection(
            [
                {
                    "_id": "a",
                    "trade_date": "20260515",
                    "snapshot_time": "202605151030",
                    "event_type": "COUNTER_TREND_SECTOR_STRENGTH",
                    "severity": "high",
                    "confidence": 86.0,
                    "subject_type": "sector",
                    "subject_code": "801011.SI",
                    "subject_name": "银行",
                    "level": "L2",
                    "evidence": {"positive_ratio_now": 0.58},
                }
            ]
        ),
        "market_insight_focus_reviews": FakeCollection([]),
    }
    realtime = {
        "realtime_sector_strength": FakeCollection(
            [
                {
                    "trade_date": "20260515",
                    "snapshot_time": "202605151045",
                    "level": "L2",
                    "sector_code": "801011.SI",
                    "positive_ratio": 0.56,
                    "relative_positive_ratio": 0.10,
                },
                {
                    "trade_date": "20260515",
                    "snapshot_time": "202605151100",
                    "level": "L2",
                    "sector_code": "801011.SI",
                    "positive_ratio": 0.55,
                    "relative_positive_ratio": 0.09,
                },
            ]
        )
    }
    monkeypatch.setattr(engine, "get_db_collections", lambda: finance)
    monkeypatch.setattr(engine, "get_data_db", lambda: realtime)

    result = engine.review_market_insight_focus("2026-05-15", horizon_minutes=60)

    assert result["data"][0]["status"] == "confirmed"
    assert result["data"][0]["metrics"]["relative_positive_ratio_min"] == pytest.approx(0.09)
    assert "相对强度" in result["data"][0]["summary"]


@pytest.mark.unit
def test_review_market_insight_focus_updates_preferences_for_feedback_users(monkeypatch):
    focus_key = "20260515|202605151030|SECTOR_DIFFUSION|sector|801011.SI"
    reviews = FakeCollection([])
    feedback = FakeCollection(
        [
            {
                "user_id": "u1",
                "trade_date": "20260515",
                "focus_key": focus_key,
                "action": "valuable",
            }
        ]
    )
    preferences = FakeCollection([])
    finance = {
        "market_insight_events": FakeCollection(
            [
                {
                    "_id": "a",
                    "trade_date": "20260515",
                    "snapshot_time": "202605151030",
                    "event_type": "SECTOR_DIFFUSION",
                    "severity": "medium",
                    "confidence": 80.0,
                    "subject_type": "sector",
                    "subject_code": "801011.SI",
                    "subject_name": "银行",
                    "level": "L2",
                    "evidence": {"positive_ratio_now": 0.55},
                }
            ]
        ),
        "market_insight_focus_reviews": reviews,
        "market_insight_focus_feedback": feedback,
        "market_insight_user_preferences": preferences,
    }
    realtime = {
        "realtime_sector_strength": FakeCollection(
            [
                {
                    "trade_date": "20260515",
                    "snapshot_time": "202605151045",
                    "level": "L2",
                    "sector_code": "801011.SI",
                    "positive_ratio": 0.64,
                }
            ]
        )
    }
    monkeypatch.setattr(engine, "get_db_collections", lambda: finance)
    monkeypatch.setattr(engine, "get_data_db", lambda: realtime)

    result = engine.review_market_insight_focus("2026-05-15", horizon_minutes=60)

    assert result["data"][0]["status"] == "confirmed"
    first_pref_update = preferences.upserts[0][1]["$set"]
    assert first_pref_update["user_id"] == "u1"
    assert first_pref_update["confirmed_count"] == 1
    assert first_pref_update["score"] == 1.0
    assert feedback.upserts[0][1]["$set"]["preference_review_status"] == "confirmed"


@pytest.mark.unit
def test_get_market_insight_focus_includes_existing_review(monkeypatch):
    focus_key = "20260515|202605151030|SECTOR_DIFFUSION|sector|801011.SI"
    finance = {
        "market_insight_events": FakeCollection(
            [
                {
                    "_id": "a",
                    "trade_date": "20260515",
                    "snapshot_time": "202605151030",
                    "event_type": "SECTOR_DIFFUSION",
                    "severity": "medium",
                    "confidence": 76.0,
                    "subject_type": "sector",
                    "subject_code": "801011.SI",
                    "subject_name": "银行",
                    "title": "银行扩散",
                    "summary": "银行上涨家数扩散。",
                },
            ]
        ),
        "trader_positions": FakeCollection([]),
        "intraday_t0_mock_positions": FakeCollection([]),
        "user_watchlists": FakeCollection([]),
        "market_insight_focus_feedback": FakeCollection([]),
        "market_insight_focus_reviews": FakeCollection(
            [
                {
                    "trade_date": "20260515",
                    "focus_key": focus_key,
                    "status": "confirmed",
                    "summary": "已延续",
                    "checked_at": 123.0,
                }
            ]
        ),
    }
    monkeypatch.setattr(engine, "get_db_collections", lambda: finance)

    result = engine.get_market_insight_focus("2026-05-15", limit=1, user_id="u1")

    assert result["data"][0]["review"]["status"] == "confirmed"
    assert result["data"][0]["review"]["label"] == "已验证"
    assert result["data"][0]["review"]["summary"] == "已延续"


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
