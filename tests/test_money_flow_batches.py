import types
import pytest
from pymongo.errors import BulkWriteError, PyMongoError

from quantFinance.tools import money_flow_collector as mfc


class DummyResult:
    def __init__(self, upserted_ids=None, upserted_count=None, modified_count=0):
        self.upserted_ids = upserted_ids or {}
        self.upserted_count = upserted_count
        self.modified_count = modified_count


def test_bulk_write_in_batches_basic():
    calls = []
    def fake_bulk_write(batch, ordered=False):
        calls.append(len(batch))
        return DummyResult(upserted_ids={i: i for i in range(len(batch))}, modified_count=0)

    collection = types.SimpleNamespace(bulk_write=fake_bulk_write)
    ops = [object() for _ in range(2500)]
    upserted, modified = mfc.bulk_write_in_batches(collection, ops, batch_size=1000)
    assert calls == [1000, 1000, 500]
    assert upserted == 2500
    assert modified == 0


def test_bulk_write_in_batches_retry_then_success():
    attempts = {'n': 0}
    def fake_bulk_write(batch, ordered=False):
        attempts['n'] += 1
        if attempts['n'] < 3:
            raise BulkWriteError({'ok': 0, 'errmsg': 'temp fail'})
        return DummyResult(upserted_ids={0: 0}, modified_count=0)

    collection = types.SimpleNamespace(bulk_write=fake_bulk_write)
    upserted, modified = mfc.bulk_write_in_batches(collection, [object()], batch_size=1, max_retries=5, backoff_factor=0.01)
    assert attempts['n'] == 3
    assert upserted == 1
    assert modified == 0


def test_bulk_write_in_batches_retry_exhaustion():
    attempts = {'n': 0}
    def fake_bulk_write(batch, ordered=False):
        attempts['n'] += 1
        raise PyMongoError('network glitch')

    collection = types.SimpleNamespace(bulk_write=fake_bulk_write)
    with pytest.raises(PyMongoError):
        mfc.bulk_write_in_batches(collection, [object()], batch_size=1, max_retries=2, backoff_factor=0.01)
    # attempts should be max_retries + 1
    assert attempts['n'] == 3
