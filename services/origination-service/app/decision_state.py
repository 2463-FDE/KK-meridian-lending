"""Shared helpers for resolving an application's current decision state.

Used by run_decision's rerun-block message, review_application's
already-decided message, and offers.py's approve-gated actions (make_offer,
accept_offer) -- all three need the same "what did staff (or the model)
actually decide, and why" lookup, just formatted into different
user-facing messages.

Also the SOLE place accept_token lifecycle rules live (issue/revoke/verify)
-- security fix, audit finding: run_decision used to mint a token on
approve but never revoked one on a rerun that flipped the outcome away
from approve, while review_application already did revoke it in that
case. Two endpoints independently implementing "when is this token valid"
is exactly how they drift; centralizing it here means both paths call the
same three functions and cannot diverge again.
"""
import hashlib
import secrets

from fastapi import HTTPException

from . import config, db

_OUTCOME_LABEL = {"approve": "APPROVED", "deny": "DENIED"}

# PR #6 review (Finding 2): decision-attempt lifecycle. A blocked decision
# rerun used to still perform a real bureau pull and durably write its own
# decision_events row before losing the finality race against a staff
# decision or funding -- this is the fix: an explicit attempt/reservation,
# created (short transaction, applications row locked) BEFORE decision-
# service is ever called, and only turned into a permanent decisions +
# decision_events row (a second short transaction, locked again) if it
# still wins after the external call returns. See
# db/migrations/0023_decision_attempts.sql and routers/applications.py::
# run_decision.

# Enforced again at the database (decision_attempts_failure_code_allowed,
# db/migrations/0023) -- checked here too so a typo fails at insert time
# with a clear Python exception instead of a raw IntegrityError.
FAILURE_CODES = frozenset({
    "timeout", "unavailable", "invalid_response", "superseded_by_staff",
    "funded", "internal_error", "expired_lease", "persistence_error",
})

# Matches decision_attempts.failure_detail's VARCHAR(200) column.
MAX_FAILURE_DETAIL_LEN = 200

# Bounded, templated text only -- never a raw exception, stack trace, HTTP
# response body, bureau response, credential, or applicant field. Every
# value here is well under MAX_FAILURE_DETAIL_LEN by construction.
_FAILURE_DETAIL = {
    "timeout": "external call to decision-service exceeded its configured timeout",
    "unavailable": "decision-service was unreachable (connection error)",
    "invalid_response": "decision-service response did not match the active attempt",
    "superseded_by_staff": "a manual review was recorded before this attempt reached persistence",
    "funded": "the application was funded before this attempt reached persistence",
    "internal_error": "an unexpected error occurred finalizing this attempt",
    "expired_lease": "lease exceeded with no completion; treated as abandoned",
    "persistence_error": "TXN B failed to persist the decision; rolled back and released for retry",
}


def sanitize_failure_detail(failure_code: str) -> str:
    """The ONLY text ever written to decision_attempts.failure_detail --
    always one of the fixed templates above, truncated defensively to
    MAX_FAILURE_DETAIL_LEN. Never interpolates an exception message,
    response body, or any caller-supplied value."""
    if failure_code not in FAILURE_CODES:
        failure_code = "internal_error"
    return _FAILURE_DETAIL[failure_code][:MAX_FAILURE_DETAIL_LEN]


def recheck_finality_locked(cur, app_id: int) -> tuple[bool, dict | None]:
    """Must be called with `cur` already holding this app_id's row lock
    (SELECT ... FOR UPDATE issued by the caller's own transaction, or
    issued here -- see callers). Returns (funded, manual_review_or_None).
    Shared by start_decision_attempt (TXN A, before decision-service is
    ever called) and run_decision's post-call recheck (TXN B) -- both need
    the exact same authoritative check, and must not drift independently."""
    cur.execute("SELECT status FROM applications WHERE id = %s FOR UPDATE", (app_id,))
    rows = cur.fetchall()
    if not rows:
        raise HTTPException(status_code=404, detail="application not found")
    funded = rows[0]["status"] == "funded"
    cur.execute(
        "SELECT outcome, reason, reviewer_name, reviewer_role, reviewed_at "
        "FROM manual_reviews WHERE app_id = %s",
        (app_id,),
    )
    manual_rows = cur.fetchall()
    return funded, (manual_rows[0] if manual_rows else None)


def verify_attempt_still_active_locked(cur, attempt_id: int) -> bool:
    """Must run inside TXN B, BEFORE persisting anything, on a cursor
    already inside a transaction (locks this one attempt row by its own
    primary key). Proves the attempt this response belongs to is still the
    live, active reservation for its application -- not just that finality
    hasn't landed.

    Three things must hold, matching the required invariant exactly:
      - state is still 'in_progress' (not already 'expired'/'discarded'/
        'failed' by a concurrent recovery -- since only one attempt may be
        'in_progress' per app at a time, this alone proves it's still THE
        active attempt, not a superseded one);
      - the lease has not passed. Recovery is lazy (only the NEXT request
        to touch this app_id reclaims a stale attempt -- see
        start_decision_attempt), so a slow/delayed response for THIS exact
        attempt can arrive after its own lease expired but before anyone
        else ever raced it. If that's discovered here, this attempt
        self-expires (marks itself 'expired', failure_code=
        'expired_lease') rather than being allowed to persist late.

    Returns False (never persist) if the row is missing, already terminal,
    or found to be lease-expired. Returns True only if the caller may
    safely proceed to write decisions/decision_events under this attempt."""
    cur.execute(
        "SELECT state, (lease_expires_at > now()) AS live "
        "FROM decision_attempts WHERE id = %s FOR UPDATE",
        (attempt_id,),
    )
    rows = cur.fetchall()
    if not rows:
        return False
    if rows[0]["state"] != "in_progress":
        return False
    if not rows[0]["live"]:
        # Discovered here, first -- nobody else has raced this attempt yet,
        # but its lease has already passed. Self-expire rather than persist
        # a late result under a reservation that's no longer valid.
        cur.execute(
            "UPDATE decision_attempts SET state = 'expired', completed_at = now(), "
            "failure_code = 'expired_lease', failure_detail = %s "
            "WHERE id = %s AND state = 'in_progress'",
            (sanitize_failure_detail("expired_lease"), attempt_id),
        )
        return False
    return True


def _bureau_request_key_for(cur, app_id: int) -> str:
    """The idempotency key this attempt should present at the bureau boundary.

    PR #6 review (Gap A). Origination cannot tell "the bureau never ran" from
    "the bureau ran and we lost the response" when its own HTTP client times
    out -- so a retry after that ambiguous outcome must reuse the SAME key,
    letting the provider return the original operation instead of performing a
    second billable hard inquiry.

    Reuse is deliberately narrow: ONLY when the immediately preceding attempt
    for this application ended in exactly that ambiguous state
    (state='failed', failure_code='timeout'). Every other predecessor --
    completed, discarded, expired, or failed for any other reason -- means
    this is a genuinely NEW decision request, which mints a fresh key and
    performs a real new pull. That boundary is what keeps this an idempotency
    key and not a credit-data cache: a staff rerun can never be served a
    stale score.

    Must run on a cursor already holding this application's row lock (see
    start_decision_attempt), so two concurrent callers cannot both inherit
    the same key and then diverge.
    """
    cur.execute(
        "SELECT state, failure_code, bureau_request_key FROM decision_attempts "
        "WHERE app_id = %s ORDER BY id DESC LIMIT 1",
        (app_id,),
    )
    rows = cur.fetchall()
    if rows:
        prev = rows[0]
        if (
            prev["state"] == "failed"
            and prev["failure_code"] == "timeout"
            and prev["bureau_request_key"]
        ):
            return prev["bureau_request_key"]
    return secrets.token_urlsafe(24)


def start_decision_attempt(app_id: int, requested_by: str) -> tuple[int, str]:
    """TXN A: lock the application, recheck funded/manual finality, atomically
    recover a stale (lease-expired) attempt if one exists, then create a
    fresh 'in_progress' attempt -- all in one short transaction, committed
    (lock released) BEFORE the caller ever makes the slow call to
    decision-service. No background worker: recovery only ever happens as
    a side effect of a later request reaching this same code path.

    `requested_by` is a role string only ('borrower' | 'csr' | 'underwriter'
    | 'admin') -- sourced server-side from the same staff/ownership check
    run_decision already performs, never from unvalidated client input.

    Returns (attempt_id, bureau_request_key) -- see _bureau_request_key_for
    for when the key is inherited from a timed-out predecessor rather than
    freshly minted.

    Raises HTTPException (404/422/409) if blocked: application missing,
    already funded, already manually decided, or another attempt is still
    genuinely live (lease not yet expired)."""
    with db.transaction() as cur:
        funded, manual = recheck_finality_locked(cur, app_id)
        if funded:
            raise HTTPException(
                status_code=422,
                detail="cannot rerun a decision on an already-funded application",
            )
        if manual:
            raise HTTPException(status_code=409, detail=format_rerun_blocked_message(manual))

        cur.execute(
            "SELECT id, (lease_expires_at > now()) AS live "
            "FROM decision_attempts WHERE app_id = %s AND state = 'in_progress' "
            "FOR UPDATE",
            (app_id,),
        )
        existing = cur.fetchall()
        if existing:
            if existing[0]["live"]:
                raise HTTPException(
                    status_code=409,
                    detail="a decision is already in progress for this application",
                )
            # Stale lease: the process that created this attempt never
            # completed it (crash, kill, deploy) -- atomically terminalize
            # it as 'expired' and fall through to create a fresh attempt,
            # all under the same lock a concurrent request would also need.
            cur.execute(
                "UPDATE decision_attempts SET state = 'expired', completed_at = now(), "
                "failure_code = 'expired_lease', failure_detail = %s WHERE id = %s",
                (sanitize_failure_detail("expired_lease"), existing[0]["id"]),
            )

        bureau_request_key = _bureau_request_key_for(cur, app_id)
        cur.execute(
            "INSERT INTO decision_attempts "
            "(app_id, state, requested_by, lease_expires_at, bureau_request_key) "
            "VALUES (%s, 'in_progress', %s, now() + (%s || ' seconds')::interval, %s) "
            "RETURNING id",
            (app_id, requested_by, config.DECISION_ATTEMPT_LEASE_SECONDS, bureau_request_key),
        )
        return cur.fetchall()[0]["id"], bureau_request_key


def mark_attempt_failed(attempt_id: int, failure_code: str) -> None:
    """Standalone short update for when the call to decision-service itself
    fails (timeout/connection error) or its response doesn't match the
    active attempt -- releases the 'in_progress' slot so a retry can create
    a fresh attempt. No applications-row lock needed: this only ever
    touches the ONE attempt row by its own primary key, which no other
    writer races (every writer of this row goes through
    start_decision_attempt's or run_decision's own applications-row lock
    first)."""
    if failure_code not in FAILURE_CODES:
        failure_code = "internal_error"
    with db.transaction() as cur:
        cur.execute(
            "UPDATE decision_attempts SET state = 'failed', completed_at = now(), "
            "failure_code = %s, failure_detail = %s "
            "WHERE id = %s AND state = 'in_progress'",
            (failure_code, sanitize_failure_detail(failure_code), attempt_id),
        )


def hash_accept_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


def issue_accept_token(cur, app_id: int) -> str:
    """Mint a fresh accept_token for a just-approved application. Called
    from inside the SAME transaction that just wrote the approve outcome
    (run_decision / review_application), on the applications row already
    locked (FOR UPDATE) by that transaction.

    Only the sha256 hash is ever stored -- the raw value is returned once,
    to be handed straight to the borrower's own browser (DecisionOut),
    never logged, never persisted in the clear. Expiry is computed by
    Postgres's own now() (server clock), not Python's, so a clock-skewed
    app host can never mint a token that looks valid/invalid to the wrong
    side of the check.
    """
    raw = secrets.token_urlsafe(32)
    cur.execute(
        "UPDATE applications SET accept_token_hash = %s, "
        "accept_token_expires_at = now() + (%s || ' seconds')::interval, "
        "accept_token_consumed_at = NULL "
        "WHERE id = %s",
        (hash_accept_token(raw), config.ACCEPT_TOKEN_TTL_SECONDS, app_id),
    )
    return raw


def revoke_accept_token(cur, app_id: int) -> None:
    """Invalidate any live accept_token on this application. Called
    whenever an automated or manual outcome becomes anything other than
    approve (rerun to deny/refer, staff correction to deny), in the same
    transaction as that outcome change -- an application that is no longer
    approved must never have a working accept link, full stop."""
    cur.execute(
        "UPDATE applications SET accept_token_hash = NULL, "
        "accept_token_expires_at = NULL WHERE id = %s",
        (app_id,),
    )


def verify_accept_token(row: dict, raw_token: str | None) -> tuple[bool, int, str]:
    """Constant-time check of a borrower-supplied raw token against the
    locked applications row's stored hash, expiry, and consumption state.

    `row` must come from a query executed on the SAME locked (FOR UPDATE)
    connection/transaction as the eventual board, and must include
    accept_token_hash, accept_token_consumed_at, and token_live (a boolean
    computed by the caller's own SQL as
    "accept_token_expires_at IS NOT NULL AND accept_token_expires_at > now()"
    -- evaluated by Postgres's own clock, never Python's).

    Returns (ok, status_code, message) instead of a bare bool so the caller
    can surface the SPECIFIC reason ("already used" vs "expired" vs "wrong
    token") -- these are different situations for the borrower with
    different fixes, not one generic 403.
    """
    if row.get("accept_token_consumed_at") is not None:
        return False, 409, "This acceptance link has already been used."
    if not row.get("accept_token_hash") or not row.get("token_live"):
        return False, 409, (
            "This acceptance link has expired. Request a new decision to "
            "get a new one."
        )
    if not accept_token_hash_matches(row["accept_token_hash"], raw_token):
        return False, 403, "not authorized to accept this offer"
    return True, 200, ""


def accept_token_hash_matches(stored_hash: str | None, raw_token: str | None) -> bool:
    """Constant-time hash comparison only -- no expiry/consumed-state check.

    Used where the only thing that needs proving is "you hold the token
    this application minted" (e.g. viewing an already-created offer before
    accepting it), as opposed to verify_accept_token's full accept/board
    gate (expiry + single-use). A consumed or expired token still proves
    the caller is the legitimate borrower for read-only purposes; nothing
    is written by a read.
    """
    if not stored_hash or not raw_token:
        return False
    return secrets.compare_digest(stored_hash, hash_accept_token(raw_token))


def format_outcome_label(outcome: str) -> str:
    return _OUTCOME_LABEL.get(outcome, outcome.upper())


def format_rerun_blocked_message(manual: dict) -> str:
    """The message run_decision returns (409) when a final manual decision
    already exists -- shared by the pre-call and post-call checks so both
    say exactly the same thing."""
    label = format_outcome_label(manual["outcome"])
    reviewed_at = manual["reviewed_at"]
    when = reviewed_at.isoformat() if hasattr(reviewed_at, "isoformat") else str(reviewed_at)
    name = manual.get("reviewer_name") or manual.get("reviewer_role") or "a staff member"
    return (
        f"This application was manually {label} by {name} on {when}. "
        f"Reason: {manual['reason']}. The automated decision cannot be "
        "rerun because it would overwrite the final staff decision."
    )


def get_manual_review(app_id: int) -> dict | None:
    """The final staff decision on this application, if one has been
    recorded (manual_reviews.app_id is UNIQUE, db/migrations/0020 -- at most
    one row, ever)."""
    rows = db.query(
        "SELECT outcome, reason, reviewer_name, reviewer_role, reviewed_at "
        "FROM manual_reviews WHERE app_id = %s",
        (app_id,),
    )
    return rows[0] if rows else None


def get_deny_reason(app_id: int) -> str | None:
    """Best-effort human-readable reason for a 'deny' outcome -- staff's own
    reason if this was manually decided, else the automated model's stored
    reason (decision_events.reason_codes, Week 3's audit trail). Returns
    None if neither has a reason on record (should not normally happen for
    a real deny, but never fabricate one if it does)."""
    manual = get_manual_review(app_id)
    if manual and manual["outcome"] == "deny":
        return manual["reason"]
    events = db.query(
        "SELECT reason_codes FROM decision_events WHERE app_id = %s "
        "ORDER BY id DESC LIMIT 1",
        (app_id,),
    )
    if events and events[0]["reason_codes"]:
        return events[0]["reason_codes"][0]
    return None
