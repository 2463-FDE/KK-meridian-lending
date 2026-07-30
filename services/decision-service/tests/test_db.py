"""Tests for db.py's transaction isolation.

Review finding: transaction() used to call get_conn(), the same process-global
connection query() also shares, and toggle its autocommit flag for the
duration. Postgres transaction state (BEGIN/COMMIT/ROLLBACK) is a property of
the connection, not the calling thread -- two concurrent requests (FastAPI
sync routes run in a threadpool) sharing that one connection could have one
request's rollback erase another request's not-yet-committed insert, silently
corrupting the exact audit trail this PR exists to build.

No live Postgres is available in this test environment (consistent with the
rest of this service's test suite), so these tests prove the fix structurally:
concurrent transaction() calls get genuinely independent connection objects,
which makes the shared-state bug impossible regardless of timing, rather than
relying on luck. A live-DB concurrency test (two real simultaneous decisions,
asserting neither undoes the other's row) would be a valuable addition given
a real Postgres to run against, but is out of reach of this unit-test suite.
"""
import threading

import pytest

from app import db


class _FakeConnection:
    """Stands in for a real psycopg2 connection -- tracks what happened to
    THIS specific instance, so two concurrent transaction() calls sharing one
    fake connection (the bug) vs. getting separate ones (the fix) is directly
    observable."""

    def __init__(self):
        self.committed = False
        self.rolled_back = False
        self.closed = False
        self.executed = []

    def cursor(self, cursor_factory=None):
        return _FakeCursor(self)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class _FakeCursor:
    def __init__(self, conn: _FakeConnection):
        self.conn = conn
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))


def test_transaction_opens_its_own_connection_not_the_shared_one(monkeypatch):
    """transaction() must never touch get_conn()'s shared connection -- that
    shared connection is exactly what let two callers' transaction state
    collide before this fix."""
    shared = _FakeConnection()
    monkeypatch.setattr(db, "get_conn", lambda: shared)

    dedicated = _FakeConnection()
    monkeypatch.setattr(db.psycopg2, "connect", lambda *a, **k: dedicated)

    db.transaction([("INSERT INTO x VALUES (%s)", (1,))])

    assert dedicated.committed is True
    assert dedicated.closed is True
    # The shared connection query() uses was never touched by transaction().
    assert shared.committed is False
    assert shared.executed == []


def test_concurrent_transactions_get_independent_connections(monkeypatch):
    """Two transaction() calls on different threads, one of which fails and
    rolls back, must not affect the other's connection at all -- proven by
    asserting they're genuinely different objects with independent state,
    not by hoping timing works out."""
    created = []
    lock = threading.Lock()

    def _fake_connect(*args, **kwargs):
        conn = _FakeConnection()
        with lock:
            created.append(conn)
        return conn

    monkeypatch.setattr(db.psycopg2, "connect", _fake_connect)

    outcomes = {}

    def _succeed(key):
        db.transaction([("INSERT INTO decisions (app_id, outcome) VALUES (%s, %s)", (1, "approve"))])
        outcomes[key] = "committed"

    def _fail(key):
        with pytest.raises(RuntimeError):
            db.transaction([("BOOM", None)])
        outcomes[key] = "rolled_back"

    # Force the "fail" thread's statement to actually raise inside execute().
    real_execute = _FakeCursor.execute

    def _maybe_boom(self, sql, params=None):
        if sql == "BOOM":
            raise RuntimeError("simulated failure")
        return real_execute(self, sql, params)

    monkeypatch.setattr(_FakeCursor, "execute", _maybe_boom)

    t1 = threading.Thread(target=_succeed, args=("ok",))
    t2 = threading.Thread(target=_fail, args=("bad",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert outcomes == {"ok": "committed", "bad": "rolled_back"}
    assert len(created) == 2
    assert created[0] is not created[1]

    committed_conns = [c for c in created if c.committed]
    rolled_back_conns = [c for c in created if c.rolled_back]
    assert len(committed_conns) == 1
    assert len(rolled_back_conns) == 1
    # The one that committed was never rolled back, and vice versa -- they
    # never shared state, so one's failure could not have touched the other's
    # outcome.
    assert committed_conns[0] is not rolled_back_conns[0]
    assert committed_conns[0].rolled_back is False
    assert rolled_back_conns[0].committed is False
