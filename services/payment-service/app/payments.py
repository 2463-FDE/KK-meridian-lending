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
`processor.lookup_authorization()` FIRST to ask the processor whether it
already has a record of this idempotency_key, and only calls
`authorize_charge()` (now itself passed the idempotency_key, so the
processor also dedupes on its end) if the processor genuinely has none.

Review fix (reconciliation, D7): the capture UPDATE now also persists
`processor_ref` -- the processor's OWN settlement reference for the capture
(db/migrations/0041) -- and takes `captured_at` from the processor rather than
from `now()` on BOTH capture paths, not only on the recovered one. Without the
reference, reconciliation had no join key to the settlement file and could
compare nothing finer than a net total per loan, which let two offsetting
defects on one loan cancel out and report a clean run. Without the processor's
timestamp on the first-attempt path, a capture that straddled midnight was
scoped to the wrong reconciliation day and manufactured the false breaks that
teach an operator to stop reading them.

Both capture UPDATEs also set `capture_source = 'processor'`
(db/migrations/0042). That column is what reconciliation's ledger side filters
on, so a capture that does not set it is silently outside the comparison -- and
`payments` has a second writer that legitimately is (servicing's legacy route),
which is why the column exists and why the default is not 'processor'.
"""
import uuid

import httpx
from decimal import Decimal, ROUND_HALF_UP

from .logging_config import get_logger
from . import db, processor, review_signals
# The one database error this module handles rather than propagates: a
# settlement reference another capture already holds (see `_mark_captured`).
from psycopg2.errors import UniqueViolation
from .config import INTERNAL_SERVICE_TOKEN, SERVICING_URL
from .processor import ChargeDeclinedError
from .redactor import redact_dict, redact_str

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


def new_correlation_id() -> str:
    """A fresh identifier for one payment's journey across services.

    Server-minted, never caller-supplied. `PaymentIn` forbids unknown fields, so
    a client cannot set this -- deliberately. A correlator decides how our own
    evidence is indexed, and letting a caller choose it would let them collide
    two unrelated payments into one trace, or push content of their choosing
    into a column we later read back into log lines.

    Distinct from `idempotency_key`, and the distinction is the point:

      * `idempotency_key` is caller-supplied and DECIDES SOMETHING -- whether
        two requests are the same payment. It is an input to a money decision.
      * `correlation_id` is server-minted and decides NOTHING. Nothing keys,
        joins, dedupes or reconciles on it. If every value were replaced with a
        different one tomorrow, no balance would move.

    Prefixed so a value found in a log line announces what it is, and opaque
    beyond that: a UUID carries no loan, no amount and no card data.
    """
    return f"pay_{uuid.uuid4().hex}"


class ServicingAuthUnavailable(Exception):
    """servicing-service will not accept our credentials, so a capture would strand money."""


def _require_servicing_auth(loan_id: int | None = None) -> None:
    """Raise unless servicing can accept AND PERSIST an apply for this loan.

    Called immediately before EVERY authorize_charge(), not once at the top of
    the happy path. Review round 3: the pending-duplicate retry branch called
    authorize_charge() directly, so a retry of a request whose authorization
    never confirmed re-charged the card with no servicing check at all -- the
    precise charged-but-uncredited case this guard exists to prevent, reachable
    by the one path most likely to be taken during an incident.
    """
    if not _servicing_auth_ok(loan_id):
        raise ServicingAuthUnavailable(
            "servicing-service rejected our internal token; refusing to charge"
        )


def _servicing_auth_ok(loan_id: int | None = None) -> bool:
    """Confirm servicing will accept our credentials, BEFORE authorizing a card.

    **Fails closed.** True is returned only for an explicit 200 carrying the
    expected body. A timeout, DNS failure, TLS error, connection reset, 5xx or an
    unrecognised body all mean the same thing for this decision: we cannot
    confirm the system that credits the borrower is reachable, so we do not take
    their money.

    Review round 4 corrected this, and the earlier version was mine to defend.
    It returned True on any exception, reasoning that "unknown is not known-bad"
    and that refusing payments on every servicing blip trades a rare accounting
    error for a common outage. That argument is wrong here, for two reasons.

    First, it contradicts what this guard is for. The preflight exists because a
    capture that cannot be credited is the worst outcome in the system; letting
    an unreachable servicing through means the guard only caught an explicit
    401 -- the narrow case -- while the broad case, servicing simply being down,
    sailed past it.

    Second, "the reconciler will fix it" is not an answer a borrower accepts.
    payment-service does have a durable drain for captured-but-unapplied rows,
    and it does work -- but it only works once servicing comes back, and until
    then real money has left a real card while the balance has not moved. An
    uncharged customer retries in a minute; a charged customer with no credit
    files a complaint.

    The cost is stated rather than hidden: card capture is now unavailable
    whenever servicing is unavailable. That coupling is deliberate and is
    recorded in ARCHITECTURE.md -- for money movement, accounting correctness
    beats availability. The timeout is kept short so an outage fails fast rather
    than hanging the request, and replaying an already-captured payment does not
    reach this check at all, because it authorizes nothing.
    """
    try:
        resp = httpx.get(
            f"{SERVICING_URL}/internal/auth-check",
            # Review round 8: the loan being charged. Servicing probes THAT
            # loan's balance row, so a 200 means the row this payment will
            # credit is writable -- not merely that some row somewhere was.
            params={"loan_id": loan_id} if loan_id is not None else None,
            headers={"X-Internal-Token": INTERNAL_SERVICE_TOKEN},
            timeout=2.0,
        )
    except Exception as e:  # noqa
        log.error(
            "servicing auth preflight did not complete (%s) -- refusing to charge, "
            "because a capture could not be credited while servicing is unreachable",
            type(e).__name__,
        )
        return False
    if resp.status_code != 200:
        log.error(
            "servicing auth preflight returned %s -- refusing to charge. If this is "
            "401/403, check INTERNAL_SERVICE_TOKEN parity between payment-service "
            "and servicing-service",
            resp.status_code,
        )
        return False
    try:
        body = resp.json()
    except Exception:  # noqa
        log.error("servicing auth preflight returned a 200 that was not JSON -- refusing to charge")
        return False
    # A 200 from something that is not servicing's auth-check -- a proxy error
    # page, a misrouted health endpoint -- must not be read as authorization.
    if body.get("auth") != "ok":
        log.error(
            "servicing auth preflight returned 200 without auth=ok (%r) -- refusing to charge",
            body,
        )
        return False
    return True


def _mark_captured(payment_id: int, loan_id: int, *, auth_id: str,
                   captured_at, processor_ref: str | None,
                   correlation_id: str | None) -> None:
    """Record the capture, and survive a settlement reference we already hold.

    Three columns travel together here and each has a reason: `captured_at` never
    moves separately from `auth_status`, because reconciliation scopes its window
    on it while `created_at` is stamped when the row was still pending
    (db/migrations/0040); `processor_ref` is the processor's own settlement
    reference and the only join key a break report can name (0041); and
    `capture_source = 'processor'` is what puts the row in scope for the
    comparison at all (0042).

    **`processor_ref` is UNIQUE**, so a settlement reference identifies exactly
    one row. When a processor hands back a reference another capture already
    carries, this UPDATE raises a unique violation -- and review of PR #79 found
    what that cost: the money had already been authorised, the request then failed
    on the write, and the `exact_provider_transaction_id` signal that collision
    exists to raise could never be recorded. The one case the signal is for was
    the one case unable to reach it.

    So the collision is handled where it happens:

      * the capture is still recorded. The card was charged; refusing to write
        that down is the worst outcome available;
      * `processor_ref` is left NULL on this row, because the reference belongs to
        the other one and a UNIQUE index is not negotiable. Reconciliation
        already has a name for a capture it cannot match by reference --
        `unreferenced_capture`, reported as a break rather than skipped (0041) --
        and that is the honest state for this row;
      * the review signal is raised, naming the payment that holds the reference,
        so a human sees both ends of the collision.

    The unique index is untouched. This observes its refusal rather than relaxing
    it.
    """
    try:
        db.query(
            "UPDATE payments SET auth_status = 'captured', authorization_id = %s, "
            "captured_at = COALESCE(%s::timestamptz, now()), "
            "processor_ref = %s, capture_source = 'processor' WHERE id = %s",
            (auth_id, captured_at, processor_ref, payment_id),
        )
    except UniqueViolation:
        # Only the reference can collide in this statement: `id` is this row's own
        # primary key and nothing else here is constrained.
        log.warning(
            "provider settlement reference already recorded against another "
            "capture -- recording this capture without it and raising a review "
            "signal payment_id=%s correlation_id=%s", payment_id, correlation_id)
        db.query(
            "UPDATE payments SET auth_status = 'captured', authorization_id = %s, "
            "captured_at = COALESCE(%s::timestamptz, now()), "
            "capture_source = 'processor' WHERE id = %s",
            (auth_id, captured_at, payment_id),
        )
        if processor_ref:
            review_signals.record_exact_provider_reference_signal(
                payment_id=payment_id, loan_id=loan_id,
                processor_ref=processor_ref, correlation_ref=correlation_id)


def _flag_review_signals(payment_id: int, loan_id: int,
                         correlation_id: str | None,
                         processor_ref: str | None) -> None:
    """Record any review signals this capture raises. Never raises, never blocks.

    Two questions, asked in the order the evidence appears:

      * did the processor hand back a settlement reference another capture
        already holds? That is an exact-duplicate signal at any distance in time.
      * is there an earlier capture on this loan, for the same amount, from the
        same source, on the same channel, inside 30 minutes? That is a heuristic
        review candidate -- all four factors, per the client's decision.

    Both are review-only. Neither reverses, refunds, blocks, reallocates or
    re-applies anything, and neither returns a value the caller acts on.
    """
    if processor_ref:
        try:
            others = db.query(
                "SELECT 1 FROM payments WHERE processor_ref = %s AND id <> %s LIMIT 1",
                (processor_ref, payment_id),
            )
        except Exception as exc:  # noqa: BLE001 -- observation must not fail a capture
            log.error("could not check for a provider-reference collision "
                      "payment_id=%s: %s", payment_id, type(exc).__name__)
            others = []
        if others:
            review_signals.record_exact_provider_reference_signal(
                payment_id=payment_id, loan_id=loan_id,
                processor_ref=processor_ref, correlation_ref=correlation_id)

    try:
        candidate = db.query(
            "SELECT id, loan_id, amount, method, source_ref, captured_at, "
            "       correlation_id "
            "FROM payments WHERE id = %s",
            (payment_id,),
        )
    except Exception as exc:  # noqa: BLE001
        log.error("could not read back the capture for review screening "
                  "payment_id=%s: %s", payment_id, type(exc).__name__)
        return
    if candidate:
        review_signals.record_heuristic_signal_if_any(candidate[0])


def charge(loan_id: int, processor_token: str, last4: str, amount: float, idempotency_key: str,
           brand: str = None, name: str = None, method: str = "card",
           # An opaque, non-identifying handle for the funding source, supplied
           # by the capture boundary (db/migrations/0044). Stored so the
           # duplicate-review heuristic can require "same source" rather than
           # guessing from loan and amount; never used for anything else, and
           # None when the caller cannot prove one.
           source_ref: str = None) -> dict:
    amount = _to_cents(amount)
    # The key is caller-supplied free text, and every branch below writes it
    # into a log line or an error message. The FIRST log goes through
    # redact_dict; the duplicate-retry branches interpolated it directly, so a
    # caller using a PAN or an SSN as their key had it masked on the initial
    # request and written in the clear on the retry -- the one request that is
    # guaranteed to happen twice. Redacted once here and used everywhere the
    # raw value would otherwise be formatted. Reviewed on PR #16.
    safe_key = redact_str(idempotency_key)

    # Minted at entry, before anything is logged or written, because the
    # authorization leg needs it too: `payment_id` is only assigned by the INSERT
    # below, so anything keyed on it starts one hop too late and the processor
    # call -- the leg where money actually leaves -- would have no trace key at
    # all. A retry replaces this with the original payment's id (see below).
    correlation_id = new_correlation_id()

    # Review fix: the log line used to write full PAN/CVV/SSN at INFO with zero
    # redaction (D5). There's no raw PAN/CVV/SSN to log anymore (ADR 0008) --
    # redact_dict still guards processor_token, since a vaulted token is
    # itself sensitive even though it's opaque.
    # The cardholder `name` is no longer included (D5d). It was logged in clear
    # beside a loan id, an amount and a last4, which together identify a person
    # and what they paid. It also served no diagnostic purpose: a charge is
    # correlated by loan_id, last4 and idempotency_key. Omitted rather than
    # merely redacted, so there is nothing to redact -- and `name` is in
    # _SENSITIVE_KEYS as well, so reintroducing the field cannot reintroduce
    # the leak.
    safe_req = redact_dict({
        "processor_token": processor_token, "last4": last4, "brand": brand,
        "amount": amount, "loan_id": loan_id,
        "idempotency_key": idempotency_key,
    })
    # Deliberately WITHOUT the correlation id. At this point the request has not
    # been resolved to a row, so on a retry the id minted above is about to be
    # discarded in favour of the stored one -- and a log line carrying a
    # discarded id is worse than one carrying none. It looks like evidence and
    # returns nothing when an operator greps it. Reviewed on PR #56
    # (CORR-LOG-001). The canonical line is emitted below, once the row is known.
    log.info("POST /payments charge req=%s -> ok", safe_req)

    # Review fix: atomic check-and-write, same ON CONFLICT DO NOTHING + read-
    # back pattern disclosure-service's create_offer uses. A duplicate request
    # (retry, double-click) never inserts a second row, even if it races the
    # original request. auth_status starts 'pending' -- authorization is NOT
    # yet confirmed at this point, on purpose (see below).
    inserted = db.query(
        "INSERT INTO payments (loan_id, last4, brand, amount, method, idempotency_key, "
        "auth_status, correlation_id, source_ref) "
        "VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s, %s) "
        "ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING "
        "RETURNING id, loan_id, amount, correlation_id",
        (loan_id, last4, brand, amount, method, idempotency_key, correlation_id,
         source_ref),
    )
    if inserted:
        row = inserted[0]
        payment_id = row["id"]
        # The line an operator greps. Emitted after the row exists, so the id in
        # it always matches a payments row.
        log.info("payment accepted correlation_id=%s payment_id=%s loan_id=%s",
                 correlation_id, payment_id, row["loan_id"])
        # Review fix: this is the actual authorization call -- a made-up
        # processor_token is declined here, not silently trusted. Only a
        # confirmed approval reaches _apply_via_servicing; a decline never
        # touches the loan balance at all.
        _require_servicing_auth(row["loan_id"])
        try:
            auth = processor.authorize_charge(processor_token, row["amount"], idempotency_key,
                                              correlation_id=correlation_id)
        except ChargeDeclinedError as exc:
            db.query("UPDATE payments SET auth_status = 'failed' WHERE id = %s", (payment_id,))
            log.warning("charge declined payment_id=%s correlation_id=%s: %s",
                        payment_id, correlation_id, exc)
            return {
                "payment_id": payment_id, "loan_id": row["loan_id"],
                "status": "failed", "applied_amount": float(row["amount"]),
            }
        # Review fix: auth_status and authorization_id used to be written in
        # two separate statements -- a crash between them left 'captured'
        # with no authorization id on record. One UPDATE, one atomic write.
        # One statement, and it survives a settlement reference we already
        # hold -- see `_mark_captured` for what a collision costs if it does not.
        _mark_captured(payment_id, row["loan_id"], auth_id=auth.authorization_id,
                       captured_at=auth.captured_at,
                       processor_ref=auth.processor_ref,
                       correlation_id=correlation_id)
        # Flag for human review, AFTER the money path has done its work and
        # WITHOUT affecting it. Both calls swallow their own failures
        # (review_signals._record) because a review queue must never be able to
        # fail a capture: the queue is an observation about the money path, not
        # part of it.
        #
        # The provider-reference signal comes first because the collision is
        # already visible here: the processor handed back a settlement reference,
        # and if another capture holds it, that is an exact-duplicate signal
        # regardless of how long ago the other one was. The unique index on
        # `payments.processor_ref` is untouched -- this observes the collision, it
        # does not permit a second row.
        _flag_review_signals(payment_id, row["loan_id"], correlation_id,
                             auth.processor_ref)
        applied = _apply_via_servicing(loan_id, row["amount"], payment_id, correlation_id)
    else:
        row = db.query(
            "SELECT id, loan_id, amount, applied_at, auth_status, correlation_id "
            "FROM payments WHERE idempotency_key = %s",
            (idempotency_key,),
        )[0]
        # The retry adopts the ORIGINAL payment's correlation id and discards the
        # one minted above. A retry is the same payment, so it belongs to the
        # same trace; minting a second here would split one payment's evidence
        # across two ids and quietly defeat the whole column -- and the retry
        # path is the one an incident actually exercises.
        #
        # NULL stays NULL, with no fallback to the fresh mint. A row captured
        # before this column existed HAS no trace, and nothing on this path
        # persists a new id -- so falling back would log, send and stamp an
        # identifier the `payments` row does not carry, unfindable by the very id
        # it advertises. That is the round-1 dead-id defect arriving through the
        # history the column deliberately preserves. Reviewed on PR #56
        # (CORR-NULL-001).
        #
        # Back-filling the row instead was considered and rejected: the capture
        # and its authorization already happened without an id, so the trace
        # would cover only the tail of the payment while looking complete. An
        # absent trace is a true statement; a partial one presented as whole is
        # the same failure in a subtler form.
        correlation_id = row.get("correlation_id")
        log.info("payment recognised as a repeat correlation_id=%s payment_id=%s "
                 "loan_id=%s", correlation_id, row["id"], row["loan_id"])
        payment_id = row["id"]
        # The idempotency key repeated, regardless of elapsed time -- an
        # exact-duplicate SIGNAL under the client's decision of 2026-08-24, and
        # nothing more. The controls below still decide the outcome: no second
        # row, no second authorization, the original result replayed. One review
        # item per payment per signal type, so a client retrying ten times gives
        # an operator one thing to look at.
        #
        # Recorded before the conflict check below, deliberately: a key reused
        # for a DIFFERENT loan or amount raises IdempotencyKeyConflict, and that
        # attempt is the one most worth a human seeing.
        review_signals.record_exact_idempotency_key_signal(
            payment_id=payment_id, loan_id=row["loan_id"],
            correlation_ref=correlation_id)
        # Review fix: row["amount"] comes back from Postgres as a Decimal
        # (psycopg2 NUMERIC mapping) while `amount` here is a plain float --
        # Decimal('10.99') != 10.99 under Python's float/Decimal comparison,
        # so an identical retry was misjudged as a conflict and 409'd instead
        # of returning the original result. Compare both sides as Decimal.
        if row["loan_id"] != loan_id or row["amount"] != Decimal(str(amount)):
            raise IdempotencyKeyConflict(
                f"idempotency_key={safe_key!r} was already used for "
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
                "correlation_id=%s still pending authorization, checking processor "
                "before retrying",
                safe_key, payment_id, correlation_id,
            )
            # ONE lookup for all three facts this branch needs -- that the
            # processor holds the charge, when it took the money, and which
            # settlement reference it will appear under. Three separate calls
            # would be three round trips to a payment processor on the path an
            # incident actually exercises, and three chances for the answers to
            # disagree with each other.
            existing = processor.lookup_authorization(idempotency_key)
            if existing:
                log.info(
                    "processor already has an authorization on record for "
                    "idempotency_key=%s -> payment_id=%s correlation_id=%s, reusing "
                    "it instead of re-charging", safe_key, payment_id, correlation_id,
                )
                auth_id = existing.authorization_id
                # The PROCESSOR's capture time, not ours.
                #
                # The money was taken when the processor took it -- possibly
                # yesterday, before the crash that left this row pending. Since
                # reconciliation windows on `captured_at`, stamping now() here
                # would place the capture on the retry date while the settlement
                # file has it on the original one: a settlement-only break on
                # day N and a ledger-only break on day N+1, two false findings
                # out of one crash.
                #
                # None when the processor reports no timestamp, and the SQL
                # below then falls back to now() -- the previous behaviour and
                # the best estimate available, but a fallback, not the default.
                captured_at = existing.captured_at
                processor_ref = existing.processor_ref
            else:
                # This retry's own processor_token since the token itself is
                # never persisted (ADR 0008); idempotency_key is passed along
                # so the processor also dedupes on its end.
                # Same guard as the first attempt. Leaving it out here was the
                # hole: this branch is the one an incident actually exercises,
                # because it is what a client retry lands on.
                #
                # The row stays 'pending' when this raises -- deliberately. It is
                # a retryable state, so the same idempotency_key can be used
                # again once the token skew is fixed, and no card was charged.
                _require_servicing_auth(row["loan_id"])
                try:
                    auth = processor.authorize_charge(processor_token, row["amount"], idempotency_key,
                                              correlation_id=correlation_id)
                except ChargeDeclinedError as exc:
                    db.query("UPDATE payments SET auth_status = 'failed' WHERE id = %s", (payment_id,))
                    log.warning("charge declined on retry payment_id=%s correlation_id=%s: %s",
                                payment_id, correlation_id, exc)
                    return {
                        "payment_id": payment_id, "loan_id": row["loan_id"],
                        "status": "failed", "applied_amount": float(row["amount"]),
                    }
                auth_id = auth.authorization_id
                # This branch charged just now, so unless the processor reports
                # its own timestamp our clock IS the capture time.
                captured_at = auth.captured_at
                processor_ref = auth.processor_ref
            _mark_captured(payment_id, row["loan_id"], auth_id=auth_id,
                           captured_at=captured_at, processor_ref=processor_ref,
                           correlation_id=correlation_id)
            # Screen the recovered capture too. Review of PR #79 found that only
            # the first-attempt path was screened, so a capture completing on the
            # retry path -- the pending-authorization recovery, which is exactly
            # the path a duplicate charge arrives on -- raised no heuristic
            # signal at all. Same swallow-failures behaviour: an observation must
            # never fail a payment.
            _flag_review_signals(payment_id, row["loan_id"], correlation_id,
                                 processor_ref)

        if row["applied_at"] is None:
            # Review fix: the original request's apply either never ran or
            # never confirmed -- this retry is the reconciliation opportunity,
            # not just a read-back. Safe to call again: servicing-service's
            # apply-payment is idempotent by payment_id (db/migrations/0013).
            log.info(
                "duplicate POST /payments for idempotency_key=%s -> payment_id=%s "
                "correlation_id=%s not yet applied, retrying apply",
                safe_key, payment_id, correlation_id,
            )
            # Review fix: reconcile against the ORIGINALLY stored loan_id, not
            # the retry request's own loan_id parameter -- a retry that (by
            # bug or bad-faith) sends a different loan_id with the same
            # idempotency_key must never misapply the payment to that loan.
            applied = _apply_via_servicing(row["loan_id"], row["amount"], payment_id,
                                           correlation_id)
        else:
            applied = True
            log.info(
                "duplicate POST /payments for idempotency_key=%s -> returning original "
                "payment_id=%s correlation_id=%s (already applied)",
                safe_key, payment_id, correlation_id,
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


def _apply_via_servicing(loan_id: int, amount: float, payment_id: int,
                         correlation_id: str | None = None) -> bool:
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
            # The correlation id crosses with the apply, so servicing can stamp
            # the ledger rows it writes with the id this service already logged
            # and stored. Sent rather than derived: servicing minting its own
            # would leave each side holding an identifier the other has never
            # seen, which looks like a trace and is not one.
            url, json={"amount": float(amount), "payment_id": payment_id,
                       "correlation_id": correlation_id}, timeout=5.0,
            # servicing-service now requires this on every money-moving route.
            # This call is the LSS half of the split payment flow and is the one
            # legitimate caller of apply-payment that is not the gateway, so it
            # has to present the token too or every capture stops reaching the
            # balance -- and it would fail quietly, since the caller treats a
            # servicing error as "captured but not yet applied" and leaves the
            # row for the reconciler.
            headers={"X-Internal-Token": INTERNAL_SERVICE_TOKEN},
        )
        # getattr, not attribute access: this branch must only fire when we
        # POSITIVELY know the answer was an auth rejection. Anything that does
        # not report a status falls through to raise_for_status() and the
        # existing pending/reconcile path, which is the conservative direction --
        # a capture wrongly treated as transient is retried, whereas one wrongly
        # treated as permanent is abandoned.
        if getattr(resp, "status_code", None) in (401, 403):
            # Distinct from a transient failure: the money is captured and this
            # will never succeed on retry until the tokens agree, so it is logged
            # as the operator-actionable event it is rather than folded into the
            # generic pending path where the reconciler would retry it forever.
            log.error(
                "servicing REJECTED OUR CREDENTIALS applying a captured payment "
                "(%s) loan_id=%s payment_id=%s -- the card was charged and the "
                "balance cannot be credited until INTERNAL_SERVICE_TOKEN matches "
                "between payment-service and servicing-service",
                resp.status_code, loan_id, payment_id,
            )
            return False
        resp.raise_for_status()
        db.query("UPDATE payments SET applied_at = now() WHERE id = %s", (payment_id,))
        log.info(
            "applied payment via servicing correlation_id=%s loan_id=%s "
            "payment_id=%s amount=%s -> ok",
            correlation_id, loan_id, payment_id, amount,
        )
        return True
    except Exception as exc:
        # Servicing unreachable / errored — the card was already charged and the row
        # written, so we still report the charge captured, but as "pending" (not yet
        # applied) rather than falsely claiming the balance moved.
        #
        # Review fix (PR #8): "or by an out-of-band job (not yet built)" used to be
        # the end of this comment, and that job really did not exist -- a charge the
        # client never retried stayed captured-and-uncredited forever. app/reconcile.py
        # is that job now; leaving applied_at NULL is what enqueues this row for it.
        # apply_next_attempt_at stays NULL here, which means "due immediately".
        #
        # Exception TYPE only: a servicing error message can embed the request
        # parameters, and this column is for triage, not for reconstructing the call.
        db.query(
            "UPDATE payments SET apply_last_error = %s WHERE id = %s",
            (type(exc).__name__, payment_id),
        )
        log.error(
            "apply-payment call to servicing failed correlation_id=%s loan_id=%s "
            "payment_id=%s error_type=%s",
            correlation_id, loan_id, payment_id, type(exc).__name__,
        )
        return False
