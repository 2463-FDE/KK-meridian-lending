"""Card-network authorization (ADR 0008 follow-up, review fix).

`payments.py::charge()` used to treat receiving a `processor_token` as proof
the card was actually charged -- the token was only shape/length-checked,
never sent anywhere for real authorization. A borrower could POST any
made-up token and last4, and the code would write a captured payment and
tell servicing-service to reduce their loan balance: real money movement
backed by nothing.

`authorize_charge()` closes that gap with the same fail-closed contract
decision-service already uses for its bureau pull / AI scorer
(`_pull_credit`/`_call_ai_scorer`, `CreditBureauUnavailableError`/
`ModelUnavailableError`): outside development/test, a missing or
unreachable real processor must refuse the charge (`ProcessorUnavailableError`)
rather than silently approve one against a fake authority. An explicit
decline from the processor (or the stub's own decline path) raises
`ChargeDeclinedError` -- a legitimate outcome, not a system failure.

No real payment processor is integrated in this training app (same
"no real vendor behind this stub" situation as the bureau pull before a real
Experian account existed). `_stub_authorize()` is the dev/test stand-in:
`ALLOW_PAYMENT_STUB` gates it exactly like `ALLOW_CREDIT_STUB`/
`ALLOW_MODEL_STUB` gate theirs.

Review fix (double-charge on retry): `authorize_charge()` used to be called
with no idempotency key at all -- a same-key retry from payments.py (e.g. the
process dying between the processor approving the charge and payments.py
persisting that fact) had no way to avoid re-issuing a brand new charge at
the processor. `idempotency_key` is now forwarded to the real processor call
(as an `Idempotency-Key` header, the standard convention a real processor
dedupes on) and to the stub, and `get_authorization()` lets a caller ask
"does the processor already have a record of this key?" *before* charging
again -- see payments.py::charge()'s pending-retry branch.

Review fix (reconciliation): `authorize_charge()` returned the authorization id
and threw the rest of the processor's answer away. It now returns an
`Authorization` carrying the processor's own capture timestamp and its
settlement reference as well, because reconciliation needs both -- see that
class's docstring for what each one was costing.
"""
import hashlib
import re
from typing import NamedTuple

import httpx

from . import redactor
from .config import ALLOW_PAYMENT_STUB, ENVIRONMENT, PROCESSOR_API_KEY, PROCESSOR_BASE_URL
from .logging_config import get_logger

log = get_logger("processor")

# Matches frontend/lib/tokenize.ts's own mock token shape exactly -- a token
# that doesn't match this was never issued by the (mock) tokenization step at
# all, the same way a real processor would reject a token it never minted.
_MOCK_TOKEN_RE = re.compile(r"^tok_mock_[0-9a-f-]{36}$", re.IGNORECASE)

# Fixed test-decline amount, same convention as a real processor's published
# test-card numbers: lets the decline path be exercised deterministically,
# with no live processor and no need to special-case a fake token value.
_TEST_DECLINE_AMOUNT = 0.02


class ProcessorUnavailableError(RuntimeError):
    """No real processor is configured/reachable and stub mode isn't allowed."""


class ChargeDeclinedError(RuntimeError):
    """The processor (or the stub standing in for one) declined the charge.

    A legitimate, expected outcome -- not a system failure. Callers must
    treat this as "no money moved," never as "captured."
    """


class Authorization(NamedTuple):
    """Everything the processor tells us about one capture.

    Review fix: `authorize_charge()` used to return the authorization id alone
    and DISCARD the rest of the processor's response. Two consequences, both
    landing on reconciliation:

    * `captured_at` was thrown away, so payments.py stamped `now()` on the
      normal charge path. A charge whose processor confirmation and local UPDATE
      straddle midnight was then scoped to the wrong reconciliation day -- the
      exact false-break class migration 0040 exists to close, closed for
      recovered rows and left open on the path almost every capture takes.
    * `processor_ref` was never asked for at all, so no payment row carried the
      settlement file's own join key and reconciliation could compare nothing
      finer than a per-loan total. Per-loan totals let two offsetting defects
      cancel and report `ok` (db/migrations/0041).

    Both extra fields are OPTIONAL, because a processor that does not report
    them is a real configuration and must not block a capture whose money has
    already moved. A missing value is recorded as missing: a NULL `captured_at`
    falls back to `now()`, and a NULL `processor_ref` is reported by
    reconciliation as an `unreferenced_capture` break. Neither is invented.
    """

    authorization_id: str
    #: The processor's own capture time, ISO-8601. None if it reports none.
    captured_at: str | None = None
    #: The reference the settlement file will carry for this capture, e.g.
    #: PR-100231. None if the processor reports none.
    processor_ref: str | None = None


# Review fix: stands in for a real processor's own idempotency-key store --
# a real processor (Stripe et al.) remembers an authorization it already
# issued for a given key and returns that SAME authorization on a repeat
# call instead of charging again. Keyed on idempotency_key, not token/amount,
# since a key is the thing a retry is guaranteed to repeat.
#
# Holds the whole Authorization rather than just the id, so the stub can answer
# the same three questions a real processor answers on lookup.
_stub_authorizations: dict[str, Authorization] = {}


def _stub_settlement_reference(processor_token: str, idempotency_key: str = None) -> str:
    """A stable, unique stand-in for the processor's settlement reference.

    Derived from the idempotency key, because that is the value guaranteed
    unique per payment. Deriving it from the token would collide across the many
    payments a demo or a test suite makes with the same mock card, and
    `idx_payments_processor_ref` is UNIQUE on purpose.

    Shaped like the references in the settlement file (`PR-...`) so a dev run
    exercises the same string shape production will, and marked STUB so nobody
    can mistake one for evidence from a real processor.
    """
    seed = idempotency_key or processor_token or ""
    return "PR-STUB-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12].upper()


def _stub_authorize(processor_token: str, amount: float,
                    idempotency_key: str = None) -> Authorization:
    if idempotency_key and idempotency_key in _stub_authorizations:
        return _stub_authorizations[idempotency_key]
    if amount == _TEST_DECLINE_AMOUNT:
        raise ChargeDeclinedError(f"processor declined: test-decline amount {amount}")
    if not _MOCK_TOKEN_RE.match(processor_token or ""):
        raise ChargeDeclinedError("processor declined: token not recognized")
    auth = Authorization(
        authorization_id="auth_stub_" + processor_token[-12:],
        # The stub authorizes right now, so OUR clock IS the capture time and
        # there is no processor timestamp to inherit. None rather than a
        # fabricated value: payments.py falls back to now(), which is correct
        # here, and this is the case the cross-midnight tests distinguish from a
        # recovered row.
        captured_at=None,
        processor_ref=_stub_settlement_reference(processor_token, idempotency_key),
    )
    if idempotency_key:
        _stub_authorizations[idempotency_key] = auth
    return auth


def _authorization_from_body(body: dict) -> Authorization:
    """Read one processor response into the three things we persist."""
    return Authorization(
        authorization_id=body["authorization_id"],
        # Named for what the processor calls it; absent on older stubs.
        captured_at=body.get("captured_at") or body.get("created"),
        # Same story: processors name the settlement handle differently, and the
        # one that matters is whichever name appears in the settlement file.
        processor_ref=(body.get("processor_ref")
                       or body.get("settlement_reference")
                       or body.get("reference")),
    )


def lookup_authorization(idempotency_key: str) -> Authorization | None:
    """Everything the processor knows about this key, in ONE round trip.

    The pending-row recovery path needs three facts about a charge it may
    already have made: that the processor holds it, when the processor took the
    money, and what reference the settlement file will use. Asking three times
    would mean three calls to a payment processor on the path an incident
    actually exercises, and three chances for the answers to disagree.

    Returns None if the processor has no record of this key (never charged, or
    genuinely unreachable in a dev/test stub configuration).
    """
    if not idempotency_key:
        return None
    if not PROCESSOR_API_KEY:
        if not ALLOW_PAYMENT_STUB:
            raise ProcessorUnavailableError(
                f"PROCESSOR_API_KEY is not set (ENVIRONMENT={ENVIRONMENT!r}) -- refusing "
                "to look up an authorization against a fake processor outside development/test."
            )
        return _stub_authorizations.get(idempotency_key)

    try:
        resp = httpx.get(
            f"{PROCESSOR_BASE_URL}/charges",
            params={"idempotency_key": idempotency_key},
            headers={"Authorization": f"Bearer {PROCESSOR_API_KEY}"},
            timeout=15.0,
        )
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:
        if not ALLOW_PAYMENT_STUB:
            raise ProcessorUnavailableError(f"processor lookup call failed: {exc}") from exc
        log.warning("processor lookup call failed (%s) -- falling back to dev/test stub lookup", exc)
        return _stub_authorizations.get(idempotency_key)

    if not body.get("approved"):
        return None
    return _authorization_from_body(body)


def get_authorization(idempotency_key: str) -> str | None:
    """Whether the processor already has an authorization on record for this
    idempotency_key, without issuing a new charge.

    Review fix: a process that dies between the processor approving a charge
    and payments.py persisting `authorization_id`/`auth_status='captured'`
    used to leave a payment stuck 'pending' with no local record that the
    charge already happened -- a retry then called authorize_charge() again,
    a real risk of a second charge at the processor. A pending retry looks the
    key up FIRST; only if the processor genuinely has no record of it is it safe
    to call authorize_charge() (see payments.py::charge()).

    Kept as the narrow id-only question. The recovery path itself uses
    `lookup_authorization()`, because it also needs the capture time and the
    settlement reference and must not pay for three round trips to get them.
    """
    existing = lookup_authorization(idempotency_key)
    return existing.authorization_id if existing else None


def get_authorization_captured_at(idempotency_key: str) -> str | None:
    """The processor's OWN capture timestamp for an existing authorization.

    Why it matters: a service that died after processor approval leaves the row
    'pending'. The borrower retries -- possibly the next morning -- and recording
    `captured_at = now()` would place the capture on the retry date while the
    processor's settlement file has it on the original one. Since reconciliation
    windows on `captured_at`, that manufactures a settlement-only break on day N
    and a ledger-only break on day N+1: two false findings from one crash.

    Returns None when the processor does not report a timestamp, or in a stub
    configuration that never recorded one. The caller then falls back to
    `now()`, which is the previous behaviour and the best available estimate --
    but it is a fallback, not the default.
    """
    existing = lookup_authorization(idempotency_key)
    return existing.captured_at if existing else None


def authorize_charge(processor_token: str, amount: float,
                     idempotency_key: str = None,
                     correlation_id: str = None) -> Authorization:
    """Confirm a real authorization for exactly `amount` before any payment
    is marked captured or any balance is touched.

    Returns the `Authorization` the processor reported: its authorization id,
    its own capture timestamp, and the settlement reference the file will carry.
    Review fix -- this used to return the id ALONE, which is why the happy path
    stamped `captured_at = now()` on a capture the processor may have taken on
    the previous day, and why no captured row carried a reference reconciliation
    could match against a settlement line.

    `idempotency_key` (Review fix) is forwarded to the real processor so it
    also dedupes on its end -- a repeat call with the same key returns the
    SAME authorization rather than charging twice.

    Raises:
        ChargeDeclinedError: the processor declined the charge.
        ProcessorUnavailableError: no processor is configured/reachable and
            ALLOW_PAYMENT_STUB is not set (i.e. outside development/test).
    """
    # Review fix: callers pass row["amount"] read back from Postgres, which
    # is a Decimal regardless of what type was inserted (same gap as
    # _apply_via_servicing) -- httpx's json= can't serialize Decimal at all,
    # and Decimal('0.02') != 0.02 under naive comparison broke the stub's own
    # test-decline check. Normalize to float once, up front.
    amount = float(amount)

    # A token that carries card or personal data never leaves this process.
    #
    # Review finding PAY-FLOW-001 on PR #51. The stub path declines an
    # unrecognised token before authorizing, so `processor_token` looked closed.
    # The REAL path did not check it at all: with `PROCESSOR_API_KEY` set, the
    # value is posted verbatim as `json={"token": ...}`, so a caller sending a
    # PAN in the one free-form string field put a card number in an outbound
    # request body -- a card number leaving the process, which is precisely the
    # question the data-flow statement exists to answer. The check sat in a
    # branch that only runs when no processor is configured.
    #
    # Placed BEFORE the stub/real split so both paths are covered by one guard,
    # and duplicated at the API boundary (`schemas.PaymentIn`) rather than only
    # here: the boundary refuses before a `payments` row is written at all, and
    # this one covers any caller that is not an HTTP request.
    #
    # `ChargeDeclinedError` rather than a new exception type, because the
    # caller's correct behaviour is identical to a decline -- no money moved, the
    # row is marked failed, nothing captured -- and `_stub_authorize` already
    # declines an unrecognised token with the same class.
    #
    # **Cost, stated rather than hidden:** a real processor whose token format
    # contains a Luhn-valid digit run, or a nine-digit run, would be refused
    # here. That direction is deliberate. A token is an opaque correlator by
    # definition, and if a live format ever collides with the shapes this
    # rejects, the fix is the token format or an explicit allowance for it --
    # not deleting the check that keeps card data out of an outbound body.
    if redactor.looks_sensitive(processor_token or ""):
        log.error(
            "refusing to authorize: the processor_token carries card or personal "
            "data shapes, and sending it would put that data in an outbound "
            "request body"
        )
        raise ChargeDeclinedError(
            "processor declined: the token carries card or personal data and was "
            "not transmitted"
        )

    if not PROCESSOR_API_KEY:
        if not ALLOW_PAYMENT_STUB:
            raise ProcessorUnavailableError(
                f"PROCESSOR_API_KEY is not set (ENVIRONMENT={ENVIRONMENT!r}) -- refusing "
                "to authorize a charge against a fake processor outside development/test."
            )
        log.warning("PROCESSOR_API_KEY not set -- using deterministic dev/test stub "
                    "authorization correlation_id=%s", correlation_id)
        return _stub_authorize(processor_token, amount, idempotency_key)

    try:
        resp = httpx.post(
            f"{PROCESSOR_BASE_URL}/charges",
            json={"token": processor_token, "amount": amount, "idempotency_key": idempotency_key},
            headers={
                "Authorization": f"Bearer {PROCESSOR_API_KEY}",
                **({"Idempotency-Key": idempotency_key} if idempotency_key else {}),
                # The trace leg no other identifier can cover: this call happens
                # before the payments row has an id, so anything keyed on
                # `payment_id` starts one hop too late. A header rather than a
                # body field -- it is metadata about the request, not part of
                # the charge, and a processor that ignores it must still charge
                # correctly.
                **({"X-Correlation-Id": correlation_id} if correlation_id else {}),
            },
            timeout=15.0,
        )
        resp.raise_for_status()
        body = resp.json()
    except ChargeDeclinedError:
        raise
    except Exception as exc:
        if not ALLOW_PAYMENT_STUB:
            raise ProcessorUnavailableError(f"processor call failed: {exc}") from exc
        log.warning("processor call failed (%s) -- falling back to dev/test stub authorization", exc)
        return _stub_authorize(processor_token, amount, idempotency_key)

    if not body.get("approved"):
        raise ChargeDeclinedError(f"processor declined: {body.get('reason', 'no reason given')}")
    auth = _authorization_from_body(body)
    if auth.processor_ref is None:
        # Not fatal -- the money has moved and the row must record that. Logged
        # at ERROR because it is an operator-actionable configuration gap: every
        # capture without a reference is a break reconciliation will report and
        # nobody can attribute, so it costs an investigation per payment.
        log.error(
            "processor approved a charge without a settlement reference -- this "
            "capture cannot be matched to a settlement line and reconciliation "
            "will report it as unreferenced"
        )
    return auth
