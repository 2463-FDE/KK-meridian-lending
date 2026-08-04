"""Shared helpers for resolving an application's current decision state.

Used by run_decision's rerun-block message, review_application's
already-decided message, and offers.py's approve-gated actions (make_offer,
accept_offer) -- all three need the same "what did staff (or the model)
actually decide, and why" lookup, just formatted into different
user-facing messages.
"""
from . import db

_OUTCOME_LABEL = {"approve": "APPROVED", "deny": "DENIED"}


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
