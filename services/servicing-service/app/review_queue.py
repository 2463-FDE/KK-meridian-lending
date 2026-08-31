"""The in-app reconciliation review queue: read it, and record a human's answer.

The client's decision of 2026-08-24 named exactly one destination for a payment
flagged for review -- "Meridian's internal in-app reconciliation queue/dashboard"
-- and ruled out every external channel (email, Slack, PagerDuty, webhook, SMS)
before the freeze. `db/migrations/0045` created the table payment-service writes
to. This is the surface a human actually reads it through.

**Why this lives in servicing-service and not in payment-service, which writes
the rows.** A disposition names its reviewer, and the row is only worth anything
if that name is a verified human rather than a claimed header. The signed
principal machinery (`principal.require_staff_principal`, Ed25519 assertions
minted by the gateway) exists here and nowhere else; payment-service
authenticates callers with a shared internal token, which identifies a *service*.
A disposition recorded there could only record who a caller *said* it was.
Splitting write-side detection from read-side judgement is the point, not an
accident of layout.

**What this module may not become.** The flag is not a duplicate conclusion and
not permission to move money. Nothing here touches `ledger_entries`, `balances`,
the payment waterfall, capture state or `payments.applied_at` -- the only table
it writes is its own. This module records what the reviewer decided; it does not
act on it.

**And what happens next is a balance adjustment, not a reversal.** This paragraph
used to finish by sending the reviewer through maker-checker to undo the payment
-- described in a way that read as though maker-checker could perform a card
reversal. The old sentence is deliberately paraphrased rather than quoted:
`db/tests/test_no_surface_promises_a_reversal.py` scans this file, and a verbatim
quotation would trip the very guard that exists to keep the claim out.
Maker-checker cannot do it: `maker_checker.ENTRY_TYPES` is
`{adjustment, fee_waived}`, and no service in this repository exposes a refund,
void, reversal or chargeback route. Reconciliation PARSES refund lines out of the
processor's settlement file, which is reading one somebody else performed rather
than performing one.

So a reviewer who concludes `confirmed_duplicate` has one supported route: a
balance ADJUSTMENT raised on the loan's account page and approved by a different
authorised person in `/approvals`. That corrects the loan balance; it does not
return money to the card, and nothing here does.

The same false claim was on `/reconciliation` in two places and was removed with
this docstring. `db/tests/test_no_surface_promises_a_reversal.py` now covers this
file as well as the page, because a defect that survives one file behind the
screen is one the next reader learns from the backend instead.
"""
import logging

from . import db

log = logging.getLogger("servicing.review_queue")

#: The three the client authorised, and no fourth. The database CHECK is the real
#: enforcement (`reconciliation_review_disposition_known`); this set is here so
#: the API refuses with a message naming the permitted values rather than letting
#: a typo surface as a constraint violation in a 500.
DISPOSITIONS = ("confirmed_duplicate", "legitimate_distinct_payment",
                "requires_further_review")

#: Which signals are exact evidence and which are a heuristic. The client drew
#: this line itself and a reviewer must see it: an exact provider-reference or
#: idempotency-key repeat is strong evidence regardless of elapsed time, while
#: the 30-minute candidate is four factors agreeing inside a window and is
#: routinely a legitimate second payment.
_EXACT_SIGNALS = ("exact_provider_transaction_id", "exact_idempotency_key")


def signal_category(signal_type: str) -> str:
    return "exact" if signal_type in _EXACT_SIGNALS else "heuristic"


#: What a reviewer is shown about each of the two payments.
#:
#: `amount`, `method` and `captured_at` are the whole question -- "is this the
#: same payment twice, or a second real one?" cannot be answered without them,
#: and this is the authenticated in-app surface the client authorised as the
#: destination. Deliberately NOT selected: `last4`, `brand`, `processor_ref`,
#: `authorization_id`, `idempotency_key`, `source_ref`. None of them change the
#: answer -- the signal already states which identity matched -- and the client's
#: constraint on review data names instrument and token material explicitly. A
#: queue is exactly the kind of surface that gets screenshotted into a ticket.
_PAYMENT_FIELDS = ("id", "amount", "method", "captured_at", "auth_status")

# **Written out, not composed.** `db/tests/test_payments_sql_is_static.py`
# refuses `%`-formatted or f-string SQL against the `payments` table, and the
# first version of this module was caught by it: `scripts/check_no_pan_readers.py`
# gates a destructive migration by folding SQL statically, and a template it
# cannot fold is a blind spot in the check that decides whether a column is safe
# to drop. Building the two projections from `_PAYMENT_FIELDS` was tidier and
# bought nothing -- the payload below is derived from that list, so a field added
# there without a matching alias here raises a KeyError on the first row rather
# than silently disappearing.
_SELECT = """
    SELECT r.id, r.created_at, r.signal_type, r.loan_id, r.correlation_ref,
           r.queue, r.status, r.disposition, r.disposition_note,
           r.reviewed_at, r.reviewed_by, r.reviewed_by_role,
           p.id AS payment_id, p.amount AS payment_amount,
           p.method AS payment_method, p.captured_at AS payment_captured_at,
           p.auth_status AS payment_auth_status,
           q.id AS related_id, q.amount AS related_amount,
           q.method AS related_method, q.captured_at AS related_captured_at,
           q.auth_status AS related_auth_status
      FROM reconciliation_review_items r
      JOIN payments p ON p.id = r.payment_id
      LEFT JOIN payments q ON q.id = r.related_payment_id
"""


def _payment(row, prefix: str) -> dict | None:
    """One payment's shown fields, or None when there is no related payment.

    `related_payment_id` is nullable by design: a provider-reference collision
    may not know which earlier capture holds the reference. A LEFT JOIN then
    yields NULLs for every related column, and `related_id IS NULL` is how that
    is told apart from a real row -- `payments.amount` is NOT NULL, so a null
    amount could never distinguish the two.
    """
    if row[prefix + "_id"] is None:
        return None
    # Built by walking `_PAYMENT_FIELDS` rather than naming each key again.
    # Spelling the dict out separately made the projection and the payload two
    # lists that could disagree: a column added to the SELECT was silently not
    # returned, and -- the direction that matters -- a field added to the payload
    # was not covered by the guard that asserts what this may not carry. One list
    # means adding a column here is caught there.
    out = {}
    for field in _PAYMENT_FIELDS:
        value = row[prefix + "_" + field]
        if field == "amount":
            # str(), not float(): NUMERIC(14,2) arrives as Decimal, and a float
            # here would reintroduce exactly the D12 defect the column type was
            # changed to fix.
            value = str(value)
        elif value is not None and field.endswith("_at"):
            value = str(value)
        out[field] = value
    return out


def _item(row) -> dict:
    return {
        "id": row["id"],
        "created_at": str(row["created_at"]),
        "signal_type": row["signal_type"],
        # Derived here rather than stored: it is a reading of the signal, and a
        # stored copy could disagree with the signal it describes.
        "signal_category": signal_category(row["signal_type"]),
        "loan_id": row["loan_id"],
        "correlation_ref": row["correlation_ref"],
        "queue": row["queue"],
        "status": row["status"],
        "disposition": row["disposition"],
        "disposition_note": row["disposition_note"],
        "reviewed_at": str(row["reviewed_at"]) if row["reviewed_at"] else None,
        "reviewed_by": row["reviewed_by"],
        "reviewed_by_role": row["reviewed_by_role"],
        "payment": _payment(row, "payment"),
        "related_payment": _payment(row, "related"),
    }


def queue(*, status: str = "open", limit: int = 100) -> list[dict]:
    """Items awaiting review, oldest signal first.

    Oldest first, unlike the maker-checker queue: a review candidate is a payment
    a borrower has already been charged for, so the longest-waiting one is the
    most urgent. `created_at DESC` would bury it under today's.
    """
    rows = db.query(
        _SELECT + " WHERE r.status = %s ORDER BY r.created_at ASC LIMIT %s",
        (status, limit))
    return [_item(row) for row in rows]


def get(item_id: int) -> dict | None:
    rows = db.query(_SELECT + " WHERE r.id = %s", (item_id,))
    return _item(rows[0]) if rows else None


def counts() -> dict:
    """How many are open, split by signal category.

    Exactly the facts the client permitted telemetry to carry: that review items
    exist, which queue, the signal category, and the status. No amount, no
    payment, no person.
    """
    rows = db.query(
        "SELECT signal_type, status, count(*) AS n "
        "  FROM reconciliation_review_items GROUP BY signal_type, status")
    out = {"open_exact": 0, "open_heuristic": 0, "reviewed": 0}
    for row in rows:
        if row["status"] == "reviewed":
            out["reviewed"] += row["n"]
        else:
            out["open_" + signal_category(row["signal_type"])] += row["n"]
    return out


class ReviewConflict(Exception):
    """The item cannot take this disposition -- it is gone, or already answered."""


def record_disposition(item_id: int, *, disposition: str, note: str | None,
                       actor) -> dict:
    """Record a human's classification. Write-once, and it moves no money.

    Conditional on `status = 'open'` in the UPDATE's own WHERE clause rather than
    checked first and written after: two reviewers opening the same item is the
    ordinary case in a shared queue, and a read-then-write would let the second
    one overwrite the first one's answer in the gap between. The database trigger
    (`reconciliation_review_items_write_once`) refuses that too -- this is the
    layer that turns it into a 409 instead of a 500.
    """
    if disposition not in DISPOSITIONS:
        # Defence in depth behind the route's own Literal validation: a future
        # internal caller must not be able to write a policy nobody authorised.
        raise ValueError("disposition must be one of %r" % (DISPOSITIONS,))

    rows = db.query(
        "UPDATE reconciliation_review_items"
        "   SET status = 'reviewed', disposition = %s, disposition_note = %s,"
        "       reviewed_at = now(), reviewed_by = %s, reviewed_by_role = %s"
        " WHERE id = %s AND status = 'open'"
        " RETURNING id",
        (disposition, note, actor.subject, actor.role, item_id))

    if not rows:
        existing = get(item_id)
        if existing is None:
            raise ReviewConflict("review item %s does not exist" % item_id)
        raise ReviewConflict(
            "review item %s was already reviewed as %r; a disposition is "
            "write-once" % (item_id, existing["disposition"]))

    log.info("review item %s dispositioned %s by %s (%s)",
             item_id, disposition, actor.subject, actor.role)
    return get(item_id)
