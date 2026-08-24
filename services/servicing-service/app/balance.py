"""Balance + payment application.

Arithmetic runs in Decimal internally (D12 fix, same pattern as
disclosure-service's apr.py). The money columns are `NUMERIC(14,2)` -- see
`db/init/001_schema.sql`, `balances.balance` and `balances.past_due`. This
docstring claimed for months that they were still `DOUBLE PRECISION` and that
the migration was "a separate, bigger step, not done in this pass"; that
migration landed, and the sentence outlived it.

D3 is CLOSED, and the sentence that said otherwise was the last thing in this
service still asserting it. `apply_payment_once` writes an immutable
`ledger_entries` row and the projection trigger maintains `balances` by
composing signed deltas (`db/migrations/0035_ledger_entries.sql`), so two
concurrent payments both survive. Proven by
`tests/test_balance_lost_update_real_postgres.py` and
`db/tests/test_0035_ledger_projection.py`, which need a real PostgreSQL and
skip without `DATABASE_URL`.

What is genuinely still open here, so nothing below reads as finished:

  * **The waterfall applies to `apply_payment_once` only (D14).** That path
    splits a payment fees -> accrued interest -> principal, in the order
    `policies/fee_schedule.md` publishes. `apply_payment` below does NOT --
    it is dead code, reached by no route, and converting it is part of ADR
    0010's writer retirement rather than this change.
  * **Maker-checker is NOT in this module, and D8 is closed elsewhere.** The
    live `adjust-balance` / `waive-fee` routes raise proposals through
    `maker_checker.propose` and move nothing; a different verified principal
    resolves them, and the approval writes its ledger entry inside
    `resolve_pending_movement`. `adjust_balance` and `waive_fee` below are the
    retired direct writers, reachable from no route (see `models.py`), and their
    docstrings describe what they did rather than what happens today. *This
    bullet read "No maker-checker (D8): they move money on one person's say-so"
    until PRs #34/#35 landed.*
  * **Legacy writers are still direct.** `apply_payment`, `adjust_balance` and
    `waive_fee` write `balances` themselves rather than through the ledger.
    They are not invisible -- 0035's compatibility bridge mirrors each committed
    delta into `ledger_entries` as a `legacy_direct_write` -- but the entry
    carries no actor, and ADR 0010's guard against direct writes stays disabled
    until those three are converted.
  * **A stale returned balance.** `apply_payment` and `waive_fee` compute their
    return value from a read taken before their own UPDATE. The stored value is
    correct; the number handed back to the caller can be out of date if another
    write lands in between.
"""
from decimal import Decimal

from .logging_config import get_logger
from . import db, waterfall

log = get_logger("balance")


class PaymentReplayConflict(ValueError):
    """An idempotency key was reused for a different payment application."""


def _to_decimal(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def get_balance(loan_id: int) -> float:
    rows = db.query("SELECT balance FROM balances WHERE loan_id = %s", (loan_id,))
    return rows[0]["balance"] if rows else 0.0


def get_past_due(loan_id: int) -> float:
    rows = db.query("SELECT past_due FROM balances WHERE loan_id = %s", (loan_id,))
    return rows[0]["past_due"] if rows else 0.0


def apply_payment(loan_id: int, amount: float) -> float:
    """The legacy apply. **Nothing calls it any more.**

    It existed for this service's own `POST /payments`, which was retired with
    D2 -- so no route, and no other module, reaches this function. It is kept for
    one reason, stated so it is not mistaken for a live path:
    `tests/test_money.py` uses it as the vehicle for D12's Decimal evidence,
    and rewriting that evidence onto another path is a money-arithmetic change,
    not part of retiring an endpoint.

    Removing it belongs with ADR 0010's writer conversion (steps 3 and 5), where
    the remaining direct `balances` writers -- `adjust_balance` and `waive_fee` --
    are converted and the guard against direct writes is enabled. Until then this
    is dead code that still writes money, which is exactly the kind of thing that
    should be named rather than left to be discovered.

    No waterfall here -- the whole amount comes off the balance, never
    fees->interest->principal. The live path (`apply_payment_once`) does apply
    it; this one is not reached by any route, and giving dead code a second
    implementation of the allocation would be two places to keep in step.

    The stored balance is safe under concurrency: the UPDATE below is a relative
    delta, so two of these compose rather than one overwriting the other, and
    0035's capture trigger mirrors the delta into the ledger. What is NOT safe is
    the value returned: `current` is read before the UPDATE, so a concurrent
    write lands between them and the caller is handed a balance that was true a
    moment ago. The database is right and the response can be wrong -- which is
    why this is described precisely rather than as "D3", the lost update that the
    ledger projection closed.
    """
    current = get_balance(loan_id)                                       # READ
    new_balance = float(_to_decimal(current) - _to_decimal(amount))      # stale by construction
    db.query(
        "UPDATE balances SET balance = balance - %s, updated_at = now() WHERE loan_id = %s",
        (amount, loan_id),
    )
    log.info("applied payment loan_id=%s balance %s -> %s", loan_id, current, new_balance)
    return new_balance


def apply_payment_once(payment_id: int, loan_id: int, amount: float,
                       correlation_id: str | None = None) -> tuple[float, bool]:
    """Review fix: apply_payment() above has no idempotency of its own -- it
    trusted payment-service to never call apply-payment twice for the same
    payment. payment-service now retries a pending apply on a same-key retry
    (db/migrations/0012), so that trust has to be a real guarantee instead:
    calling this twice for the same payment_id must move the balance once.

    payment_applications' PK on payment_id is the atomic guard -- the INSERT
    only lands a row for whichever call gets there first; only that call goes
    on to actually move the balance. Returns (balance, applied) so the caller
    can tell a genuine apply from a no-op replay.

    Review fix: the marker INSERT and the balance UPDATE must commit or roll
    back together. Each used to be its own auto-committed statement, so if
    apply_payment()'s UPDATE errored or timed out AFTER the marker had already
    landed, the marker was permanent but the balance never moved -- every
    retry for this payment_id then hit the ON CONFLICT path and silently
    skipped the apply forever (money captured, loan never credited). Both
    statements now run inside one transaction (db.transaction()), through the
    cursor it yields -- not apply_payment()/db.query(), which run on a
    separate, shared autocommit connection and so would run outside this
    transaction entirely: if the UPDATE raises, the marker rolls back with
    it, so a retry sees no marker and genuinely retries the apply instead of
    skipping it.
    """
    with db.transaction() as cur:
        cur.execute("SELECT auth_status FROM payments WHERE id = %s", (payment_id,))
        payment_rows = cur.fetchall()
        if not payment_rows:
            raise LookupError(f"payment_id={payment_id} does not exist")
        if payment_rows[0]["auth_status"] != "captured":
            raise ValueError(
                f"payment_id={payment_id} is not captured "
                f"(status={payment_rows[0]['auth_status']})"
            )
        cur.execute(
            "INSERT INTO payment_applications (payment_id, loan_id, amount) "
            "VALUES (%s, %s, %s) ON CONFLICT (payment_id) DO NOTHING RETURNING payment_id",
            (payment_id, loan_id, amount),
        )
        if not cur.fetchall():
            cur.execute(
                "SELECT pa.loan_id, pa.amount, p.auth_status, b.balance "
                "FROM payment_applications pa "
                "JOIN payments p ON p.id = pa.payment_id "
                "JOIN balances b ON b.loan_id = pa.loan_id "
                "WHERE pa.payment_id = %s",
                (payment_id,),
            )
            replay_rows = cur.fetchall()
            if len(replay_rows) != 1:
                raise PaymentReplayConflict(
                    f"payment_id={payment_id} has no complete persisted application"
                )
            persisted = replay_rows[0]
            if (persisted["loan_id"] != loan_id
                    or _to_decimal(persisted["amount"]) != _to_decimal(amount)
                    or persisted["auth_status"] != "captured"):
                raise PaymentReplayConflict(
                    f"payment_id={payment_id} replay does not match its persisted application"
                )
            log.info(
                "apply-payment payment_id=%s exact replay -- skipping duplicate apply",
                payment_id,
            )
            return persisted["balance"], False

        # --- the waterfall (D14) ------------------------------------------
        #
        # This used to write ONE `principal` entry for the whole amount, so a
        # borrower carrying a late fee had their payment reduce principal while
        # the fee stayed owed and kept the loan delinquent.
        #
        # `policies/fee_schedule.md` publishes the order as the source of truth:
        # fees -> accrued interest -> principal. The ledger has been able to
        # hold the split since 0035 (uniqueness is per `(payment_id,
        # component)`, not per payment), and `ledger_payment_allocation_exact`
        # already requires a payment's entries to sum to the captured amount --
        # deferred to commit, which is what lets several land in one
        # transaction. `waterfall.allocate` asserts the same sum before any of
        # them is written.
        cur.execute(
            "SELECT l.principal, l.note_rate_pct, l.term_months, "
            "       l.regular_payment, l.final_payment, l.schedule_version, "
            "       l.opened_at, b.balance, COALESCE(b.past_due, 0) AS past_due "
            "  FROM loans l JOIN balances b ON b.loan_id = l.id "
            " WHERE l.id = %s",
            (loan_id,),
        )
        loan_rows = cur.fetchall()
        if not loan_rows:
            raise LookupError(f"no balances row for loan_id={loan_id}")
        loan = loan_rows[0]

        # Interest already applied, taken from the ledger rather than a column:
        # the ledger is the record of what was actually applied, so this cannot
        # drift from it and no new state has to be kept in step. Payments are
        # negative, so negating the sum gives the amount paid.
        cur.execute(
            "SELECT COALESCE(-SUM(amount), 0) AS paid FROM ledger_entries "
            " WHERE loan_id = %s AND component = 'interest'",
            (loan_id,),
        )
        interest_paid = cur.fetchall()[0]["paid"]

        allocation = waterfall.allocate(
            amount,
            fees_owed=loan["past_due"],
            interest_owed=waterfall.interest_owed(
                loan, interest_already_paid=interest_paid),
            principal_owed=loan["balance"],
        )

        # One row per component that actually moved. A zero entry is refused by
        # `CHECK (amount <> 0)` anyway, and would claim a movement that did not
        # happen.
        for component, part in allocation.components():
            cur.execute(
                # `correlation_id` is stamped from the value RECEIVED with the
                # apply, never minted here (db/migrations/0043). One payment can
                # write three entries -- fees, interest, principal -- and all
                # three carry the same id, so "show me this charge" returns the
                # whole allocation rather than one row of it.
                "INSERT INTO ledger_entries "
                "(loan_id, component, amount, entry_type, payment_id, correlation_id) "
                "VALUES (%s, %s, %s, 'payment', %s, %s)",
                (loan_id, component, -part, payment_id, correlation_id),
            )

        cur.execute("SELECT balance FROM balances WHERE loan_id = %s", (loan_id,))
        rows = cur.fetchall()
        if not rows:
            raise LookupError(f"no balances row for loan_id={loan_id}")
        new_balance = rows[0]["balance"]
        log.info(
            "applied payment correlation_id=%s loan_id=%s fees=%s interest=%s "
            "principal=%s new_balance=%s",
            correlation_id, loan_id, allocation.fees, allocation.interest,
            allocation.principal, new_balance,
        )
    return new_balance, True


def adjust_balance(loan_id: int, new_value: float) -> float:
    """Set the balance directly. RETIRED: no route reaches this.

    This function writes no ledger entry of its own, and the comment here used to
    conclude "the prior value is gone forever". That stopped being true when
    `db/migrations/0035_ledger_entries.sql` landed: its `capture_legacy_balance_delta`
    trigger mirrors this UPDATE's committed delta into `ledger_entries` as a
    `legacy_direct_write`, so the movement is recoverable after the fact.

    What the ledger cannot supply is who did it: the entry's `actor_id` is NULL,
    because this function is handed no human principal to record.

    **Not the live path, and not D8's open half any more.** No route reaches this
    function. `POST /accounts/{loan_id}/adjust-balance` raises a proposal
    (`maker_checker.propose`) that moves nothing, and the approval writes its
    entry inside `resolve_pending_movement` with the approver as `actor_id`
    (spec 0002, ADR 0011, PRs #34/#35). *This paragraph said that half of D8 was
    untouched, which stopped being true when the proposal path landed.*
    """
    new_balance = float(_to_decimal(new_value))
    with db.transaction() as cur:
        cur.execute("SELECT balance FROM balances WHERE loan_id = %s FOR UPDATE", (loan_id,))
        rows = cur.fetchall()
        if not rows:
            raise LookupError(f"no balances row for loan_id={loan_id}")
        current = rows[0]["balance"]
        cur.execute(
            "UPDATE balances SET balance = %s, updated_at = now() WHERE loan_id = %s",
            (new_balance, loan_id),
        )
    log.info("adjusted balance loan_id=%s %s -> %s", loan_id, current, new_value)
    return new_balance


def waive_fee(loan_id: int, amount: float) -> float:
    """Reduce past_due. RETIRED: no route reaches this.

    Same shape as `apply_payment`: the UPDATE is a relative delta, so the stored
    `past_due` composes correctly with a concurrent write, and only the returned
    figure can be stale. It used to be described as racing with `apply_payment`
    under D3; the two touch different columns and neither loses an update.

    Authorisation is not this function's any more, and not open: no route
    reaches it. `POST /accounts/{loan_id}/waive-fee` raises a proposal that moves
    nothing, and a different verified principal's approval writes the entry
    naming them (spec 0002, ADR 0011). *This paragraph read "no role check here,
    no approver, and the captured ledger entry names nobody (D8)", which
    described the live path until PRs #34/#35.*
    """
    rows = db.query("SELECT past_due FROM balances WHERE loan_id = %s", (loan_id,))
    past_due = rows[0]["past_due"] if rows else 0.0
    new_past_due = float(_to_decimal(past_due) - _to_decimal(amount))
    db.query(
        "UPDATE balances SET past_due = COALESCE(past_due, 0) - %s, updated_at = now() WHERE loan_id = %s",
        (amount, loan_id),
    )
    log.info("waived fee loan_id=%s past_due %s -> %s", loan_id, past_due, new_past_due)
    return new_past_due
