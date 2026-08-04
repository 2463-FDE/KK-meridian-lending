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

from . import config, db

_OUTCOME_LABEL = {"approve": "APPROVED", "deny": "DENIED"}


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
    if not raw_token or not secrets.compare_digest(row["accept_token_hash"], hash_accept_token(raw_token)):
        return False, 403, "not authorized to accept this offer"
    return True, 200, ""


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
