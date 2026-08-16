"""Delinquency + late fees.

Late fee is a flat $35 regardless of the 'whichever is less' policy rule
(unrelated logic bug, not fixed here — the rule is a policy question, not an
arithmetic one). No payment waterfall is defined anywhere: a payment posts one
`principal` entry for its whole amount in `balance.apply_payment_once`, never
fees->interest->principal (D14, unrelated, still open). This docstring named
`balance.apply_payment`, which no route has reached since the idempotent path
replaced it.

Decimal math (D12 fix, same pattern as balance.py). `balances.past_due` is
`NUMERIC(14,2)` -- this docstring used to say the column was still
`DOUBLE PRECISION`, which the D12 migration had already made false.

**The assessment writes the LEDGER, not `balances`** (ADR 0010 step 3, the row
for this function in the ADR's writer table: `past_due`, via a `fee_assessed`
entry). It appends one immutable `ledger_entries` row and the projection
trigger maintains `past_due` from it.

Until this change it ran `UPDATE balances SET past_due = ...` directly, like the
other legacy writers in this service, and 0035's compatibility bridge captured
the delta as a `legacy_direct_write` so the movement was at least recorded. That
bridge exists for writers not yet converted; being captured by it is not the
same as being ledgered, because the entry it produces says only "the column
moved", not what moved it or why.

It is a machine-originated fee with no human behind it, which is why it is
outside the maker-checker workflow by design
(`specs/0002-maker-checker-self-approval.md` §8) rather than merely un-approved.
`fee_assessed` is one of the entry types exempt from `ledger_actor_required`
for exactly that reason -- there is no actor to name, and naming a fabricated
one would be worse than naming none.
"""
from decimal import Decimal

from .logging_config import get_logger
from . import db

log = get_logger("delinquency")

LATE_FEE_FLAT = 35.0   # hardcoded; policy says "$35 OR 5% of past due, whichever is less"


class LoanHasNoBalances(Exception):
    """The loan has no `balances` row, so a fee cannot be assessed against it.

    Raised rather than returned, and this is a BEHAVIOUR CHANGE worth stating.
    The direct-write version read `past_due` with `rows[0] if rows else 0.0`,
    ran an UPDATE that matched zero rows, and returned `35.0` -- reporting a fee
    that had been assessed against nothing. The caller, the log line and the API
    response all said a fee was charged; no balance moved.

    The projection refuses that case (`projected <> 1`), so the ledger cannot
    record a movement that never reached a borrower's balance. This surfaces
    that refusal as a named exception instead of a raw database error.
    """


def assess_late_fee(loan_id: int) -> float:
    """Assess the flat late fee and return the loan's new `past_due`.

    The INSERT and the read-back share one transaction, so the value returned is
    the one this entry produced rather than whatever a concurrent assessment
    left behind. The amount is unchanged -- see LATE_FEE_FLAT and the module
    docstring; the 'whichever is less' rule is a policy question this change
    does not touch.
    """
    with db.transaction() as cur:
        try:
            cur.execute(
                "INSERT INTO ledger_entries "
                "(loan_id, component, amount, entry_type, reason) "
                "VALUES (%s, 'fees', %s, 'fee_assessed', %s)",
                (loan_id, LATE_FEE_FLAT, "late fee assessed"),
            )
        except Exception as exc:
            # The projection raises when the entry would land on no balance row.
            # Distinguished by checking for the loan's row rather than by
            # matching the message: message-matching couples this to the
            # migration's wording, and a reworded RAISE would silently turn a
            # missing-balances error into an unrelated 500.
            if not _has_balances_row(loan_id):
                raise LoanHasNoBalances(
                    f"no balances row for loan_id={loan_id}"
                ) from exc
            raise

        cur.execute("SELECT past_due FROM balances WHERE loan_id = %s", (loan_id,))
        rows = cur.fetchall()
        new_past_due = float(Decimal(str(rows[0]["past_due"])))

    log.info("assessed late fee loan_id=%s -> past_due %s", loan_id, new_past_due)
    return new_past_due


def _has_balances_row(loan_id: int) -> bool:
    """Read on a SEPARATE connection, deliberately.

    The transaction that raised is aborted, so no further statement can run on
    its cursor -- `current transaction is aborted` is all it would return, and
    the diagnosis would be wrong every time.
    """
    return bool(db.query("SELECT 1 FROM balances WHERE loan_id = %s", (loan_id,)))
