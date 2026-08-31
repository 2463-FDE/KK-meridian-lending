"""Delinquency + late fees.

The late fee here is the SMALLER of $35 and five per cent of the ARREARS
(`late_fee_for`). That was the published schedule until 2026-08-29 and is no
longer -- see the superseded-rule note below. It is still what this module
computes. For months only the flat figure was
implemented, which overcharged every borrower whose arrears were below $700,
because the flat fee is the larger of the two under that threshold. This
docstring called that "a policy question, not an arithmetic one" -- it was
arithmetic against a rule already published, and describing it as undecided is
what let it sit.

**A NEWER RULE HAS BEEN DECIDED AND IS NOT IMPLEMENTED HERE (`docs/DEBT.md`
D23).** As of 2026-08-29 the client's rule is at most ONE fee per missed
scheduled installment, after the existing grace period, priced at
`min($35, 5% x unpaid scheduled PRINCIPAL + INTEREST for THAT installment)`,
with previous late fees and all other fees excluded from the base. What this
module does instead is price off `balances.past_due` -- one projected total
mixing principal, interest and every fee already assessed -- and it applies no
per-installment cap at all.

That gap is deliberate and it is not a TODO. The decided rule needs facts this
schema does not hold: nothing records which installment a payment satisfied
(`payment_applications` stores one total per payment, `ledger_entries` a delta
per component, neither with a period), and nothing records which installment a
fee belongs to. Deriving "unpaid scheduled P&I for installment N" would mean
inventing an allocation order across installments and writing it down as though
it had been observed. D23 states the missing primitive, the smallest addition
that would close it, and why no backfill could be truthful.

So a reader should not take the comparison below for current policy. It is the
older published rule, still faithfully implemented, and knowingly superseded.
Approximating the new rule from `past_due` is specifically refused: a number
that resembles the decided rule without being it is worse than one that is
legibly the old one.

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

#: The SUPERSEDED arrears rule, which is what this module still computes:
#:     $35 flat, or 5% of the past-due amount, whichever is less
#:
#: `policies/fee_schedule.md` published exactly that until 2026-08-29. It no
#: longer does -- the table there now carries the decided installment-level
#: rule, and the arrears formula survives only in that file's "Current
#: implementation differs" section. So these two constants are no longer the
#: published policy; they are the older rule the code has not moved off yet
#: (`docs/DEBT.md` D23).
#:
#: Both halves are here because the rule is a comparison, and for months only
#: the flat figure was implemented. That is not a rounding nit: the flat fee is
#: the LARGER of the two whenever the past-due balance is below $700, so every
#: borrower under that threshold was charged more than even that rule allowed
#: -- up to $34.99 more on a small arrears balance.
LATE_FEE_FLAT = Decimal("35.00")
LATE_FEE_PCT_OF_PAST_DUE = Decimal("0.05")


class NoFeeIsDue(Exception):
    """The arrears rule yields a fee of zero, so there is nothing to post.

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
    """The superseded arrears rule, as one comparison.

    Not the published policy any more (`policies/fee_schedule.md` now publishes
    the installment-level rule); this is what the code computes, unchanged.

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


#: The DECIDED rule's percentage, applied to ONE installment's unpaid scheduled
#: principal and interest (`policies/fee_schedule.md`, client decision
#: 2026-08-29). Numerically equal to `LATE_FEE_PCT_OF_PAST_DUE` above and
#: deliberately a separate constant: the two rules differ in their BASE, not their
#: rate, and sharing one name would hide that. `db/tests` reads both out of this
#: module to check the published text against the code, so collapsing them would
#: also collapse the check.
LATE_FEE_PCT_OF_INSTALLMENT_PI = Decimal("0.05")


def late_fee_for_installment(unpaid_scheduled_pi) -> Decimal:
    """The DECIDED rule: `min($35, 5% x unpaid scheduled P&I for THAT installment)`.

    The client's rule of 2026-08-29 (`policies/fee_schedule.md`, `docs/DEBT.md`
    D23), as one comparison over one installment's own scheduled principal and
    interest.

    **This is not yet wired to the assessment route**, and that is the honest
    state of D23 rather than an oversight. Two facts the rule needs are still
    missing and neither can be invented here: the grace period it says a fee comes
    "after" does not exist anywhere in this repository, and the unpaid figure this
    function takes as input is only derivable while nothing has been paid
    (`installments.unpaid_scheduled_pi`, which refuses instead of guessing). The
    arithmetic lands now, with the client's own worked examples as tests, so that
    when those two decisions arrive the remaining change is wiring rather than
    arithmetic nobody has checked.

    **Fees are excluded from the base WHEN THE BASE COMES FROM THE SCHEDULE**, and
    that qualifier is doing real work rather than hedging. This function takes a
    `Decimal`; it cannot tell where the number came from, so the exclusion is a
    property of the CALLER, not of the arithmetic. Sourced from
    `installments.Installment.scheduled_pi` -- an amortization row's principal plus
    interest -- there is no fee in the input to exclude, so "previous late fees and
    all other fees are excluded" needs no filtering step. Handed
    `balances.past_due` instead, it would price the superseded rule and look
    identical while doing so.

    The claim used to read "by construction" without naming the source, which was
    stronger than the signature supports. `tests/test_late_fee_installment_rule.py`
    now walks the real chain -- stored contract, `installments_for`, one
    installment, `scheduled_pi`, this function -- so the exclusion is asserted end
    to end rather than asserted about a parameter.

    Contrast `late_fee_for` above, whose base IS `balances.past_due` and therefore
    includes every fee ever assessed.

    Rounding is ROUND_DOWN for the same reason as the superseded rule: "the lesser
    of" means the fee may exceed neither bound, and half-up rounding breaks that
    at the cap -- five per cent of $699.99 is $34.9995, which half-up bills as
    $35.00, half a cent above the percentage bound.

    The client's worked examples, supplied with the decision and asserted in
    `tests/test_late_fee_installment_rule.py`:

        $200 -> $10      (5% binds)
        $500 -> $25      (5% binds)
        $700 -> $35      (the two bounds meet exactly)
        $1000 -> $35     ($35 cap binds)
    """
    base = (unpaid_scheduled_pi if isinstance(unpaid_scheduled_pi, Decimal)
            else Decimal(str(unpaid_scheduled_pi)))
    if base <= 0:
        raise NoFeeIsDue(
            f"unpaid scheduled P&I is {base}; the schedule charges nothing")
    pct = (base * LATE_FEE_PCT_OF_INSTALLMENT_PI).quantize(CENT, rounding=ROUND_DOWN)
    fee = min(LATE_FEE_FLAT, pct)
    if fee <= 0:
        # Under $0.20 of unpaid scheduled P&I: five per cent is below a cent once
        # rounded down. `ledger_entries` refuses a zero amount by CHECK, so this
        # could not be written even if it meant something.
        raise NoFeeIsDue(
            f"unpaid scheduled P&I is {base}; five per cent of it rounds to {pct}")
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
    """Assess the late fee the ARREARS rule allows, and return `past_due`.

    The fee is the SMALLER of $35 and five per cent of the arrears
    (`late_fee_for`). That is the rule `policies/fee_schedule.md` published
    until 2026-08-29, and it is not what that file publishes now.
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

    What this still does NOT do is limit how often a fee may be assessed. Two
    sequential assessments on one day each price correctly and the ledger
    composes both deltas -- but nothing here refuses the second.

    That question is now DECIDED and this function does not implement the
    answer. Since 2026-08-29 the rule is one fee per missed scheduled
    installment, priced off that installment's unpaid scheduled principal and
    interest (`policies/fee_schedule.md`, `docs/DEBT.md` D23). Enforcing it
    needs a fact this schema does not hold -- which installment a fee belongs to
    -- so the guard cannot be written here yet, and the older published
    comparison is what runs. Not invented, not approximated from `past_due`.
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
