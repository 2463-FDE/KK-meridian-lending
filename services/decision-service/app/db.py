"""Lazy Postgres connection helper (psycopg2)."""
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


def transaction(statements):
    """Run a list of (sql, params) statements as one all-or-nothing transaction.

    Commits only if every statement succeeds; rolls back and re-raises otherwise,
    so a partial failure never leaves some statements applied and others not. Used
    for decision.py's `decisions` + `decision_events` writes, which must land or
    fail together (review finding: writing them separately let a decision commit
    with no matching audit row when the second insert failed silently).
    """
    conn = get_conn()
    conn.autocommit = False
    try:
        results = []
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            for sql, params in statements:
                cur.execute(sql, params or ())
                results.append(cur.fetchall() if cur.description else [])
        conn.commit()
        return results
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.autocommit = True
