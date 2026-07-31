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
    """A dedicated, non-autocommit connection for one all-or-nothing unit of
    work (balance.apply_payment_once()'s idempotency marker INSERT and the
    balance UPDATE it guards).

    Review fix: this used to toggle autocommit off on the shared module-level
    _conn that every query() call also uses. Under a threaded server, a
    different request's query() could land on that same connection while a
    transaction was in flight and get committed or rolled back along with it
    -- real money state at risk. Opens its own connection instead, same
    pattern as origination-service's db.py: every statement in the
    transaction must run through the yielded cursor directly, not through
    query()/get_conn(), or it silently executes on the shared connection
    again, outside this transaction.
    """
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
