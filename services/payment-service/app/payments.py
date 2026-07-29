"""Payment handling (moved verbatim from servicing-service's payments.py).

Stores the FULL PAN and the CVV on the payments row (D5 — still open; that's the
persisted-storage half, unrelated to logging and not fixed here).

D12 note: unlike disclosure-service/servicing-service, this service does no
repeated arithmetic on amount (no accumulation loop), so there's no float-drift
scenario to fix here in that sense. What WAS missing: the incoming amount was
never validated or normalized at all -- a malformed float from a client (e.g.
19.999999999999996) got stored and forwarded verbatim, uncorrected. charge()
now quantizes to exactly 2 decimal places via Decimal before it touches the DB
row or the servicing call, so every downstream consumer sees the same, correct
cents value instead of whatever precision happened to arrive.

Review fix: a timeout retry or a double-click on submit used to insert a
second payments row and apply the balance twice via servicing-service -- there
was no idempotency key at all. `idempotency_key` is now required at the API
boundary (see routers/payments.py / schemas.PaymentIn) and enforced by a
partial unique index (db/migrations/0009). The insert's own
ON CONFLICT ... DO NOTHING makes the check-and-write atomic (same pattern
disclosure-service's create_offer uses): a duplicate request is detected even
if it races the original, and returns the ORIGINAL payment result without
charging or calling servicing-service again.

Review fix (follow-up): the above closed the double-CHARGE gap, but a charge
could still silently never reach the loan balance -- if _apply_via_servicing
failed, the exception was swallowed and charge() still reported "captured".
`applied_at` (db/migrations/0012) tracks that separately from "the card was
charged": NULL is a pending/outbox record. A retry on the same
idempotency_key now checks it and retries the apply instead of blindly
repeating "captured". servicing-service's apply-payment is now idempotent by
payment_id itself (db/migrations/0013,
services/servicing-service/app/balance.py), so retrying it is always safe.
"""
import httpx
from decimal import Decimal, ROUND_HALF_UP

from .logging_config import get_logger
from . import db
from .config import SERVICING_URL
from .redactor import redact_dict

log = get_logger("payment")   # writes to logs/payment-service.log

_CENTS = Decimal("0.01")


def _to_cents(amount) -> float:
    d = amount if isinstance(amount, Decimal) else Decimal(str(amount))
    return float(d.quantize(_CENTS, rounding=ROUND_HALF_UP))


def charge(loan_id: int, pan: str, cvv: str, amount: float, idempotency_key: str,
           ssn: str = None, name: str = None, method: str = "card") -> dict:
    amount = _to_cents(amount)

    # D5 fix: the log line used to write full PAN/CVV/SSN at INFO with zero
    # redaction. Storage in the payments table (below) is a separate, still-open
    # gap -- this only closes the logging half.
    safe_req = redact_dict({
        "pan": pan, "cvv": cvv, "ssn": ssn, "amount": amount,
        "loan_id": loan_id, "name": name, "idempotency_key": idempotency_key,
    })
    log.info("POST /payments charge req=%s -> ok", safe_req)

    # Review fix: atomic check-and-write, same ON CONFLICT DO NOTHING + read-
    # back pattern disclosure-service's create_offer uses. A duplicate request
    # (retry, double-click) never inserts a second row or re-applies the
    # balance, even if it races the original request.
    inserted = db.query(
        "INSERT INTO payments (loan_id, pan, cvv, amount, method, idempotency_key) "
        "VALUES (%s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING "
        "RETURNING id, loan_id, amount",
        (loan_id, pan, cvv, amount, method, idempotency_key),   # full PAN + CVV persisted
    )
    if inserted:
        row = inserted[0]
        payment_id = row["id"]
        # Apply the captured amount to the balance via servicing-service --
        # only for the request that actually inserted the row.
        applied = _apply_via_servicing(loan_id, row["amount"], payment_id)
    else:
        row = db.query(
            "SELECT id, loan_id, amount, applied_at FROM payments WHERE idempotency_key = %s",
            (idempotency_key,),
        )[0]
        payment_id = row["id"]
        if row["applied_at"] is None:
            # Review fix: the original request's apply either never ran or
            # never confirmed -- this retry is the reconciliation opportunity,
            # not just a read-back. Safe to call again: servicing-service's
            # apply-payment is idempotent by payment_id (db/migrations/0013).
            log.info(
                "duplicate POST /payments for idempotency_key=%s -> payment_id=%s "
                "not yet applied, retrying apply",
                idempotency_key, payment_id,
            )
            applied = _apply_via_servicing(loan_id, row["amount"], payment_id)
        else:
            applied = True
            log.info(
                "duplicate POST /payments for idempotency_key=%s -> returning original "
                "payment_id=%s (already applied)",
                idempotency_key, payment_id,
            )

    return {
        "payment_id": payment_id,
        "loan_id": row["loan_id"],
        # Review fix: "captured" now means the balance is confirmed applied, not
        # just that the card was charged and the row written. "pending" means
        # the charge is captured but the balance apply hasn't been confirmed yet
        # -- a retry with the same idempotency_key will keep trying to reconcile it.
        "status": "captured" if applied else "pending",
        "applied_amount": float(row["amount"]),
    }


def _apply_via_servicing(loan_id: int, amount: float, payment_id: int) -> bool:
    """Tell servicing-service to apply this payment to the loan balance.

    Returns whether the apply was confirmed. Review fix: this used to swallow
    the exception and let charge() report "captured" regardless -- the card
    was charged but the balance never moved, with no record anything was left
    undone. Now records applied_at (db/migrations/0012) only on confirmed
    success, so a same-key retry can tell the difference and retry the apply
    instead of repeating a false "captured".
    """
    url = f"{SERVICING_URL}/accounts/{loan_id}/apply-payment"
    try:
        resp = httpx.post(
            url, json={"amount": amount, "payment_id": payment_id}, timeout=5.0
        )
        resp.raise_for_status()
        db.query("UPDATE payments SET applied_at = now() WHERE id = %s", (payment_id,))
        log.info(
            "applied payment via servicing loan_id=%s payment_id=%s amount=%s -> ok",
            loan_id, payment_id, amount,
        )
        return True
    except Exception as exc:
        # Servicing unreachable / errored — the card was already charged and the row
        # written, so we still report the charge captured, but as "pending" (not yet
        # applied) rather than falsely claiming the balance moved. Reconciled by the
        # next same-key retry, or by an out-of-band job (not yet built) for a charge
        # that's never retried.
        log.error(
            "apply-payment call to servicing failed loan_id=%s payment_id=%s: %s",
            loan_id, payment_id, exc,
        )
        return False
