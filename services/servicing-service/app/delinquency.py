"""Delinquency + late fees.

The late fee follows its published schedule: the SMALLER of $35 and five per
cent of the arrears (`late_fee_for`). For months only the flat figure was
implemented, which overcharged every borrower whose arrears were below $700,
because the flat fee is the larger of the two under that threshold. This
docstring called that "a policy question, not an arithmetic one" -- it was
arithmetic against a rule already published, and describing it as undecided is
what let it sit.

A payment is applied fees -> accrued interest -> principal by
`waterfall.allocate` (D14, closed). This docstring named `balance.apply_payment`
as the payment path, which no route has reached since the idempotent path
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
from decimal import ROUND_DOWN, Decimal

from .logging_config import get_logger
from . import db

log = get_logger("delinquency")

CENT = Decimal("0.01")

#: `policies/fee_schedule.md`, the published schedule:
#:     Late payment fee | $35 flat, or 5% of the past-due amount, whichever is **less**
#:
#: Both halves are here because the rule is a comparison, and for months only
#: the flat figure was implemented. That is not a rounding nit: the flat fee is
#: the LARGER of the two whenever the past-due balance is below $700, so every
#: borrower under that threshold was charged more than the published schedule
#: allows -- up to $34.99 more on a small arrears balance.
LATE_FEE_FLAT = Decimal("35.00")
LATE_FEE_PCT_OF_PAST_DUE = Decimal("0.05")


class NoFeeIsDue(Exception):
    """The published rule yields a fee of zero, so there is nothing to post.

    Two ways to get here, and neither is an error in the caller's arithmetic:

      * the loan owes no arrears -- five per cent of nothing is nothing; or
      * the arrears are so small that five per cent is under a cent (anything
        below $0.20, since rounding is down).

    `ledger_entries` refuses a zero amount by CHECK, so this cannot be written
    even if it were meaningful. Refusing here names the reason; letting the
    insert fail would surface a constraint violation that says nothing about
    the fee schedule.
    """


def late_fee_for(past_due) -> Decimal:
    """The published rule, as one comparison.

    Returns the SMALLER of the flat fee and five per cent of the arrears, in
    whole cents.

    The percentage is quantized before the comparison, not after, so the figure
    compared is the figure charged.

    Rounding is ROUND_DOWN, and that is the rule's own arithmetic rather than a
    preference. "Whichever is less" means the fee may not exceed EITHER bound,
    and half-up rounding breaks that: five per cent of $699.99 is $34.9995,
    which half-up bills as $35.00 -- half a cent more than the percentage the
    schedule caps it at. Rounding down cannot exceed either figure. A test
    asserts the fee against both bounds independently, which is what caught
    this.
    """
    arrears = past_due if isinstance(past_due, Decimal) else Decimal(str(past_due))
    if arrears <= 0:
        raise NoFeeIsDue(f"past_due is {arrears}; the schedule charges nothing")
    pct = (arrears * LATE_FEE_PCT_OF_PAST_DUE).quantize(CENT, rounding=ROUND_DOWN)
    fee = min(LATE_FEE_FLAT, pct)
    if fee <= 0:
        # Arrears under $0.20: five per cent is under a cent once rounded
        # down. Found by
        # running the boundary rather than by reading the rule.
        raise NoFeeIsDue(
            f"past_due is {arrears}; five per cent of it rounds to {pct}")
    return fee


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
    """Assess the late fee the published schedule allows, and return `past_due`.

    The fee is the SMALLER of $35 and five per cent of the arrears
    (`late_fee_for`), which is what `policies/fee_schedule.md` has always said.
    The flat figure alone overcharged every borrower whose arrears were below
    $700.

    The arrears are read INSIDE the same transaction as the insert, and the row
    is LOCKED while it is read, because the amount now depends on it.

    The lock is not inherited caution -- it is this change's own debt. While the
    fee was a flat $35 it did not matter that two concurrent assessments read
    the same `past_due`: the amount did not depend on the number they read, so
    the concurrent result and the serialised result were identical ($35 twice,
    either way). Pricing off arrears breaks that equivalence. A posted fee
    raises `past_due` -- `project_ledger_entry` adds a `fees` entry straight
    onto it -- so serialised, the second assessment prices off the higher figure;
    unlocked and concurrent, both price off the stale lower one. That divergence
    did not exist before this commit, so it is not pre-existing debt to be waved
    at a future PR. `FOR UPDATE` makes the second assessment wait and re-read,
    which is what makes the two orders agree again.

    What this still does NOT do is decide whether a second assessment should
    happen at all. Two sequential assessments on one day now each price
    correctly, and the ledger composes both deltas; whether the schedule permits
    charging twice is a policy question `policies/fee_schedule.md` does not
    answer, and this function will not invent an answer to it.
    """
    with db.transaction() as cur:
        # FOR UPDATE: see the docstring. Locks this loan's balances row for the
        # rest of the transaction, so a concurrent assessment blocks here and
        # then reads the arrears this one leaves behind.
        cur.execute(
            "SELECT past_due FROM balances WHERE loan_id = %s FOR UPDATE", (loan_id,))
        rows = cur.fetchall()
        if not rows:
            raise LoanHasNoBalances(f"no balances row for loan_id={loan_id}")
        fee = late_fee_for(rows[0]["past_due"] or 0)

        cur.execute(
            "INSERT INTO ledger_entries "
            "(loan_id, component, amount, entry_type, reason) "
            "VALUES (%s, 'fees', %s, 'fee_assessed', %s)",
            (loan_id, fee, f"late fee assessed ({fee})"),
        )

        cur.execute("SELECT past_due FROM balances WHERE loan_id = %s", (loan_id,))
        new_past_due = float(Decimal(str(cur.fetchall()[0]["past_due"])))

    log.info("assessed late fee loan_id=%s -> past_due %s", loan_id, new_past_due)
    return new_past_due
