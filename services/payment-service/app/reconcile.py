"""Drain captured-but-unapplied payments (PR #8 review, high).

`charge()` authorizes the card first and only then asks servicing to move the
loan balance. When that second step fails, `_apply_via_servicing` records the
failure honestly -- `applied_at` stays NULL -- and returns False. What was
missing is anything that ever came back for those rows: `applied_at IS NULL`
appeared in no query in the repository. Recovery depended entirely on the
client retrying the same idempotency_key, so a borrower who closed the tab
after authorization left money captured and the balance uncredited, with no
retry, no alert, and nothing that would even list the affected payments.

This module is that missing half. It is deliberately a poll-and-drain loop over
the `payments` table rather than a separate queue: the row IS the outbox
record, written in the same transaction as the capture, so there is no window
where a payment exists but its work item does not.

Safety properties, in order of how much they matter:

  * At most one worker per row. A row is claimed by the same UPDATE that
    selects it (`FOR UPDATE SKIP LOCKED` in the sub-select, then pushing
    `apply_next_attempt_at` into the future), so two replicas polling at the
    same instant get disjoint sets.
  * Retrying is always safe. servicing-service's apply-payment is idempotent by
    `payment_id` (`payment_applications`, db/migrations/0013), so a retry after
    a lost response applies nothing twice.
  * A permanently broken row backs off instead of hammering servicing --
    exponential, capped -- and stops being retried after MAX_ATTEMPTS. It is
    NOT deleted or marked resolved: it stays visible to
    `unreconciled_summary()` precisely because a human needs to look at it.
  * Nothing here ever touches a declined ('failed') or already-applied row.

What this is not: it does not void or reverse an authorization. Voiding is the
other half of the reviewer's suggestion and needs a processor that supports it;
`processor.py` has no void call, and inventing one against a stub would be
pretending to a capability this system does not have.
"""
from .config import RECONCILE_BACKOFF_CAP_SECONDS, RECONCILE_MAX_ATTEMPTS
from . import db
from .logging_config import get_logger

log = get_logger("reconcile")

# Claim + schedule the next attempt in one statement. The sub-select is what
# makes this safe under concurrency; the UPDATE is what makes the claim
# durable if this process dies mid-apply (the row simply comes due again).
_CLAIM_SQL = """
UPDATE payments
   SET apply_attempts = apply_attempts + 1,
       apply_next_attempt_at = now() + (least(power(2, apply_attempts)::int, %s) || ' seconds')::interval
 WHERE id IN (
     SELECT id FROM payments
      WHERE auth_status = 'captured'
        AND applied_at IS NULL
        AND apply_attempts < %s
        AND (apply_next_attempt_at IS NULL OR apply_next_attempt_at <= now())
      ORDER BY id
      LIMIT %s
      FOR UPDATE SKIP LOCKED
 )
RETURNING id, loan_id, amount, apply_attempts, correlation_id
"""


def claim_due(limit: int = 20):
    """Claim up to `limit` payments that are due for an apply retry."""
    return db.query(_CLAIM_SQL, (RECONCILE_BACKOFF_CAP_SECONDS, RECONCILE_MAX_ATTEMPTS, limit))


def reconcile_once(limit: int = 20) -> dict:
    """One pass. Returns counts -- the caller decides whether to log or export."""
    # Imported here, not at module scope: payments.py imports nothing from this
    # module, and keeping the dependency one-directional avoids a cycle.
    from .payments import _apply_via_servicing

    claimed = claim_due(limit)
    applied = 0
    for row in claimed:
        # The row's OWN correlation id, carried forward. This drain runs long
        # after the capture, so it is the likeliest place for a trace to be
        # dropped or a fresh id invented -- and either would split one payment's
        # evidence in two while every individual log line still looked correct.
        if _apply_via_servicing(row["loan_id"], row["amount"], row["id"],
                                row.get("correlation_id")):
            applied += 1
            log.info(
                "reconciled captured payment correlation_id=%s payment_id=%s "
                "loan_id=%s attempt=%s",
                row.get("correlation_id"), row["id"], row["loan_id"],
                row["apply_attempts"],
            )
        elif row["apply_attempts"] >= RECONCILE_MAX_ATTEMPTS:
            # Out of automatic retries. Left in place and still reported by
            # unreconciled_summary() -- money was captured and the balance was
            # never credited, so this needs a human, not a silent give-up.
            log.error(
                "payment still unapplied after %s attempts, giving up on automatic "
                "retry payment_id=%s loan_id=%s -- needs manual reconciliation",
                row["apply_attempts"], row["id"], row["loan_id"],
            )
    return {"claimed": len(claimed), "applied": applied,
            "still_pending": len(claimed) - applied}


def unreconciled_summary() -> dict:
    """What an operator (or an alert) needs to see: how much money is captured
    and not credited, how old the oldest one is, and how many have exhausted
    their automatic retries."""
    rows = db.query(
        "SELECT count(*)::int AS pending, "
        "       coalesce(sum(amount), 0) AS amount_pending, "
        "       count(*) FILTER (WHERE apply_attempts >= %s)::int AS exhausted, "
        "       min(created_at) AS oldest_created_at "
        "  FROM payments "
        " WHERE auth_status = 'captured' AND applied_at IS NULL",
        (RECONCILE_MAX_ATTEMPTS,),
    )
    r = rows[0]
    return {
        "pending": r["pending"],
        "amount_pending": float(r["amount_pending"] or 0),
        "exhausted": r["exhausted"],
        "oldest_created_at": r["oldest_created_at"].isoformat() if r["oldest_created_at"] else None,
    }
