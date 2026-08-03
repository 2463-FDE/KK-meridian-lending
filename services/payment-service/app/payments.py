"""Payment handling (moved verbatim from servicing-service's payments.py).

Review fix (ADR 0008, Week 5 tokenization): this used to store the FULL PAN
and CVV on the payments row (D5). Card capture now tokenizes at the processor
(see frontend/lib/tokenize.ts) -- this service never receives a raw PAN/CVV
at all anymore, only an opaque processor_token plus last4/brand for display.
The token itself is never persisted either (a vaulted token is itself
sensitive) -- only last4/brand reach the `payments` row. See
specs/0001-online-payments-idempotency-tokenization.md Part 2.

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
partial unique index (db/migrations/0007, redundantly also 0010 -- see that
file). The insert's own ON CONFLICT ... DO NOTHING makes the check-and-write
atomic (same pattern disclosure-service's create_offer uses): a duplicate
request is detected even if it races the original, and returns the ORIGINAL
payment result without charging or calling servicing-service again.

Review fix (follow-up): the above closed the double-CHARGE gap, but a
charge could still silently never reach the loan balance -- if
_apply_via_servicing failed, the exception was swallowed and charge() still
reported "captured". `applied_at` (db/migrations/0012) tracks that
separately from "the card was charged": NULL is a pending/outbox record. A
retry on the same idempotency_key now checks it and retries the apply instead
of blindly repeating "captured". servicing-service's apply-payment is now
idempotent by payment_id itself (db/migrations/0013,
services/servicing-service/app/balance.py), so retrying it is always safe.

Review fix: `amount` is range-constrained in schemas.PaymentIn (0, 1_000_000] --
a negative value used to credit the borrower's balance instead of charging
them (servicing computes new_balance = current - amount), and NaN/Infinity
passed through uncaught too.

Review fix: `charge()` used to treat receiving a `processor_token` as proof
the card was actually charged -- the token was only shape/length-checked,
never sent to a processor for real authorization. A borrower could POST any
made-up token and last4, and this code would write a captured payment and
tell servicing-service to reduce their loan balance for real. Every charge
now goes through `processor.authorize_charge()` first (fail-closed outside
dev/test, same convention as decision-service's bureau/AI-scorer calls) --
a row is written `auth_status='pending'` before that call, then flipped to
`'captured'` or `'failed'` once it actually returns (db/migrations/0017), so
a crash mid-authorization is never silently mistaken for success.

Review fix (double-charge on retry): flipping `auth_status` to 'captured'
used to be a SEPARATE write from the processor call itself, with no record
of the processor's own authorization id at all -- a crash between the
processor approving the charge and that UPDATE running left a payment
row stuck 'pending' with a real authorization already issued, and a same-
key retry then called authorize_charge() again with no way to know that.
Two things close this: (1) `authorization_id` (db/migrations/0019) is now
persisted in the SAME UPDATE statement that flips auth_status to
'captured' -- one atomic write, not two; (2) a pending retry calls
`processor.get_authorization()` FIRST to ask the processor whether it
already has a record of this idempotency_key, and only calls
`authorize_charge()` (now itself passed the idempotency_key, so the
processor also dedupes on its end) if the processor genuinely has none.
"""
import httpx
from decimal import Decimal, ROUND_HALF_UP

from .logging_config import get_logger
from . import db, processor
from .config import SERVICING_URL
from .processor import ChargeDeclinedError
from .redactor import redact_dict

log = get_logger("payment")   # writes to logs/payment-service.log

_CENTS = Decimal("0.01")


class IdempotencyKeyConflict(Exception):
    """Raised when a repeated idempotency_key arrives attached to a DIFFERENT
    loan_id or amount than the request that originally used that key.

    Review fix: silently honoring the stored row would either misapply the
    ORIGINAL amount to a caller who thinks they're charging a different
    loan/amount, or -- before this check existed -- risk reconciling against
    whichever loan_id happened to be passed on the retry. A key collision like
    this means the caller reused a key for a genuinely different payment,
    which is a client bug (or an attempted key-guessing attack), not a safe
    retry -- surfaced as 409 rather than silently doing either thing.
    """


def _to_cents(amount) -> float:
    d = amount if isinstance(amount, Decimal) else Decimal(str(amount))
    return float(d.quantize(_CENTS, rounding=ROUND_HALF_UP))


def charge(loan_id: int, processor_token: str, last4: str, amount: float, idempotency_key: str,
           brand: str = None, name: str = None, method: str = "card") -> dict:
    amount = _to_cents(amount)

    # Review fix: the log line used to write full PAN/CVV/SSN at INFO with zero
    # redaction (D5). There's no raw PAN/CVV/SSN to log anymore (ADR 0008) --
    # redact_dict still guards processor_token, since a vaulted token is
    # itself sensitive even though it's opaque.
    safe_req = redact_dict({
        "processor_token": processor_token, "last4": last4, "brand": brand,
        "amount": amount, "loan_id": loan_id, "name": name,
        "idempotency_key": idempotency_key,
    })
    log.info("POST /payments charge req=%s -> ok", safe_req)

    # Review fix: atomic check-and-write, same ON CONFLICT DO NOTHING + read-
    # back pattern disclosure-service's create_offer uses. A duplicate request
    # (retry, double-click) never inserts a second row, even if it races the
    # original request. auth_status starts 'pending' -- authorization is NOT
    # yet confirmed at this point, on purpose (see below).
    inserted = db.query(
        "INSERT INTO payments (loan_id, last4, brand, amount, method, idempotency_key, auth_status) "
        "VALUES (%s, %s, %s, %s, %s, %s, 'pending') "
        "ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING "
        "RETURNING id, loan_id, amount",
        (loan_id, last4, brand, amount, method, idempotency_key),
    )
    if inserted:
        row = inserted[0]
        payment_id = row["id"]
        # Review fix: this is the actual authorization call -- a made-up
        # processor_token is declined here, not silently trusted. Only a
        # confirmed approval reaches _apply_via_servicing; a decline never
        # touches the loan balance at all.
        try:
            auth_id = processor.authorize_charge(processor_token, row["amount"], idempotency_key)
        except ChargeDeclinedError as exc:
            db.query("UPDATE payments SET auth_status = 'failed' WHERE id = %s", (payment_id,))
            log.warning("charge declined payment_id=%s: %s", payment_id, exc)
            return {
                "payment_id": payment_id, "loan_id": row["loan_id"],
                "status": "failed", "applied_amount": float(row["amount"]),
            }
        # Review fix: auth_status and authorization_id used to be written in
        # two separate statements -- a crash between them left 'captured'
        # with no authorization id on record. One UPDATE, one atomic write.
        db.query(
            "UPDATE payments SET auth_status = 'captured', authorization_id = %s WHERE id = %s",
            (auth_id, payment_id),
        )
        applied = _apply_via_servicing(loan_id, row["amount"], payment_id)
    else:
        row = db.query(
            "SELECT id, loan_id, amount, applied_at, auth_status FROM payments WHERE idempotency_key = %s",
            (idempotency_key,),
        )[0]
        payment_id = row["id"]
        # Review fix: row["amount"] comes back from Postgres as a Decimal
        # (psycopg2 NUMERIC mapping) while `amount` here is a plain float --
        # Decimal('10.99') != 10.99 under Python's float/Decimal comparison,
        # so an identical retry was misjudged as a conflict and 409'd instead
        # of returning the original result. Compare both sides as Decimal.
        if row["loan_id"] != loan_id or row["amount"] != Decimal(str(amount)):
            raise IdempotencyKeyConflict(
                f"idempotency_key={idempotency_key!r} was already used for "
                f"loan_id={row['loan_id']} amount={row['amount']} -- this "
                f"request is loan_id={loan_id} amount={amount}"
            )

        if row["auth_status"] == "failed":
            # Already declined for this key -- stays declined. A borrower who
            # wants to actually retry the charge needs a new idempotency_key
            # (a genuinely new attempt), not a replay of a declined one.
            return {
                "payment_id": payment_id, "loan_id": row["loan_id"],
                "status": "failed", "applied_amount": float(row["amount"]),
            }

        if row["auth_status"] == "pending":
            # The original request's authorization call never ran, never
            # confirmed, or confirmed but the process died before persisting
            # that fact (process died mid-flight, any of the three). Review
            # fix: ask the processor whether it ALREADY has an authorization
            # on record for this idempotency_key before charging again -- a
            # blind re-authorize here risked a second real charge in exactly
            # the "processor approved, then we crashed" case.
            log.info(
                "duplicate POST /payments for idempotency_key=%s -> payment_id=%s "
                "still pending authorization, checking processor before retrying",
                idempotency_key, payment_id,
            )
            existing_auth_id = processor.get_authorization(idempotency_key)
            if existing_auth_id:
                log.info(
                    "processor already has an authorization on record for "
                    "idempotency_key=%s -> payment_id=%s, reusing it instead of "
                    "re-charging", idempotency_key, payment_id,
                )
                auth_id = existing_auth_id
            else:
                # This retry's own processor_token since the token itself is
                # never persisted (ADR 0008); idempotency_key is passed along
                # so the processor also dedupes on its end.
                try:
                    auth_id = processor.authorize_charge(processor_token, row["amount"], idempotency_key)
                except ChargeDeclinedError as exc:
                    db.query("UPDATE payments SET auth_status = 'failed' WHERE id = %s", (payment_id,))
                    log.warning("charge declined on retry payment_id=%s: %s", payment_id, exc)
                    return {
                        "payment_id": payment_id, "loan_id": row["loan_id"],
                        "status": "failed", "applied_amount": float(row["amount"]),
                    }
            db.query(
                "UPDATE payments SET auth_status = 'captured', authorization_id = %s WHERE id = %s",
                (auth_id, payment_id),
            )

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
            # Review fix: reconcile against the ORIGINALLY stored loan_id, not
            # the retry request's own loan_id parameter -- a retry that (by
            # bug or bad-faith) sends a different loan_id with the same
            # idempotency_key must never misapply the payment to that loan.
            applied = _apply_via_servicing(row["loan_id"], row["amount"], payment_id)
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
        # "failed" means the processor declined the authorization -- no balance
        # was ever touched.
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

    E2E bug found in the field (same fix as kalab-week4-disclosure-automation):
    `amount` here is read back from the payments row's RETURNING/SELECT
    (row["amount"]), and psycopg2 hands back a NUMERIC column as Decimal
    regardless of what type was inserted -- httpx's json= can't serialize
    Decimal, so this raised on every real (non-mocked) call and every
    payment silently reported "pending" forever. float() at the JSON
    boundary fixes it regardless of what type the caller passes.
    """
    url = f"{SERVICING_URL}/accounts/{loan_id}/apply-payment"
    try:
        resp = httpx.post(
            url, json={"amount": float(amount), "payment_id": payment_id}, timeout=5.0
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
