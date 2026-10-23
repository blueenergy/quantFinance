"""15:10 IV 定时任务：只在 SSE 交易日入队。"""
from datetime import datetime

import scheduler


class FakeCalendar:
    def __init__(self, trading_day):
        self.trading_day = trading_day

    def find_one(self, query):
        return {"trade_date": query["trade_date"], "is_open": 1} if self.trading_day else None


class FakeTasks:
    def __init__(self):
        self.inserted = []

    def find_one(self, query):
        return None

    def insert_one(self, doc):
        self.inserted.append(doc)

        class Result:
            inserted_id = "fake-id"

        return Result()


class FakeUsers:
    def find_one(self, query):
        return None


def _patch_db(monkeypatch, trading_day):
    tasks = FakeTasks()
    monkeypatch.setattr(
        "db.get_data_db",
        lambda: {"trade_calendar": FakeCalendar(trading_day)},
    )
    monkeypatch.setattr(
        "db.get_db_collections",
        lambda: {"analysis_tasks": tasks, "users": FakeUsers()},
    )
    return tasks


def test_iv_job_skips_non_trading_day(monkeypatch):
    tasks = _patch_db(monkeypatch, trading_day=False)

    scheduler.generate_iv_analysis_task("iv_analysis_1510")

    assert tasks.inserted == []


def test_iv_job_enqueues_on_trading_day(monkeypatch):
    tasks = _patch_db(monkeypatch, trading_day=True)

    scheduler.generate_iv_analysis_task("iv_analysis_1510")

    assert len(tasks.inserted) == 1
    doc = tasks.inserted[0]
    assert doc["type"] == "iv_analysis"
    assert doc["params"]["with_llm"] is True
    assert doc["params"]["date"] == datetime.now().strftime("%Y%m%d")
