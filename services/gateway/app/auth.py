"""Session auth for the gateway.

Real-ish login: credentials are checked against the `users` table, a random opaque
token is minted and the session (user id / role / name) is stored in Redis with a TTL.
Subsequent requests present `Authorization: Bearer <token>`; the gateway resolves the
session and forwards the resolved identity downstream as `X-User-*` headers.

Caveats kept on purpose (brownfield): password hashes are unsalted sha256, tokens never
rotate, and downstream services trust the forwarded `X-User-Role` without re-checking it.
"""
import hashlib
import json
import uuid

import redis

from . import db
from .config import REDIS_URL, SESSION_TTL_SECONDS

_redis = None

STAFF_ROLES = ("csr", "underwriter", "admin")


def _client() -> "redis.Redis":
    global _redis
    if _redis is None:
        _redis = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    return _redis


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def authenticate(username: str, password: str) -> dict | None:
    rows = db.query(
        "SELECT id, username, role, display_name, applicant_id, password_hash, is_active "
        "FROM users WHERE username = %s",
        (username,),
    )
    if not rows:
        return None
    user = rows[0]
    if not user["is_active"]:
        return None
    if user["password_hash"] != hash_password(password):
        return None
    return {
        "id": user["id"],
        "username": user["username"],
        "role": user["role"],
        "name": user["display_name"],
        # Set for borrower logins only -- links this session to its owned
        # applications/loans for the ownership checks in main.py. None for
        # staff logins (csr/underwriter/admin never need it; is_staff() below
        # always takes precedence over an ownership check for them).
        "applicant_id": user["applicant_id"],
    }


def is_staff(user: dict) -> bool:
    return user.get("role") in STAFF_ROLES


def owns_loan(user: dict, loan_id) -> bool:
    """Does this (borrower) session's applicant own the given loan?

    A loan is boarded from an application (loans.app_id -> applications.id),
    and an application belongs to an applicant (applications.applicant_id) --
    the same applicant a borrower's session is tied to (users.applicant_id).
    Same shared Postgres instance every service already uses, so this is a
    plain join, not a cross-service call.
    """
    applicant_id = user.get("applicant_id")
    if not applicant_id:
        return False
    try:
        loan_id = int(loan_id)
    except (TypeError, ValueError):
        return False
    rows = db.query(
        "SELECT 1 FROM loans l JOIN applications a ON a.id = l.app_id "
        "WHERE l.id = %s AND a.applicant_id = %s",
        (loan_id, applicant_id),
    )
    return bool(rows)


def create_session(user: dict) -> str:
    token = uuid.uuid4().hex
    _client().setex(f"session:{token}", SESSION_TTL_SECONDS, json.dumps(user))
    return token


def get_session(token: str) -> dict | None:
    if not token:
        return None
    raw = _client().get(f"session:{token}")
    return json.loads(raw) if raw else None


def delete_session(token: str) -> None:
    if token:
        _client().delete(f"session:{token}")


def bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return authorization.strip()
