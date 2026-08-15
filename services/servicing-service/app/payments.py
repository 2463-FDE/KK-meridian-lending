"""Payment handling (formerly the vendor's prototype 'pay.py').

There is NO idempotency key. A retry inserts another payment record and applies
the loan balance again. It double-records and double-applies; it does not perform
another processor charge — this route calls no processor at all, so the card is
untouched and what is wrong is the loan balance and the payment history.

That distinction is the difference between this defect and the one
payment-service had, and this comment used to collapse the two by borrowing
payment-service's name for it. The names matter because the remedies differ: one
is corrected by refunding a borrower, the other by correcting a balance.

This is the still-open half of D2:
payment-service's `POST /payments` was fixed (required `idempotency_key`, partial
unique index, apply-once through `payment_applications`), and this duplicate was
never ported. D2 in `docs/DEBT.md` records both halves; reading it as "D2 is
fixed" is what this note exists to prevent.

Bounded, not closed: the route requires `X-Internal-Token` and the gateway 404s
the path rather than proxying it, so the retry that duplicates has to come from
inside the compose network. Having no processor is also why its rows are labelled
`capture_source='servicing_legacy'` and excluded from reconciliation (D7) —
there is no settlement line that could ever corroborate either copy.

`servicing-service/tests/test_legacy_payments_is_not_idempotent.py` characterizes both
duplications. It fails when this route becomes idempotent or is deleted, which is
deliberate: the test and the D2 entry move in the same change.

ADR 0008 (Week 5 tokenization): this used to receive and store the FULL PAN
and CVV on the payments row, and log the full charge request (PAN, CVV, SSN)
at INFO with zero redaction (D5) -- this duplicate, legacy endpoint just
hadn't been ported to the same fix payment-service's own /payments already
got. Card capture tokenizes at the processor -- this never receives a raw
PAN/CVV/SSN at all anymore, only an opaque processor_token (accepted but not
persisted -- there's nothing here that needs it after the fact) plus
last4/brand for display.
"""
from .logging_config import get_logger
from . import db, balance

log = get_logger("payment")   # writes to logs/payment-service.log


def charge(loan_id: int, processor_token: str, last4: str, brand: str, amount: float,
           name: str = None, method: str = "card") -> dict:
    # No raw PAN/CVV/SSN ever reaches this point (ADR 0008) -- nothing left
    # here that needs redacting before logging.
    log.info(
        "POST /payments charge loan_id=%s amount=%s method=%s -> ok",
        loan_id, amount, method,
    )
    # No idempotency check. No unique charge reference. Every POST inserts a row.
    #
    # `capture_source` is written explicitly (db/migrations/0042), and it is the
    # one honest thing this route can say about itself: it calls NO processor, so
    # the row it writes will never appear in a settlement file. Before the column
    # existed the insert took `auth_status`'s default of 'captured' and left
    # `processor_ref` NULL, which meant reconciliation -- which had just learned
    # to report unreferenced captures -- reported every payment this route made,
    # permanently, as money it could not corroborate. It never could have: there
    # is nothing on the other side to corroborate it against.
    #
    # Labelled rather than excluded by inference. Reconciliation counts these and
    # reports the count; what it does not do is compare them against a file that
    # cannot contain them. That this route moves a balance with no processor
    # behind it at all is D2, and it is not this control's to fix.
    db.query(
        "INSERT INTO payments (loan_id, last4, brand, amount, method, capture_source) "
        "VALUES (%s, %s, %s, %s, %s, 'servicing_legacy')",
        (loan_id, last4, brand, float(amount), method),
    )
    new_balance = balance.apply_payment(loan_id, amount)
    return {"loan_id": loan_id, "amount": amount, "balance": new_balance}
