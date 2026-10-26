import pytest

from routers import entropy_ranking


class _FakeAdjColl:
    def __init__(self, docs):
        self.docs = docs

    def find_one(self, query, projection=None, sort=None):
        sym = query.get("symbol")
        td = query.get("trade_date")
        rows = [d for d in self.docs if d.get("symbol") == sym]
        if isinstance(td, dict) and "$lte" in td:
            ub = str(td["$lte"])
            rows = [d for d in rows if str(d.get("trade_date", "")) <= ub]
            rows.sort(key=lambda d: str(d.get("trade_date", "")), reverse=True)
            return rows[0] if rows else None
        if td is not None:
            rows = [d for d in rows if str(d.get("trade_date")) == str(td)]
        return rows[0] if rows else None


class _FakeVpColl:
    def __init__(self, docs, adj_docs):
        self.docs = docs
        self._adj = _FakeAdjColl(adj_docs)

    def find_one(self, query, sort=None):
        variants = query.get("symbol", {}).get("$in", [])
        td = query.get("trade_date")
        rows = [d for d in self.docs if d.get("symbol") in variants]
        if isinstance(td, dict) and "$lte" in td:
            ub = str(td["$lte"])
            rows = [d for d in rows if str(d.get("trade_date", "")) <= ub]
            rows.sort(key=lambda d: str(d.get("trade_date", "")), reverse=True)
            return rows[0] if rows else None
        if isinstance(td, dict) and "$gte" in td:
            lb = str(td["$gte"])
            rows = [d for d in rows if str(d.get("trade_date", "")) >= lb]
            rows.sort(key=lambda d: str(d.get("trade_date", "")))
            return rows[0] if rows else None
        return None


class _FakeDataDb(dict):
    def __getitem__(self, key):
        if key == "volume_price":
            return self._vp
        if key == "stock_adj_factor":
            return self._vp._adj
        return super().__getitem__(key)


@pytest.mark.unit
def test_rank_entropy_docs_missing_scores_last():
    class _FakeColl:
        def aggregate(self, pipeline):
            docs = [
                {"symbol": "AAA", "final_entropy_score": 0.2},
                {"symbol": "BBB"},
                {"symbol": "CCC", "entropy_score": 0.1},
                {"symbol": "DDD", "final_entropy_score": 0.05},
            ]
            match = pipeline[0]["$match"]
            rows = [d for d in docs if d.get("score_date", "20260101") == match.get("score_date", "20260101")]
            ranked = sorted(rows, key=entropy_ranking._entropy_sort_key)
            limit = pipeline[3]["$limit"]
            return [{k: v for k, v in d.items()} for d in ranked[:limit]]

    docs = entropy_ranking._rank_entropy_docs(
        _FakeColl(),
        {"score_date": "20260101"},
        3,
    )
    assert [d["symbol"] for d in docs] == ["DDD", "CCC", "AAA"]


@pytest.mark.unit
def test_future_returns_uses_hfq_factors(monkeypatch):
    vp = _FakeVpColl(
        [
            {"symbol": "600519.SH", "trade_date": "20240101", "close": 10.0},
            {"symbol": "600519.SH", "trade_date": "20240201", "close": 11.0},
        ],
        [
            {"symbol": "600519.SH", "trade_date": "20240101", "adj_factor": 1.0},
            {"symbol": "600519.SH", "trade_date": "20240201", "adj_factor": 1.1},
        ],
    )
    fake_db = _FakeDataDb()
    fake_db._vp = vp
    monkeypatch.setattr(entropy_ranking, "get_data_db", lambda: fake_db)

    out = entropy_ranking._future_returns("600519.SH", "20240101")

    # hfq: (11*1.1)/(10*1.0)-1 = 21%
    assert out["future_return_1m_pct"] == pytest.approx(21.0)


@pytest.mark.unit
def test_future_returns_degrades_both_legs_when_one_factor_missing(monkeypatch):
    """目标日缺因子时必须整体退裸价，不能「后复权 ÷ 裸价」。"""
    vp = _FakeVpColl(
        [
            {"symbol": "600519.SH", "trade_date": "20240101", "close": 10.0},
            {"symbol": "600519.SH", "trade_date": "20240201", "close": 11.0},
        ],
        [
            # 只有基准日有因子，目标日缺失（如目标日当天因子尚未入库）
            {"symbol": "600519.SH", "trade_date": "20240101", "adj_factor": 2.0},
        ],
    )
    fake_db = _FakeDataDb()
    fake_db._vp = vp
    monkeypatch.setattr(entropy_ranking, "get_data_db", lambda: fake_db)

    out = entropy_ranking._future_returns("600519.SH", "20240101")

    # 裸价口径: 11/10-1 = 10%；旧实现会算成 11/(10*2)-1 = -45%
    assert out["future_return_1m_pct"] == pytest.approx(10.0)
