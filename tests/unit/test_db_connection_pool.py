"""Guard the Mongo connection pool bounds.

Regression: pymongo's default ``maxIdleTimeMS=None`` never reclaims an idle
connection, so this long-lived API process climbed to ``maxPoolSize`` and held
there until mongod ran out of file descriptors and refused every new connection
(2026-08-23). Reverting either bound to the pymongo default reopens that.
"""

import db


def test_client_reclaims_idle_connections():
    pool = db.client.options.pool_options
    assert pool.max_idle_time_seconds is not None
    assert pool.max_idle_time_seconds == db.MAX_IDLE_TIME_MS / 1000


def test_client_pool_size_is_bounded():
    pool = db.client.options.pool_options
    assert pool.max_pool_size == db.MAX_POOL_SIZE


def test_pool_defaults_reclaim_within_a_minute():
    # Defaults only; an env override is a deliberate ops decision.
    assert db.MAX_IDLE_TIME_MS <= 60000
    assert db.MAX_POOL_SIZE <= 50
