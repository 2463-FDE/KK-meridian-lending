"""Lazy Postgres connection helper (psycopg2)."""
import contextlib

import psycopg2
import psycopg2.extras
from .config import DATABASE_URL

_conn = None


def get_conn():
    """Open the connection on first use so importing the app needs no DB."""
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


@contextlib.contextmanager
def transaction():
    """A dedicated, non-autocommit connection for a single all-or-nothing unit
    of work (see routers/applications.py accept_offer). get_conn()'s shared
    connection above is autocommit -- every other call in this module is its
    own independent statement -- so a real multi-statement transaction needs
    its own connection rather than toggling that shared one out from under
    concurrent callers on other requests.
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
