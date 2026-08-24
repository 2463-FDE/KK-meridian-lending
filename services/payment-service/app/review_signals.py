"""Flag payments for human reconciliation review. Nothing here moves money.

**Authority.** Client decision, 2026-08-24, which replaced the deferral recorded
in `docs/DEBT.md` D22:

    "Flag qualifying payments for human reconciliation review. Do not treat the
     flag as a duplicate or validity conclusion or as permission to move money."

That sentence decides everything in this module. A row written here is a
CANDIDATE FOR A HUMAN TO LOOK AT. It is not a duplicate, not a finding, not a
validity judgement, and not an instruction. No function here blocks, reverses,
refunds, reallocates, re-authorises or re-applies anything, and none of them can:
this module writes to `reconciliation_review_items` and reads `payments`, and it
touches no other table.

**Two signals, kept apart, because they carry different weight.**

1. **Exact.** The same provider transaction reference, or the same idempotency
   key, seen again -- *regardless of elapsed time*. Strong evidence, and still
   only evidence.
2. **Heuristic.** Same loan, same amount, same payment source, same channel,
   inside a rolling 30 minutes. All four, plus the window. Same loan and same
   amount alone must never flag: that is what a second legitimate installment
   looks like, and flagging it is how a review queue teaches operators to stop
   reading it.

**The money controls stay exactly as they are.** `payments.idempotency_key` has a
partial unique index and `charge()` inserts with `ON CONFLICT DO NOTHING`;
`payments.processor_ref` has a partial unique index. A repeated attempt is
therefore *already* refused or replayed without a second money movement, and this
module observes that refusal rather than relaxing it. Nothing here creates a
second `payments` row so that a signal has something to point at -- a review
queue that weakened idempotency to fill itself would be worse than no queue.

**Privacy.** A row records the two payment ids, the loan, the signal, and the
payment's own `correlation_id` as a non-identifying reference. Not the amount, not
the last4, not the brand, not the cardholder, not the source handle: a reviewer
reads those from the payment itself inside an authenticated surface, and a review
queue is exactly the kind of table that gets exported to a spreadsheet. The
counters this module emits carry signal type only.
"""
import datetime
from decimal import Decimal

from . import db
from .logging_config import get_logger

log = get_logger("payment.review")

#: The rolling window, from the client's decision. Thirty minutes, not "about
#: half an hour": the boundary is tested at 29:59, 30:00 and 30:01.
WINDOW = datetime.timedelta(minutes=30)

#: **Inclusive at exactly 30:00.** The client said "within a rolling 30-minute
#: window", and a payment exactly thirty minutes later is within thirty minutes
#: rather than outside it. Stated here because a boundary nobody wrote down gets
#: decided twice -- once in the query and once in the test -- and the two
#: disagree. `<=` is the whole of the decision.
WINDOW_IS_INCLUSIVE = True

SIGNAL_EXACT_PROVIDER_REF = "exact_provider_transaction_id"
SIGNAL_EXACT_IDEMPOTENCY_KEY = "exact_idempotency_key"
SIGNAL_HEURISTIC_WINDOW = "heuristic_30_minute_candidate"

QUEUE_RECONCILIATION_REVIEW = "reconciliation_review"


def _as_decimal(amount) -> Decimal:
    """Money compared as Decimal, never as float.

    Two captures of 410.50 must compare equal, and `0.1 + 0.2 != 0.3` is the
    reason this repository moved its money columns to NUMERIC (D12). A heuristic
    that missed a match because of binary float would be a silent false negative
    in a control whose whole job is noticing a resemblance.
    """
    return Decimal(str(amount)).quantize(Decimal("0.01"))


def is_within_window(earlier, later, *, window: datetime.timedelta = WINDOW) -> bool:
    """Is `later` within `window` of `earlier`, inclusive at the boundary?

    Both are capture times, not row-insert times -- see `heuristic_candidates`.
    An unknown time is not a match: an authorization that never confirmed has no
    capture instant, and treating a missing timestamp as "now" would make the
    window depend on when the query ran.
    """
    if earlier is None or later is None:
        return False
    gap = abs(later - earlier)
    return gap <= window if WINDOW_IS_INCLUSIVE else gap < window


def heuristic_matches(candidate: dict, other: dict) -> bool:
    """Do two captures match on all four factors the client named?

    Pure, so the predicate can be tested exhaustively without a database, and so
    the rule reads as the client wrote it rather than as a WHERE clause.

    A missing `source_ref` on either side is NOT a match. The client's rule
    requires the source to be the same, and this system cannot always prove it --
    an ACH payment has no tokenizer, and a capture written before
    `db/migrations/0044` has no handle. Insufficient evidence means no signal;
    degrading to loan + amount + channel would flag the legitimate second
    installment the rule exists to protect.
    """
    if candidate.get("id") == other.get("id"):
        return False                      # a payment is not its own candidate
    if candidate.get("loan_id") != other.get("loan_id"):
        return False
    if _as_decimal(candidate["amount"]) != _as_decimal(other["amount"]):
        return False

    source = candidate.get("source_ref")
    if not source or source != other.get("source_ref"):
        return False                      # unknown is not a match

    if candidate.get("method") != other.get("method"):
        return False                      # channel: card is not ach

    return is_within_window(other.get("captured_at"), candidate.get("captured_at"))


def _record(signal_type: str, *, payment_id: int, loan_id: int,
            related_payment_id: int | None, correlation_ref: str | None) -> int | None:
    """Write one review item, or leave the existing one alone.

    `ON CONFLICT DO NOTHING` against the `(payment_id, signal_type)` unique
    constraint is the flood guard the client's "do not flood the queue" concern
    needs: a caller retrying the same request produces the same observation, and
    an operator should see one thing to look at, not one per retry.

    Failure is logged and swallowed **deliberately**, and this is the most
    important line in the module. Recording a review item must never fail a
    payment: the money path is authoritative, the queue is an observation about
    it, and a review-queue outage that refused captures would convert a
    reporting feature into an availability incident on the money path. The
    counterpart guarantee is that nothing downstream trusts this return value.
    """
    try:
        rows = db.query(
            "INSERT INTO reconciliation_review_items "
            "(signal_type, payment_id, related_payment_id, loan_id, correlation_ref, queue) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (payment_id, signal_type) DO NOTHING RETURNING id",
            (signal_type, payment_id, related_payment_id, loan_id,
             correlation_ref, QUEUE_RECONCILIATION_REVIEW),
        )
    except Exception as exc:  # noqa: BLE001 -- see the docstring
        log.error("could not record review signal type=%s payment_id=%s: %s",
                  signal_type, payment_id, type(exc).__name__)
        return None

    if not rows:
        # Already flagged for this reason. Not an error, and not worth a warning:
        # it is the dedupe doing its job.
        log.info("review signal already present type=%s payment_id=%s",
                 signal_type, payment_id)
        return None

    item_id = rows[0]["id"]
    # Signal category, queue, and the non-identifying correlation reference.
    # No amount, no loan-level money, no instrument data, no customer -- the
    # client's privacy instruction applied to the log line as well as to the row.
    log.info("review signal recorded type=%s queue=%s item_id=%s correlation_ref=%s",
             signal_type, QUEUE_RECONCILIATION_REVIEW, item_id, correlation_ref)
    return item_id


def record_exact_idempotency_key_signal(*, payment_id: int, loan_id: int,
                                        correlation_ref: str | None) -> int | None:
    """A request arrived again under an idempotency key that already has a payment.

    Called from the replay branch of `charge()`, where the existing controls have
    already decided the outcome: no second row, no second authorization, the
    original result replayed. The signal says a human should know the attempt
    happened; it changes nothing about the payment.

    `related_payment_id` is the same row, because on this path the attempt has no
    payment of its own -- which is exactly the point. The queue shows one item
    against the payment that already exists, however many times the key is
    retried.
    """
    return _record(SIGNAL_EXACT_IDEMPOTENCY_KEY, payment_id=payment_id,
                   loan_id=loan_id, related_payment_id=payment_id,
                   correlation_ref=correlation_ref)


def record_exact_provider_reference_signal(*, payment_id: int, loan_id: int,
                                           processor_ref: str,
                                           correlation_ref: str | None) -> int | None:
    """The processor returned a settlement reference another capture already has.

    Regardless of elapsed time, per the client's decision: an identical provider
    transaction id is an exact-duplicate *signal* whether it arrives seconds or
    weeks later.

    The unique index on `payments.processor_ref` means the second write is
    refused, and it stays refused -- this records the collision around that
    refusal rather than relaxing it. The earlier payment is looked up so a
    reviewer has both ends of the collision.
    """
    related = None
    try:
        rows = db.query(
            "SELECT id FROM payments WHERE processor_ref = %s AND id <> %s "
            "ORDER BY id LIMIT 1",
            (processor_ref, payment_id),
        )
        related = rows[0]["id"] if rows else None
    except Exception as exc:  # noqa: BLE001
        log.error("could not resolve the earlier payment for a provider-reference "
                  "collision payment_id=%s: %s", payment_id, type(exc).__name__)

    return _record(SIGNAL_EXACT_PROVIDER_REF, payment_id=payment_id,
                   loan_id=loan_id, related_payment_id=related,
                   correlation_ref=correlation_ref)


def heuristic_candidates(candidate: dict, *, now=None) -> list:
    """Earlier captures this one resembles on all four factors, inside the window.

    The database narrows by loan, source, channel and time -- the index in
    `db/migrations/0044` exists for exactly this query -- and
    `heuristic_matches` then decides, so the rule the client wrote lives in one
    testable predicate rather than in a WHERE clause nobody can unit-test.

    Capture time, not row-insert time. `captured_at` is when the processor
    confirmed the money moved (`db/migrations/0040`); `created_at` is stamped at
    INSERT while the row is still pending, and using it would put an
    authorization that crossed midnight in the wrong window -- the same defect
    reconciliation's scoping already had to fix.
    """
    source = candidate.get("source_ref")
    captured_at = candidate.get("captured_at")
    if not source or captured_at is None:
        # Cannot prove same source, or cannot place the capture in time. Either
        # way the client's rule is not satisfied and no signal is produced.
        return []

    since = captured_at - WINDOW
    try:
        rows = db.query(
            "SELECT id, loan_id, amount, method, source_ref, captured_at "
            "FROM payments "
            "WHERE loan_id = %s AND source_ref = %s AND method = %s "
            "  AND auth_status = 'captured' AND captured_at IS NOT NULL "
            "  AND captured_at >= %s AND id <> %s "
            "ORDER BY captured_at DESC",
            (candidate["loan_id"], source, candidate["method"], since,
             candidate["id"]),
        )
    except Exception as exc:  # noqa: BLE001
        log.error("could not look for review candidates payment_id=%s: %s",
                  candidate.get("id"), type(exc).__name__)
        return []

    return [row for row in rows if heuristic_matches(candidate, row)]


def record_heuristic_signal_if_any(candidate: dict) -> int | None:
    """Flag `candidate` when an earlier capture matches all four factors.

    One item per candidate payment, pointing at the nearest earlier match. The
    reviewer's question is "are these two the same payment?", and answering it
    needs one pair, not a fan-out.
    """
    matches = heuristic_candidates(candidate)
    if not matches:
        return None

    return _record(SIGNAL_HEURISTIC_WINDOW, payment_id=candidate["id"],
                   loan_id=candidate["loan_id"],
                   related_payment_id=matches[0]["id"],
                   correlation_ref=candidate.get("correlation_id"))
