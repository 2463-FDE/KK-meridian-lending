"""Lazy Postgres connection helper (psycopg2)."""
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
from .config import DATABASE_URL

_conn = None


def get_conn():
    global _conn
    if _conn is None or _conn.closed:
        _conn = psycopg2.connect(DATABASE_URL)
        _conn.autocommit = True
    return _conn


def query(sql, params=None):
    conn = get_conn()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params or ())
        if cur.description:
            return cur.fetchall()
        return []


@contextmanager
def transaction():
    """Run a block of query() calls as one atomic transaction.

    Review fix: every query() call above autocommits on its own -- fine for a
    single statement, but balance.apply_payment_once() needs its idempotency
    marker INSERT and the balance UPDATE it guards to commit or fail together.
    Without this, a balance-update failure after the marker had already landed
    left a permanent marker with no balance ever applied: every retry hit the
    ON CONFLICT path and silently skipped the apply, forever. query() calls
    made inside this block share the same connection with autocommit off, so
    an exception rolls back everything (marker included) instead of leaving a
    committed marker with no corresponding balance change.
    """
    conn = get_conn()
    conn.autocommit = False
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.autocommit = True
