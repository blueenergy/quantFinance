import re
import pytest
from fastapi.testclient import TestClient
from routers import admin
from main import app


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = docs or []

    def count_documents(self, query=None):
        if query is None or query == {}:
            return len(self.docs)
        # naive simple query handling
        if isinstance(query, dict) and '$gte' in str(query):
            return 0
        return len([d for d in self.docs if all(item in d.items() for item in query.items())])

    def find(self, query=None, sort=None):
        class Cursor(list):
            def sort(self, *args, **kwargs):
                return self

            def skip(self, n):
                return Cursor(list(self)[n:])

            def limit(self, n):
                return Cursor(list(self)[:n])

        return Cursor(self.docs)

    def find_one(self, query, sort=None):
        # return first doc matching symbol or id-like query
        for d in self.docs:
            match = True
            for k, v in query.items():
                if k == '_id':
                    # Accept direct string equality, or extract 24-hex substring from either side
                    ds = str(d.get('_id'))
                    vs = str(v)
                    m1 = re.search(r'([0-9a-fA-F]{24})', ds)
                    m2 = re.search(r'([0-9a-fA-F]{24})', vs)
                    if m1 and m2:
                        if m1.group(1) != m2.group(1):
                            match = False
                    else:
                        if ds != vs:
                            match = False
                elif d.get(k) != v:
                    match = False
            if match:
                return d
        return None


@pytest.mark.unit
def test_get_all_users_and_pagination(monkeypatch):
    # prepare fake users using ObjectId-like 24-char ids
    users = [{'_id': f"00000000000000000000000{i}", 'username': f'u{i}', 'created_at': i} for i in range(1, 6)]
    fake_collections = {
        'users': FakeCollection(users),
        'user_watchlists': FakeCollection([]),
        'login_logs': FakeCollection([]),
        'admin_logs': FakeCollection([]),
        'analysis_history': FakeCollection([])
    }

    # ensure admin functions that call get_db_collections() use our fakes
    monkeypatch.setattr(admin, 'get_db_collections', lambda: fake_collections)
    # also set module-level collections (some functions may use them)
    monkeypatch.setattr(admin, 'users_collection', fake_collections['users'])
    monkeypatch.setattr(admin, 'user_watchlists_collection', fake_collections['user_watchlists'])
    monkeypatch.setattr(admin, 'login_logs_collection', fake_collections['login_logs'])
    monkeypatch.setattr(admin, 'admin_logs_collection', fake_collections['admin_logs'])
    monkeypatch.setattr(admin, 'analysis_history_collection', fake_collections['analysis_history'])

    client = TestClient(app)
    # override admin auth dependency and noop the log_admin_action to avoid DB writes
    client.app.dependency_overrides[admin.get_current_admin_user] = lambda: {'id': 'admin1', 'username': 'admin'}
    monkeypatch.setattr(admin, 'log_admin_action', lambda *args, **kwargs: None)

    resp = client.get('/api/admin/users?page=1&page_size=2')
    assert resp.status_code == 200
    j = resp.json()
    assert j['success'] is True
    assert 'users' in j['data']
    assert j['data']['pagination']['page_size'] == 2

    client.app.dependency_overrides.pop(admin.get_current_admin_user, None)
    monkeypatch.setattr(admin, 'log_admin_action', lambda *args, **kwargs: None)


@pytest.mark.unit
def test_get_user_detail(monkeypatch):
    user = {'_id': '000000000000000000000012', 'username': 'bob', 'email': 'b@x.com'}
    watchlist = {'user_id': '000000000000000000000012', 'symbols': ['000001', '000002']}
    login_logs = [{'_id': 'l1', 'user_id': '000000000000000000000012', 'login_time': 1}, {'_id': 'l2', 'user_id': '000000000000000000000012', 'login_time': 2}]

    fake_collections = {
        'users': FakeCollection([user]),
        'user_watchlists': FakeCollection([watchlist]),
        'login_logs': FakeCollection(login_logs),
        'admin_logs': FakeCollection([]),
        'analysis_history': FakeCollection([])
    }

    # ensure admin functions use fakes via get_db_collections and module-level vars
    monkeypatch.setattr(admin, 'get_db_collections', lambda: fake_collections)
    monkeypatch.setattr(admin, 'users_collection', fake_collections['users'])
    monkeypatch.setattr(admin, 'user_watchlists_collection', fake_collections['user_watchlists'])
    monkeypatch.setattr(admin, 'login_logs_collection', fake_collections['login_logs'])
    monkeypatch.setattr(admin, 'admin_logs_collection', fake_collections['admin_logs'])
    monkeypatch.setattr(admin, 'analysis_history_collection', fake_collections['analysis_history'])

    client = TestClient(app)
    client.app.dependency_overrides[admin.get_current_admin_user] = lambda: {'id': 'admin1', 'username': 'admin'}
    monkeypatch.setattr(admin, 'log_admin_action', lambda *args, **kwargs: None)

    # request the exact ObjectId-like id we put in the fake user
    resp = client.get('/api/admin/users/000000000000000000000012')
    assert resp.status_code == 200
    j = resp.json()
    assert j['data']['user']['username'] == 'bob'
    assert j['data']['statistics']['watchlist_count'] == 2

    client.app.dependency_overrides.pop(admin.get_current_admin_user, None)
