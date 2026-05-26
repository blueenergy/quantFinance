import pytest
from fastapi.testclient import TestClient
from routers import stock_ranking
from main import app

# Small in-memory fake collection classes to mimic minimal pymongo API used in router
class FakeCollection:
    def __init__(self, docs=None):
        self.docs = docs or []

    @staticmethod
    def _doc_matches_query(d, query):
        if not query:
            return True
        for k, v in query.items():
            if k == "symbol" and isinstance(v, dict) and "$in" in v:
                if d.get("symbol") not in v["$in"]:
                    return False
            elif k == "index_codes":
                want = v
                ic = d.get("index_codes")
                if isinstance(want, str):
                    if isinstance(ic, list):
                        if want not in ic:
                            return False
                    elif ic != want:
                        return False
                elif d.get(k) != v:
                    return False
            elif d.get(k) != v:
                return False
        return True

    def find(self, query=None, sort=None, projection=None):
        # return list-like cursor with limit
        class Cursor(list):
            def limit(self, n):
                return list(self)[:n]

        rows = self.docs
        if query:
            rows = [d for d in self.docs if self._doc_matches_query(d, query)]
        if projection:
            keep = [k for k, v in projection.items() if v == 1]
            rows = [{k: d.get(k) for k in keep if k in d} for d in rows]
        return Cursor(rows)

    def aggregate(self, pipeline):
        """模拟 $match / $sort / $group($first) / $replaceRoot / $limit；与 selected 轻量管道及排行榜管道兼容。"""
        docs = list(self.docs)
        used_group_first = False
        for stage in pipeline:
            if "$match" in stage:
                m = stage["$match"]
                sym_f = m.get("symbol")
                if isinstance(sym_f, dict) and "$in" in sym_f:
                    allow = set(sym_f["$in"])
                    docs = [d for d in docs if d.get("symbol") in allow]
                else:
                    docs = [d for d in docs if self._doc_matches_query(d, m)]
            elif "$sort" in stage:
                so = stage["$sort"]

                def _sort_key(d):
                    parts = []
                    for k, direction in so.items():
                        v = d.get(k)
                        desc = direction == -1
                        if k in ("score_date", "trade_date") and desc:
                            try:
                                parts.append(-int(str(v)[:8]))
                            except (TypeError, ValueError):
                                parts.append(0)
                        elif k == "composite_score" and desc:
                            parts.append(
                                -stock_ranking.extract_strategy_score(
                                    d.get("composite_score", 0), "balanced"
                                )
                            )
                        else:
                            parts.append(v)
                    return tuple(parts)

                docs.sort(key=_sort_key)
            elif "$group" in stage:
                g = stage["$group"]
                doc_first = isinstance(g.get("doc"), dict) and g["doc"].get("$first") == "$$ROOT"
                grp_first = isinstance(g.get("first"), dict) and g["first"].get("$first") == "$$ROOT"
                if g.get("_id") == "$symbol" and (doc_first or grp_first):
                    used_group_first = True
                    key = "doc" if doc_first else "first"
                    by_sym = {}
                    for d in docs:
                        sid = d.get("symbol")
                        if sid not in by_sym:
                            by_sym[sid] = d
                    docs = [{"_id": sid, key: d} for sid, d in by_sym.items()]
            elif "$replaceRoot" in stage:
                nr = stage["$replaceRoot"].get("newRoot")
                if nr in ("$doc", "$first") and docs and isinstance(docs[0], dict):
                    k = "doc" if nr == "$doc" else "first"
                    if k in docs[0]:
                        docs = [row[k] for row in docs]
            elif "$limit" in stage:
                lim = stage["$limit"]
                if isinstance(lim, int) and lim > 0:
                    docs = docs[:lim]
            elif "$project" in stage:
                proj = stage["$project"]
                keep = [k for k, v in proj.items() if v == 1]
                docs = [{k: d.get(k) for k in keep if k in d} for d in docs]
        if used_group_first:
            return docs
        return sorted(
            docs,
            key=lambda d: stock_ranking.extract_strategy_score(
                d.get("composite_score", 0), "balanced"
            ),
            reverse=True,
        )

    def find_one(self, query):
        for d in self.docs:
            if self._doc_matches_query(d, query):
                return d
        return None


class FakeDB:
    def __init__(self, stock_scores_docs=None, stock_info_docs=None):
        self._collections = {
            'stock_scores': FakeCollection(stock_scores_docs or []),
            'stock_info': FakeCollection(stock_info_docs or [])
        }

    def __getitem__(self, key):
        return self._collections[key]


class FakeVolumePriceCollection:
    """Supports find_one with sort and trade_date $lte for return_since_score tests."""

    def __init__(self, docs):
        self.docs = list(docs)

    def find_one(self, query, sort=None):
        sym = query.get("symbol")
        rows = [d for d in self.docs if d.get("symbol") == sym]
        td_flt = query.get("trade_date")
        if isinstance(td_flt, dict) and "$lte" in td_flt:
            ub = str(td_flt["$lte"])
            rows = [d for d in rows if str(d.get("trade_date", "")) <= ub]
        if not rows:
            return None
        if sort:
            k, direction = sort[0]
            assert k == "trade_date"
            rows.sort(key=lambda d: str(d.get("trade_date", "")), reverse=(direction == -1))
        return rows[0]


def _vp_ranking_pair():
    return [
        {"symbol": "AAA.SH", "trade_date": "20250620", "close": 80},
        {"symbol": "AAA.SH", "trade_date": "20250901", "close": 50},
        {"symbol": "AAA.SH", "trade_date": "20250920", "close": 100},
        {"symbol": "AAA.SH", "trade_date": "20251001", "close": 110},
        {"symbol": "BBB.SH", "trade_date": "20250620", "close": 250},
        {"symbol": "BBB.SH", "trade_date": "20250920", "close": 200},
        {"symbol": "BBB.SH", "trade_date": "20251001", "close": 180},
    ]


@pytest.mark.unit
def test_stock_rankings_sorts_mixed_composite(monkeypatch):
    # composite_score: one doc with dict, one with numeric
    docs = [
        {'symbol': 'AAA.SH', 'composite_score': {'balanced': 75}, 'cycle_score': 10, 'value_score': 20, 'fundamental_score': 30, 'growth_score': 5, 'technical_score': 7, 'money_flow_score': 3, 'score_date': '20250920'},
        {'symbol': 'BBB.SH', 'composite_score': 80, 'cycle_score': 12, 'value_score': 21, 'fundamental_score': 31, 'growth_score': 6, 'technical_score': 6, 'money_flow_score': 4, 'score_date': '20250920'},
    ]
    info_docs = [{'symbol': 'AAA.SH', 'name': 'A Corp', 'industry': 'Tech'}, {'symbol': 'BBB.SH', 'name': 'B Corp', 'industry': 'Finance'}]
    fake_db = FakeDB(stock_scores_docs=docs, stock_info_docs=info_docs)

    def fake_get_collections():
        return fake_db

    monkeypatch.setattr(stock_ranking, 'get_db_collections', fake_get_collections)
    monkeypatch.setattr(
        stock_ranking,
        'get_data_db',
        lambda: {'volume_price': FakeVolumePriceCollection(_vp_ranking_pair())},
    )

    client = TestClient(app)
    resp = client.get('/api/stock-rankings?limit=2&strategy=balanced')
    assert resp.status_code == 200
    data = resp.json()
    # BBB has numeric 80 > AAA balanced 75, so BBB should be first
    assert data['data'][0]['symbol'] == 'BBB.SH'
    assert data['data'][1]['symbol'] == 'AAA.SH'
    assert data['data'][0]['return_since_score_pct'] == pytest.approx(-10.0)
    assert data['data'][1]['return_since_score_pct'] == pytest.approx(10.0)
    assert data['data'][1]['price_base_trade_date'] == '20250920'
    assert data['data'][1]['price_latest_trade_date'] == '20251001'
    assert data['data'][0]['prior_3m_return_pct'] == pytest.approx(-20.0)
    assert data['data'][1]['prior_3m_return_pct'] == pytest.approx(25.0)
    assert data['data'][1]['prior_3m_base_trade_date'] == '20250620'
    assert data['data'][1]['prior_3m_end_trade_date'] == '20250920'


@pytest.mark.unit
def test_selected_stock_rankings_defaults_and_fields(monkeypatch):
    docs = [
        {'symbol': 'CCC.SH', 'composite_score': {'balanced': 60}, 'cycle_score': 11, 'value_score': 19, 'fundamental_score': 25, 'growth_score': 6, 'technical_score': 9, 'money_flow_score': 5, 'score_date': '20250919'},
        {'symbol': 'DDD.SH', 'composite_score': 55, 'cycle_score': 9, 'value_score': 18, 'fundamental_score': 22, 'growth_score': 7, 'technical_score': 8, 'money_flow_score': 2, 'score_date': '20250919'},
    ]
    info_docs = [{'symbol': 'CCC.SH', 'name': 'C Corp', 'industry': 'Energy'}, {'symbol': 'DDD.SH', 'name': 'D Corp', 'industry': 'Retail'}]
    fake_db = FakeDB(stock_scores_docs=docs, stock_info_docs=info_docs)

    def fake_get_collections():
        return fake_db

    monkeypatch.setattr(stock_ranking, 'get_db_collections', fake_get_collections)
    vp = [
        {"symbol": "CCC.SH", "trade_date": "20250619", "close": 50},
        {"symbol": "CCC.SH", "trade_date": "20250910", "close": 80},
        {"symbol": "CCC.SH", "trade_date": "20250919", "close": 100},
        {"symbol": "CCC.SH", "trade_date": "20251005", "close": 115},
        {"symbol": "DDD.SH", "trade_date": "20250619", "close": 50},
        {"symbol": "DDD.SH", "trade_date": "20250919", "close": 50},
        {"symbol": "DDD.SH", "trade_date": "20251005", "close": 50},
    ]
    monkeypatch.setattr(
        stock_ranking,
        'get_data_db',
        lambda: {'volume_price': FakeVolumePriceCollection(vp)},
    )

    client = TestClient(app)
    body = {'symbols': ['CCC.SH', 'DDD.SH']}
    resp = client.post('/api/stock-rankings/selected', json=body)
    assert resp.status_code == 200
    j = resp.json()
    assert j['total'] == 2
    for s in j['data']:
        assert 'technical_score' in s
        assert isinstance(s['technical_score'], (int, float))
        assert 'composite_score' in s
        assert isinstance(s['composite_score'], dict)
        assert 'return_since_score_pct' in s
    by_sym = {s['symbol']: s for s in j['data']}
    assert by_sym['CCC.SH']['return_since_score_pct'] == pytest.approx(15.0)
    assert by_sym['DDD.SH']['return_since_score_pct'] == pytest.approx(0.0)
    assert by_sym['CCC.SH']['prior_3m_return_pct'] == pytest.approx(100.0)
    assert by_sym['CCC.SH']['prior_3m_base_trade_date'] == '20250619'
    assert by_sym['CCC.SH']['prior_3m_end_trade_date'] == '20250919'


@pytest.mark.unit
def test_selected_multi_date_per_date_return_since(monkeypatch):
    docs = [
        {'symbol': 'EEE.SH', 'composite_score': {'balanced': 70}, 'cycle_score': 1, 'value_score': 2, 'fundamental_score': 3, 'growth_score': 4, 'technical_score': 5, 'money_flow_score': 6, 'score_date': '20250910'},
        {'symbol': 'EEE.SH', 'composite_score': {'balanced': 71}, 'cycle_score': 1, 'value_score': 2, 'fundamental_score': 3, 'growth_score': 4, 'technical_score': 5, 'money_flow_score': 6, 'score_date': '20250920'},
    ]
    info_docs = [{'symbol': 'EEE.SH', 'name': 'E Corp', 'industry': 'X'}]
    fake_db = FakeDB(stock_scores_docs=docs, stock_info_docs=info_docs)

    monkeypatch.setattr(stock_ranking, 'get_db_collections', lambda: fake_db)
    vp = [
        {"symbol": "EEE.SH", "trade_date": "20250610", "close": 5},
        {"symbol": "EEE.SH", "trade_date": "20250620", "close": 10},
        {"symbol": "EEE.SH", "trade_date": "20250905", "close": 10},
        {"symbol": "EEE.SH", "trade_date": "20250910", "close": 10},
        {"symbol": "EEE.SH", "trade_date": "20250920", "close": 20},
        {"symbol": "EEE.SH", "trade_date": "20251001", "close": 40},
    ]
    monkeypatch.setattr(
        stock_ranking,
        'get_data_db',
        lambda: {'volume_price': FakeVolumePriceCollection(vp)},
    )

    client = TestClient(app)
    resp = client.post(
        '/api/stock-rankings/selected',
        json={'symbols': ['EEE.SH'], 'dates': ['20250910', '20250920']},
    )
    assert resp.status_code == 200
    j = resp.json()
    assert j['total'] == 1
    row = j['data'][0]
    assert 'per_date_return_since' in row
    pr = row['per_date_return_since']
    # 20250910 base close 10, latest 40 -> +300%
    assert pr['20250910']['return_since_score_pct'] == pytest.approx(300.0)
    # 20250920 base close 20, latest 40 -> +100%
    assert pr['20250920']['return_since_score_pct'] == pytest.approx(100.0)
    p3 = row['per_date_prior_3m_return']
    assert p3['20250910']['prior_3m_return_pct'] == pytest.approx(100.0)
    assert p3['20250920']['prior_3m_return_pct'] == pytest.approx(100.0)
    assert p3['20250920']['prior_3m_base_trade_date'] == '20250620'
    assert p3['20250920']['prior_3m_end_trade_date'] == '20250920'


@pytest.mark.unit
def test_selected_stock_rankings_sorts_by_strategy_query(monkeypatch):
    """balanced vs aggressive 名次相反时，顺序随 ?strategy= 变化。"""
    docs = [
        {
            "symbol": "AAA.SH",
            "composite_score": {"balanced": 90, "aggressive": 50},
            "cycle_score": 1,
            "value_score": 1,
            "fundamental_score": 1,
            "growth_score": 1,
            "technical_score": 1,
            "money_flow_score": 1,
            "score_date": "20250919",
        },
        {
            "symbol": "BBB.SH",
            "composite_score": {"balanced": 60, "aggressive": 95},
            "cycle_score": 1,
            "value_score": 1,
            "fundamental_score": 1,
            "growth_score": 1,
            "technical_score": 1,
            "money_flow_score": 1,
            "score_date": "20250919",
        },
    ]
    info_docs = [
        {"symbol": "AAA.SH", "name": "A", "industry": "X"},
        {"symbol": "BBB.SH", "name": "B", "industry": "Y"},
    ]
    fake_db = FakeDB(stock_scores_docs=docs, stock_info_docs=info_docs)
    monkeypatch.setattr(stock_ranking, "get_db_collections", lambda: fake_db)
    monkeypatch.setattr(
        stock_ranking,
        "get_data_db",
        lambda: {"volume_price": FakeVolumePriceCollection([])},
    )
    client = TestClient(app)
    body = {"symbols": ["AAA.SH", "BBB.SH"]}
    r_bal = client.post("/api/stock-rankings/selected?strategy=balanced", json=body)
    assert r_bal.status_code == 200
    data_bal = r_bal.json()["data"]
    assert [s["symbol"] for s in data_bal] == ["AAA.SH", "BBB.SH"]

    r_agg = client.post("/api/stock-rankings/selected?strategy=aggressive", json=body)
    assert r_agg.status_code == 200
    data_agg = r_agg.json()["data"]
    assert [s["symbol"] for s in data_agg] == ["BBB.SH", "AAA.SH"]

    r_body = client.post(
        "/api/stock-rankings/selected",
        json={"symbols": ["AAA.SH", "BBB.SH"], "strategy": "aggressive"},
    )
    assert r_body.status_code == 200
    assert [s["symbol"] for s in r_body.json()["data"]] == ["BBB.SH", "AAA.SH"]

    # Query 优先于 body.strategy
    r_q_over = client.post(
        "/api/stock-rankings/selected?strategy=balanced",
        json={"symbols": ["AAA.SH", "BBB.SH"], "strategy": "aggressive"},
    )
    assert r_q_over.status_code == 200
    assert [s["symbol"] for s in r_q_over.json()["data"]] == ["AAA.SH", "BBB.SH"]


@pytest.mark.unit
def test_selected_stock_rankings_sorts_by_strategy_with_date_query(monkeypatch):
    """固定 date= 分支同样按 strategy 排序。"""
    docs = [
        {
            "symbol": "AAA.SH",
            "composite_score": {"balanced": 88, "aggressive": 40},
            "cycle_score": 1,
            "value_score": 1,
            "fundamental_score": 1,
            "growth_score": 1,
            "technical_score": 1,
            "money_flow_score": 1,
            "score_date": "20250919",
        },
        {
            "symbol": "BBB.SH",
            "composite_score": {"balanced": 50, "aggressive": 99},
            "cycle_score": 1,
            "value_score": 1,
            "fundamental_score": 1,
            "growth_score": 1,
            "technical_score": 1,
            "money_flow_score": 1,
            "score_date": "20250919",
        },
    ]
    info_docs = [
        {"symbol": "AAA.SH", "name": "A", "industry": "X"},
        {"symbol": "BBB.SH", "name": "B", "industry": "Y"},
    ]
    fake_db = FakeDB(stock_scores_docs=docs, stock_info_docs=info_docs)
    monkeypatch.setattr(stock_ranking, "get_db_collections", lambda: fake_db)
    monkeypatch.setattr(
        stock_ranking,
        "get_data_db",
        lambda: {"volume_price": FakeVolumePriceCollection([])},
    )
    client = TestClient(app)
    body = {"symbols": ["BBB.SH", "AAA.SH"]}
    r_bal = client.post(
        "/api/stock-rankings/selected?date=20250919&strategy=balanced", json=body
    )
    assert r_bal.status_code == 200
    assert [s["symbol"] for s in r_bal.json()["data"]] == ["AAA.SH", "BBB.SH"]

    r_agg = client.post(
        "/api/stock-rankings/selected?date=20250919&strategy=aggressive", json=body
    )
    assert r_agg.status_code == 200
    assert [s["symbol"] for s in r_agg.json()["data"]] == ["BBB.SH", "AAA.SH"]


@pytest.mark.unit
def test_selected_matches_scores_when_request_ts_code_db_bare(monkeypatch):
    """指数成分返回 ts_code 时，stock_scores 仍为裸代码应能命中。"""
    docs = [
        {
            "symbol": "600519",
            "composite_score": {"balanced": 80},
            "cycle_score": 1,
            "value_score": 2,
            "fundamental_score": 3,
            "growth_score": 4,
            "technical_score": 5,
            "money_flow_score": 6,
            "score_date": "20250919",
        },
    ]
    info_docs = [{"symbol": "600519", "name": "贵州茅台", "industry": "白酒"}]
    fake_db = FakeDB(stock_scores_docs=docs, stock_info_docs=info_docs)
    monkeypatch.setattr(stock_ranking, "get_db_collections", lambda: fake_db)
    monkeypatch.setattr(
        stock_ranking,
        "get_data_db",
        lambda: {"volume_price": FakeVolumePriceCollection([])},
    )
    client = TestClient(app)
    resp = client.post(
        "/api/stock-rankings/selected",
        json={"symbols": ["600519.SH"]},
    )
    assert resp.status_code == 200
    j = resp.json()
    assert j["total"] == 1
    assert j["data"][0]["symbol"] == "600519.SH"
    assert j["data"][0]["composite_score"]["balanced"] == 80


def test_selected_multi_date_sorted_by_strategy(monkeypatch):
    """多日期分支返回列表按 effective_strategy 与 composite_score 排序。"""
    docs = [
        {
            "symbol": "MMM.SH",
            "composite_score": {"balanced": 70, "aggressive": 30},
            "cycle_score": 1,
            "value_score": 1,
            "fundamental_score": 1,
            "growth_score": 1,
            "technical_score": 1,
            "money_flow_score": 1,
            "score_date": "20250910",
        },
        {
            "symbol": "NNN.SH",
            "composite_score": {"balanced": 65, "aggressive": 90},
            "cycle_score": 1,
            "value_score": 1,
            "fundamental_score": 1,
            "growth_score": 1,
            "technical_score": 1,
            "money_flow_score": 1,
            "score_date": "20250910",
        },
    ]
    info_docs = [
        {"symbol": "MMM.SH", "name": "M", "industry": "X"},
        {"symbol": "NNN.SH", "name": "N", "industry": "Y"},
    ]
    fake_db = FakeDB(stock_scores_docs=docs, stock_info_docs=info_docs)
    monkeypatch.setattr(stock_ranking, "get_db_collections", lambda: fake_db)
    monkeypatch.setattr(
        stock_ranking,
        "get_data_db",
        lambda: {"volume_price": FakeVolumePriceCollection([])},
    )
    client = TestClient(app)
    j = {
        "symbols": ["NNN.SH", "MMM.SH"],
        "dates": ["20250910"],
    }
    r_bal = client.post("/api/stock-rankings/selected?strategy=balanced", json=j)
    assert r_bal.status_code == 200
    assert [s["symbol"] for s in r_bal.json()["data"]] == ["MMM.SH", "NNN.SH"]

    r_agg = client.post("/api/stock-rankings/selected?strategy=aggressive", json=j)
    assert r_agg.status_code == 200
    assert [s["symbol"] for s in r_agg.json()["data"]] == ["NNN.SH", "MMM.SH"]


@pytest.mark.unit
def test_selected_skips_volume_price_enrich_when_over_auto_threshold(monkeypatch):
    """成分股批量超阈值时不跑 volume_price enrich，减轻 DB。"""
    monkeypatch.setattr(stock_ranking, "SELECTED_RETURN_SINCE_AUTO_THRESHOLD", 1)
    docs = [
        {
            "symbol": "AAA.SH",
            "composite_score": {"balanced": 50},
            "cycle_score": 1,
            "value_score": 1,
            "fundamental_score": 1,
            "growth_score": 1,
            "technical_score": 1,
            "money_flow_score": 1,
            "score_date": "20250919",
        },
        {
            "symbol": "BBB.SH",
            "composite_score": {"balanced": 51},
            "cycle_score": 1,
            "value_score": 1,
            "fundamental_score": 1,
            "growth_score": 1,
            "technical_score": 1,
            "money_flow_score": 1,
            "score_date": "20250919",
        },
    ]
    info_docs = [
        {"symbol": "AAA.SH", "name": "A", "industry": "X"},
        {"symbol": "BBB.SH", "name": "B", "industry": "Y"},
    ]
    fake_db = FakeDB(stock_scores_docs=docs, stock_info_docs=info_docs)
    monkeypatch.setattr(stock_ranking, "get_db_collections", lambda: fake_db)
    monkeypatch.setattr(
        stock_ranking,
        "get_data_db",
        lambda: {"volume_price": FakeVolumePriceCollection([])},
    )
    client = TestClient(app)
    r = client.post("/api/stock-rankings/selected", json={"symbols": ["AAA.SH", "BBB.SH"]})
    assert r.status_code == 200
    for row in r.json()["data"]:
        assert row.get("return_since_score_pct") is None
        assert row.get("prior_3m_return_pct") is None


@pytest.mark.unit
def test_selected_pagination_returns_slice_and_total(monkeypatch):
    docs = [
        {
            "symbol": "AAA.SH",
            "composite_score": {"balanced": 90},
            "cycle_score": 1,
            "value_score": 1,
            "fundamental_score": 1,
            "growth_score": 1,
            "technical_score": 1,
            "money_flow_score": 1,
            "score_date": "20250919",
        },
        {
            "symbol": "BBB.SH",
            "composite_score": {"balanced": 70},
            "cycle_score": 1,
            "value_score": 1,
            "fundamental_score": 1,
            "growth_score": 1,
            "technical_score": 1,
            "money_flow_score": 1,
            "score_date": "20250919",
        },
        {
            "symbol": "CCC.SH",
            "composite_score": {"balanced": 50},
            "cycle_score": 1,
            "value_score": 1,
            "fundamental_score": 1,
            "growth_score": 1,
            "technical_score": 1,
            "money_flow_score": 1,
            "score_date": "20250919",
        },
    ]
    info_docs = [
        {"symbol": "AAA.SH", "name": "A", "industry": "X"},
        {"symbol": "BBB.SH", "name": "B", "industry": "Y"},
        {"symbol": "CCC.SH", "name": "C", "industry": "Z"},
    ]
    fake_db = FakeDB(stock_scores_docs=docs, stock_info_docs=info_docs)
    monkeypatch.setattr(stock_ranking, "get_db_collections", lambda: fake_db)
    monkeypatch.setattr(
        stock_ranking,
        "get_data_db",
        lambda: {"volume_price": FakeVolumePriceCollection([])},
    )
    client = TestClient(app)
    r0 = client.post(
        "/api/stock-rankings/selected?page_size=1&offset=0",
        json={"symbols": ["AAA.SH", "BBB.SH", "CCC.SH"], "index_mode": True},
    )
    assert r0.status_code == 200
    j0 = r0.json()
    assert j0["total"] == 3
    assert len(j0["data"]) == 1
    assert j0["data"][0]["symbol"] == "AAA.SH"
    r1 = client.post(
        "/api/stock-rankings/selected?page_size=1&offset=1",
        json={"symbols": ["AAA.SH", "BBB.SH", "CCC.SH"], "index_mode": True},
    )
    assert r1.json()["data"][0]["symbol"] == "BBB.SH"


@pytest.mark.unit
def test_selected_index_mode_skips_enrich(monkeypatch):
    called = []

    def track_enrich(rows, col):
        called.append(True)

    monkeypatch.setattr(stock_ranking, "enrich_rows_return_since_score", track_enrich)
    docs = [
        {
            "symbol": "YYY.SH",
            "composite_score": {"balanced": 41},
            "cycle_score": 1,
            "value_score": 1,
            "fundamental_score": 1,
            "growth_score": 1,
            "technical_score": 1,
            "money_flow_score": 1,
            "score_date": "20250919",
        },
    ]
    info_docs = [{"symbol": "YYY.SH", "name": "Y", "industry": "Y"}]
    fake_db = FakeDB(stock_scores_docs=docs, stock_info_docs=info_docs)
    monkeypatch.setattr(stock_ranking, "get_db_collections", lambda: fake_db)
    monkeypatch.setattr(
        stock_ranking,
        "get_data_db",
        lambda: {"volume_price": FakeVolumePriceCollection([])},
    )
    client = TestClient(app)
    r = client.post(
        "/api/stock-rankings/selected",
        json={"symbols": ["YYY.SH"], "index_mode": True},
    )
    assert r.status_code == 200
    assert called == []
    assert r.json()["data"][0].get("return_since_score_pct") is None
    assert r.json()["data"][0].get("prior_3m_return_pct") is None


@pytest.mark.unit
def test_selected_skip_return_since_true_in_body(monkeypatch):
    called = []

    def track_enrich(rows, col):
        called.append(True)
        stock_ranking.enrich_rows_return_since_score(rows, col)

    monkeypatch.setattr(stock_ranking, "enrich_rows_return_since_score", track_enrich)
    docs = [
        {
            "symbol": "ZZZ.SH",
            "composite_score": {"balanced": 40},
            "cycle_score": 1,
            "value_score": 1,
            "fundamental_score": 1,
            "growth_score": 1,
            "technical_score": 1,
            "money_flow_score": 1,
            "score_date": "20250919",
        },
    ]
    info_docs = [{"symbol": "ZZZ.SH", "name": "Z", "industry": "Z"}]
    fake_db = FakeDB(stock_scores_docs=docs, stock_info_docs=info_docs)
    monkeypatch.setattr(stock_ranking, "get_db_collections", lambda: fake_db)
    monkeypatch.setattr(
        stock_ranking,
        "get_data_db",
        lambda: {"volume_price": FakeVolumePriceCollection([])},
    )
    client = TestClient(app)
    r = client.post(
        "/api/stock-rankings/selected",
        json={"symbols": ["ZZZ.SH"], "skip_return_since": True},
    )
    assert r.status_code == 200
    assert called == []
    assert r.json()["data"][0].get("return_since_score_pct") is None
    assert r.json()["data"][0].get("prior_3m_return_pct") is None


@pytest.mark.unit
def test_by_index_rankings_latest_per_symbol_and_sort(monkeypatch):
    """无 date：每 symbol 取最新 score_date，再筛 index_codes；按 strategy 排序。"""
    docs = [
        {
            "symbol": "A.SH",
            "composite_score": {"balanced": 50, "aggressive": 90},
            "cycle_score": 1,
            "value_score": 1,
            "fundamental_score": 1,
            "growth_score": 1,
            "technical_score": 1,
            "money_flow_score": 1,
            "score_date": "20250918",
            "index_codes": ["csi500"],
        },
        {
            "symbol": "A.SH",
            "composite_score": {"balanced": 70, "aggressive": 20},
            "cycle_score": 1,
            "value_score": 1,
            "fundamental_score": 1,
            "growth_score": 1,
            "technical_score": 1,
            "money_flow_score": 1,
            "score_date": "20250920",
            "index_codes": ["csi500"],
        },
        {
            "symbol": "B.SH",
            "composite_score": {"balanced": 80, "aggressive": 30},
            "cycle_score": 1,
            "value_score": 1,
            "fundamental_score": 1,
            "growth_score": 1,
            "technical_score": 1,
            "money_flow_score": 1,
            "score_date": "20250920",
            "index_codes": ["csi500"],
        },
        {
            "symbol": "C.SH",
            "composite_score": {"balanced": 99},
            "cycle_score": 1,
            "value_score": 1,
            "fundamental_score": 1,
            "growth_score": 1,
            "technical_score": 1,
            "money_flow_score": 1,
            "score_date": "20250920",
            "index_codes": [],
        },
    ]
    info_docs = [
        {"symbol": "A.SH", "name": "A", "industry": "X"},
        {"symbol": "B.SH", "name": "B", "industry": "Y"},
    ]
    fake_db = FakeDB(stock_scores_docs=docs, stock_info_docs=info_docs)
    monkeypatch.setattr(stock_ranking, "get_db_collections", lambda: fake_db)
    monkeypatch.setattr(
        stock_ranking,
        "get_data_db",
        lambda: {"volume_price": FakeVolumePriceCollection([])},
    )
    client = TestClient(app)
    r_bal = client.get("/api/stock-rankings/by-index/csi500?strategy=balanced")
    assert r_bal.status_code == 200
    j = r_bal.json()
    assert j["total"] == 2
    assert [x["symbol"] for x in j["data"]] == ["B.SH", "A.SH"]
    assert j["data"][0]["composite_score"]["balanced"] == 80
    assert j["data"][0].get("return_since_score_pct") is None
    assert j["data"][0].get("prior_3m_return_pct") is None

    r_agg = client.get("/api/stock-rankings/by-index/csi500?strategy=aggressive")
    # A 最新 aggressive=20，B=30 → 降序 B 在前
    assert [x["symbol"] for x in r_agg.json()["data"]] == ["B.SH", "A.SH"]


@pytest.mark.unit
def test_by_index_rankings_fixed_date_and_paging(monkeypatch):
    docs = [
        {
            "symbol": "P.SH",
            "composite_score": {"balanced": 10},
            "cycle_score": 1,
            "value_score": 1,
            "fundamental_score": 1,
            "growth_score": 1,
            "technical_score": 1,
            "money_flow_score": 1,
            "score_date": "20250920",
            "index_codes": ["hs300"],
        },
        {
            "symbol": "Q.SH",
            "composite_score": {"balanced": 20},
            "cycle_score": 1,
            "value_score": 1,
            "fundamental_score": 1,
            "growth_score": 1,
            "technical_score": 1,
            "money_flow_score": 1,
            "score_date": "20250920",
            "index_codes": ["hs300"],
        },
        {
            "symbol": "R.SH",
            "composite_score": {"balanced": 30},
            "cycle_score": 1,
            "value_score": 1,
            "fundamental_score": 1,
            "growth_score": 1,
            "technical_score": 1,
            "money_flow_score": 1,
            "score_date": "20250920",
            "index_codes": ["hs300"],
        },
    ]
    info_docs = [
        {"symbol": "P.SH", "name": "P", "industry": "x"},
        {"symbol": "Q.SH", "name": "Q", "industry": "x"},
        {"symbol": "R.SH", "name": "R", "industry": "x"},
    ]
    fake_db = FakeDB(stock_scores_docs=docs, stock_info_docs=info_docs)
    monkeypatch.setattr(stock_ranking, "get_db_collections", lambda: fake_db)
    monkeypatch.setattr(
        stock_ranking,
        "get_data_db",
        lambda: {"volume_price": FakeVolumePriceCollection([])},
    )
    client = TestClient(app)
    r = client.get(
        "/api/stock-rankings/by-index/hs300?date=20250920&offset=1&page_size=1"
    )
    assert r.status_code == 200
    j = r.json()
    assert j["total"] == 3
    assert len(j["data"]) == 1
    assert j["data"][0]["symbol"] == "Q.SH"


@pytest.mark.unit
def test_by_index_rankings_empty_without_tag(monkeypatch):
    docs = [
        {
            "symbol": "Z.SH",
            "composite_score": {"balanced": 1},
            "cycle_score": 1,
            "value_score": 1,
            "fundamental_score": 1,
            "growth_score": 1,
            "technical_score": 1,
            "money_flow_score": 1,
            "score_date": "20250920",
        },
    ]
    fake_db = FakeDB(stock_scores_docs=docs, stock_info_docs=[])
    monkeypatch.setattr(stock_ranking, "get_db_collections", lambda: fake_db)
    monkeypatch.setattr(
        stock_ranking,
        "get_data_db",
        lambda: {"volume_price": FakeVolumePriceCollection([])},
    )
    client = TestClient(app)
    r = client.get("/api/stock-rankings/by-index/star50")
    assert r.status_code == 200
    j = r.json()
    assert j["total"] == 0
    assert j["data"] == []


@pytest.mark.unit
def test_by_index_invalid_code_400():
    client = TestClient(app)
    r = client.get("/api/stock-rankings/by-index/zztop")
    assert r.status_code == 400
