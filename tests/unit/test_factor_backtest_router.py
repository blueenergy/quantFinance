"""Factor backtest jobs API.

The worker that drains this queue lives in another repo and cannot be imported
here, so the params contract is pinned by name: ``test_job_params_carry_exactly``
fails if this service starts sending a key the worker would refuse.
"""

import asyncio
from datetime import datetime

import pytest
from fastapi import HTTPException

from routers import factor_backtest


class _Cursor(list):
    def sort(self, field, direction=None):
        if isinstance(field, list):
            field, direction = field[0]
        return _Cursor(sorted(self, key=lambda row: row.get(field) or "", reverse=direction == -1))

    def limit(self, n):
        return _Cursor(list(self)[:n])


class _Collection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    def create_index(self, *args, **kwargs):
        return True

    def find(self, query=None, projection=None):
        return _Cursor([dict(doc) for doc in self.docs if self._matches(doc, query or {})])

    def find_one(self, query=None, projection=None, sort=None):
        rows = self.find(query, projection)
        return rows[0] if rows else None

    def insert_one(self, doc):
        self.docs.append(dict(doc))

    def delete_one(self, query=None):
        for idx, doc in enumerate(self.docs):
            if self._matches(doc, query or {}):
                del self.docs[idx]
                return type("DeleteResult", (), {"deleted_count": 1})()
        return type("DeleteResult", (), {"deleted_count": 0})()

    def delete_many(self, query=None):
        keep = [doc for doc in self.docs if not self._matches(doc, query or {})]
        deleted = len(self.docs) - len(keep)
        self.docs = keep
        return type("DeleteResult", (), {"deleted_count": deleted})()

    def _matches(self, doc, query):
        return all(doc.get(key) == expected for key, expected in query.items())


def _collections(catalog=None, jobs=None, results=None):
    return {
        "factor_backtest_jobs": _Collection(jobs),
        "factor_backtest_results": _Collection(results),
        "factor_catalog": _Collection(catalog),
    }


def _catalog_docs():
    return [
        {
            "factor_set": "alpha158",
            "factor_count": 2,
            "factors": [
                {"name": "MA20", "expression": "Mean($close, 20)/$close", "family": "趋势", "window": 20},
                {"name": "STD20", "expression": "Std($close, 20)/$close", "family": "波动", "window": 20},
            ],
        },
        {
            "factor_set": "alpha8",
            "factor_count": 1,
            "factors": [{"name": "alpha1", "expression": "($close-$open)/($high-$low)", "family": "K线形态"}],
        },
    ]


def _request(**overrides):
    payload = {
        "name": "因子回测",
        "index_code": "csi1000",
        "start_date": "2020-01-01",
        "end_date": "2024-12-31",
    }
    payload.update(overrides)
    return factor_backtest.CreateFactorBacktestJobRequest(**payload)


def _create(collections, monkeypatch, **overrides):
    monkeypatch.setattr(factor_backtest, "get_trading_db_collections", lambda: collections)
    return asyncio.run(
        factor_backtest.create_factor_backtest_job(_request(**overrides), current_user={"id": "user-1"})
    )


# Calling the handlers directly skips FastAPI's dependency resolution, so every
# Query parameter has to be named or it arrives as a Query object.
def _list_jobs(user_id="user-1", *, status=None, factor_set=None, index_code=None, limit=50):
    return asyncio.run(
        factor_backtest.list_factor_backtest_jobs(
            status=status,
            factor_set=factor_set,
            index_code=index_code,
            limit=limit,
            current_user={"id": user_id},
        )
    )


def _catalog(user_id="user-1", *, factor_set=None):
    return asyncio.run(
        factor_backtest.list_factor_catalog(factor_set=factor_set, current_user={"id": user_id})
    )


def test_create_enqueues_a_pending_job(monkeypatch):
    collections = _collections()

    response = _create(collections, monkeypatch)

    job = response["data"]
    assert job["status"] == "pending"
    assert job["user_id"] == "user-1"
    assert job["attempts"] == 0
    assert job["result_id"] is None
    assert job["start_date"] == "20200101"
    assert job["end_date"] == "20241231"
    assert collections["factor_backtest_jobs"].docs[0]["job_id"] == job["job_id"]


def test_job_params_carry_exactly_what_the_worker_accepts(monkeypatch):
    """FactorBacktestParams.from_mapping refuses any key it does not define."""
    collections = _collections()

    params = _create(collections, monkeypatch)["data"]["params"]

    assert set(params) == {
        "source",
        "index_code",
        "start_date",
        "end_date",
        "factor_set",
        "factors",
        "warmup_days",
        "horizons",
        "quantiles",
        "min_names",
        "top_k",
        "screen_top",
        "screen_horizon",
        "slippage_bps",
        "turnover",
    }


def test_job_params_pin_the_mongo_source_and_leave_the_budget_to_the_worker(monkeypatch):
    """A caller naming a panel path or a larger budget could read files or OOM the pod."""
    collections = _collections()

    params = _create(collections, monkeypatch)["data"]["params"]

    assert params["source"] == "mongo"
    assert "panel" not in params
    assert "memory_budget_gb" not in params
    assert "workers" not in params


def test_create_normalizes_horizons_and_top_k(monkeypatch):
    collections = _collections()

    params = _create(collections, monkeypatch, horizons=[20, 5, 5, 1], top_k=[50, 20, 20])["data"]["params"]

    assert params["horizons"] == [1, 5, 20]
    assert params["top_k"] == [20, 50]


def test_create_rejects_a_reversed_date_range(monkeypatch):
    collections = _collections()

    with pytest.raises(HTTPException) as exc:
        _create(collections, monkeypatch, start_date="2024-12-31", end_date="2020-01-01")

    assert exc.value.status_code == 400


def test_create_rejects_a_factor_outside_the_published_catalog(monkeypatch):
    collections = _collections(catalog=_catalog_docs())

    with pytest.raises(HTTPException) as exc:
        _create(collections, monkeypatch, factor_set="alpha158", factors=["MA20", "NOPE9"])

    assert exc.value.status_code == 400
    assert "NOPE9" in exc.value.detail
    assert "MA20" not in exc.value.detail


def test_create_accepts_named_factors_from_the_catalog(monkeypatch):
    collections = _collections(catalog=_catalog_docs())

    params = _create(collections, monkeypatch, factor_set="alpha158", factors=["MA20"])["data"]["params"]

    assert params["factors"] == ["MA20"]


def test_create_defers_to_the_worker_when_no_catalog_is_published(monkeypatch):
    """The worker validates names anyway; an unpublished catalog must not block work."""
    collections = _collections()

    params = _create(collections, monkeypatch, factor_set="alpha158", factors=["MA20"])["data"]["params"]

    assert params["factors"] == ["MA20"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"index_code": "sp500"},
        {"factor_set": "alpha42"},
        {"quantiles": 1},
        {"min_names": 3, "quantiles": 5},
        {"horizons": [5], "screen_horizon": 20},
        {"turnover": 0},
        {"turnover": 1.5},
        {"factors": []},
        {"horizons": []},
    ],
)
def test_create_request_rejects_bad_parameters(overrides):
    with pytest.raises(Exception):
        _request(**overrides)


def test_alpha158_requests_do_not_have_to_name_a_warmup(monkeypatch):
    """The worker raises alpha158 warm-up to 150 itself, so the default is fine here."""
    collections = _collections()

    params = _create(collections, monkeypatch, factor_set="alpha158")["data"]["params"]

    assert params["warmup_days"] == 60


def test_list_jobs_is_scoped_to_the_caller(monkeypatch):
    jobs = [
        {"job_id": "a", "user_id": "user-1", "status": "completed", "created_at": "2026-01-02", "report": "big"},
        {"job_id": "b", "user_id": "user-2", "status": "completed", "created_at": "2026-01-03"},
        {"job_id": "c", "user_id": "user-1", "status": "pending", "created_at": "2026-01-01"},
    ]
    collections = _collections(jobs=jobs)
    monkeypatch.setattr(factor_backtest, "get_trading_db_collections", lambda: collections)

    rows = _list_jobs()["data"]

    assert [row["job_id"] for row in rows] == ["a", "c"]


def test_list_jobs_filters_by_status(monkeypatch):
    jobs = [
        {"job_id": "a", "user_id": "user-1", "status": "completed", "created_at": "2026-01-02"},
        {"job_id": "c", "user_id": "user-1", "status": "pending", "created_at": "2026-01-01"},
    ]
    collections = _collections(jobs=jobs)
    monkeypatch.setattr(factor_backtest, "get_trading_db_collections", lambda: collections)

    rows = _list_jobs(status="pending")["data"]

    assert [row["job_id"] for row in rows] == ["c"]


def test_get_job_hides_another_users_job(monkeypatch):
    collections = _collections(jobs=[{"job_id": "a", "user_id": "user-2", "status": "completed"}])
    monkeypatch.setattr(factor_backtest, "get_trading_db_collections", lambda: collections)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(factor_backtest.get_factor_backtest_job("a", current_user={"id": "user-1"}))

    assert exc.value.status_code == 404


def test_get_results_returns_the_full_report(monkeypatch):
    collections = _collections(
        jobs=[{"job_id": "a", "user_id": "user-1", "status": "completed", "result_id": "r1"}],
        results=[{"result_id": "r1", "job_id": "a", "user_id": "user-1", "report": {"factors": {"MA20": {}}}}],
    )
    monkeypatch.setattr(factor_backtest, "get_trading_db_collections", lambda: collections)

    data = asyncio.run(factor_backtest.get_factor_backtest_results("a", current_user={"id": "user-1"}))["data"]

    assert data["report"]["factors"]["MA20"] == {}


def test_get_results_404s_while_the_job_is_still_running(monkeypatch):
    collections = _collections(jobs=[{"job_id": "a", "user_id": "user-1", "status": "running"}])
    monkeypatch.setattr(factor_backtest, "get_trading_db_collections", lambda: collections)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(factor_backtest.get_factor_backtest_results("a", current_user={"id": "user-1"}))

    assert exc.value.status_code == 404


@pytest.mark.parametrize("status", ["pending", "running"])
def test_delete_refuses_an_in_flight_job(monkeypatch, status):
    """The worker still holds it and would write its result back over the deletion."""
    collections = _collections(jobs=[{"job_id": "a", "user_id": "user-1", "status": status}])
    monkeypatch.setattr(factor_backtest, "get_trading_db_collections", lambda: collections)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(factor_backtest.delete_factor_backtest_job("a", current_user={"id": "user-1"}))

    assert exc.value.status_code == 409
    assert collections["factor_backtest_jobs"].docs


def test_delete_removes_a_finished_job_and_its_report(monkeypatch):
    collections = _collections(
        jobs=[{"job_id": "a", "user_id": "user-1", "status": "completed"}],
        results=[{"result_id": "r1", "job_id": "a", "user_id": "user-1"}],
    )
    monkeypatch.setattr(factor_backtest, "get_trading_db_collections", lambda: collections)

    data = asyncio.run(factor_backtest.delete_factor_backtest_job("a", current_user={"id": "user-1"}))["data"]

    assert data == {"job_id": "a", "deleted_jobs": 1, "deleted_results": 1}
    assert collections["factor_backtest_jobs"].docs == []
    assert collections["factor_backtest_results"].docs == []


def test_catalog_returns_every_published_set(monkeypatch):
    collections = _collections(catalog=_catalog_docs())
    monkeypatch.setattr(factor_backtest, "get_trading_db_collections", lambda: collections)

    rows = _catalog()["data"]

    assert [row["factor_set"] for row in rows] == ["alpha158", "alpha8"]
    assert rows[0]["factors"][0]["expression"] == "Mean($close, 20)/$close"


def test_catalog_can_be_narrowed_to_one_set(monkeypatch):
    collections = _collections(catalog=_catalog_docs())
    monkeypatch.setattr(factor_backtest, "get_trading_db_collections", lambda: collections)

    rows = _catalog(factor_set="alpha8")["data"]

    assert [row["factor_set"] for row in rows] == ["alpha8"]


def test_catalog_says_so_when_the_worker_has_not_published_yet(monkeypatch):
    collections = _collections()
    monkeypatch.setattr(factor_backtest, "get_trading_db_collections", lambda: collections)

    with pytest.raises(HTTPException) as exc:
        _catalog()

    assert exc.value.status_code == 503


def test_endpoints_reject_a_user_without_an_id(monkeypatch):
    collections = _collections()
    monkeypatch.setattr(factor_backtest, "get_trading_db_collections", lambda: collections)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(factor_backtest.create_factor_backtest_job(_request(), current_user={}))

    assert exc.value.status_code == 401


def test_created_at_is_set_so_the_worker_can_order_the_queue(monkeypatch):
    """claim() sorts by created_at, so a job without it would never be picked up."""
    collections = _collections()

    job = _create(collections, monkeypatch)["data"]

    assert datetime.fromisoformat(job["created_at"])
