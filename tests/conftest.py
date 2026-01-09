import sys
import os
import re
from pathlib import Path

# Ensure the package root is importable when pytest runs from repo root
REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = REPO_ROOT / 'quantFinance'
if str(PACKAGE_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGE_DIR))

# Ensure test-run code paths use the in-memory DB fallback to avoid requiring
# a running MongoDB during unit tests. Tests opt into this behavior explicitly.
os.environ.setdefault('USE_IN_MEMORY_DB', '1')
os.environ.setdefault('TESTING', 'true')

import pytest
from fastapi.testclient import TestClient

# Shared FakeCollection and Cursor helpers used across unit tests
class Cursor(list):
    def sort(self, *args, **kwargs):
        return self

    def skip(self, n):
        return Cursor(list(self)[n:])

    def limit(self, n):
        return Cursor(list(self)[:n])


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = docs or []

    def count_documents(self, query=None):
        if query is None or query == {}:
            return len(self.docs)
        return len([d for d in self.docs if all(item in d.items() for item in query.items())])

    def find(self, query=None, projection=None):
        if query is None or query == {}:
            return Cursor(self.docs)
        # Filter documents matching query
        filtered = [d for d in self.docs if all(d.get(k) == v for k, v in query.items())]
        return Cursor(filtered)

    def distinct(self, field, query=None):
        """Return unique values for a field, optionally filtered by query."""
        if query is None or query == {}:
            docs = self.docs
        else:
            docs = [d for d in self.docs if all(d.get(k) == v for k, v in query.items())]
        
        values = [d.get(field) for d in docs if field in d]
        return list(set(values))  # Return unique values

    def aggregate(self, pipeline):
        # Default: return docs as-is; tests may override with monkeypatch if needed
        return self.docs

    def find_one(self, query, sort=None):
        for d in self.docs:
            match = True
            for k, v in query.items():
                if k == '_id':
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


@pytest.fixture
def fake_collections():
    """Return a fresh dict-like container of common collections used in tests."""
    return {
        'stock_scores': FakeCollection([]),
        'stock_info': FakeCollection([]),
        'volume_price': FakeCollection([]),
        'money_flow': FakeCollection([]),
        'users': FakeCollection([]),
        'user_watchlists': FakeCollection([]),
        'login_logs': FakeCollection([]),
        'admin_logs': FakeCollection([]),
        'analysis_history': FakeCollection([]),
    }


@pytest.fixture
def client():
    """Provide a TestClient with a convenience to override dependencies in tests."""
    from main import app
    c = TestClient(app)
    yield c
    # cleanup any dependency overrides set by tests
    try:
        c.app.dependency_overrides.clear()
    except Exception:
        pass


@pytest.fixture
def admin_auth_override():
    """Return a callable that can be used to set admin auth override on a TestClient: client.app.dependency_overrides[admin.get_current_admin_user] = admin_auth_override()"""
    return lambda: {'id': 'admin1', 'username': 'admin'}

