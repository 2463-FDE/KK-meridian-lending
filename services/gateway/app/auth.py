"""Session auth for the gateway.

Real-ish login: credentials are checked against the `users` table, a random opaque
token is minted and the session (user id / role / name) is stored in Redis with a TTL.
Subsequent requests present `Authorization: Bearer <token>`; the gateway resolves the
session and forwards the resolved identity downstream as `X-User-*` headers.

Caveats kept on purpose (brownfield): password hashes are unsalted sha256, tokens never
rotate, and the forwarded `X-User-Role` is still an authorization input downstream.

That third one was written when it was flatly true and no longer is, which is why it now
says less than it used to. Inbound `x-user-*` headers are stripped here before the pair is
re-set from the resolved session, every staff-gated route on origination/payment/kyc/
decision pairs the role with `X-Internal-Token`, and servicing's money routes call the
headers untrusted hints and verify a gateway-signed Ed25519 principal instead. What is
left is bounded rather than absent -- `docs/DEBT.md` **SEC-16** states the width, and the
register is where it is tracked, because a caveat in a docstring is invisible to planning.
"""
import hashlib
import json
import logging
import uuid

import redis

from . import db
from .config import REDIS_URL, SESSION_TTL_SECONDS

log = logging.getLogger("gateway.auth")

_redis = None

STAFF_ROLES = ("csr", "underwriter", "admin")
# Money-moving actions (adjust-balance/waive-fee/late-fee) are CSR/admin only --
# underwriter is staff but has no business changing a loan's balance or past-due.
MONEY_ROLES = ("csr", "admin")


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


def can_move_money(user: dict) -> bool:
    return user.get("role") in MONEY_ROLES


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


def account_is_current(user_id) -> bool:
    """Is this account still active, as of now?

    Deliberately a fresh read rather than anything cached. The whole value of
    this check is that it reflects a change made a moment ago; a cached answer
    to "is this account still allowed" is the defect, not an optimisation.

    Fails CLOSED. If the row is gone the account cannot be confirmed, and an
    unconfirmable account is not a current one.
    """
    rows = db.query("SELECT is_active FROM users WHERE id = %s", (user_id,))
    return bool(rows) and bool(rows[0]["is_active"])


def get_session(token: str) -> dict | None:
    """Resolve a bearer token to the account it belongs to, or None.

    WHY THIS RE-READS `users.is_active` (G-02). The session in Redis is a
    SNAPSHOT taken at login: it carries the id, role and name the account had
    then, and it lives for `SESSION_TTL_SECONDS` -- eight hours by default.
    Nothing re-checked the account behind it, so deactivating a staff member
    revoked nothing they were already holding. Measured on a running stack
    before this change, with `is_active` set false and the same bearer token
    reused: `POST /auth/login` correctly refused with 401, while `GET /auth/me`
    answered 200 and that same session went on to raise a balance-adjustment
    proposal, raise a fee waiver, APPROVE a pending movement (writing a real
    `ledger_entries` row), assess a late fee (another ledger row), record a
    reconciliation review disposition, and deny an application -- setting the
    adverse-action reason the applicant is told. Offboarding and compromise
    response were ineffective for up to eight hours.

    This is the boundary half of the fix, and it is here because this is the one
    funnel every path shares: `/auth/me`, the `X-User-*` pair `_proxy` forwards
    to every backend, and the Ed25519 principal the gateway mints for servicing
    all resolve identity through this function. One check covers them together
    instead of eight call sites, one of which would be forgotten. The database
    half -- what makes a money write safe against a deactivation racing it --
    is `resolve_pending_movement`.

    WHAT IT COSTS, stated rather than left to be discovered: one indexed
    primary-key read per authenticated request, on top of the Redis lookup
    already being made. That is the price of revocation actually taking effect,
    and the alternative is the behaviour measured above.

    The session is NOT deleted here. A deactivation can be reversed, and
    treating "not currently allowed" as "destroy the session" would also mean a
    brief database problem silently ending every session in progress -- failing
    closed in the wrong direction, unavailable rather than merely refused.
    """
    if not token:
        return None
    raw = _client().get(f"session:{token}")
    if not raw:
        return None
    user = json.loads(raw)
    if not account_is_current(user.get("id")):
        # Logged so an operator can tell a revoked account from an expired
        # session; the caller is told the same thing either way.
        log.warning("session presented for inactive account id=%s", user.get("id"))
        return None
    return user


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
