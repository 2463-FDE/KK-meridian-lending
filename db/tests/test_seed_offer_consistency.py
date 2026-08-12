"""Every seeded offer must be a disclosure a regulator could read.

The demo runs on seed data, so a seeded offer that fails a TILA relationship is
visible to anyone who clicks an application -- indistinguishable, on screen, from
a real defect in the calculation. Before this suite existed, 183 of 188 seeded
offers failed `amount financed + finance charge = total of payments`, the worst by
1,498.74, and none carried a note rate at all.

These are seed-data assertions, not unit tests of `apr.py`: the expected values
are recomputed here from principal / note rate / term rather than read back from
the same source that produced them. A stale APR literal in the seed lookup
therefore fails here rather than sitting unnoticed.

Independence note. The expected payment is derived from `applications.amount`,
`offers.note_rate_pct` and `applications.term_months` -- not from the stored
payment, and not from `loans.apr`. `loans.apr` is separately asserted to equal
the offer's note rate, so the servicing check is not "compare loans.apr against a
payment generated from loans.apr".
"""
import os
import pathlib
from decimal import Decimal, ROUND_HALF_UP, getcontext

import psycopg2
import pytest

getcontext().prec = 50

DATABASE_URL = os.getenv("DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL not set -- no Postgres to test against"
)

CENT = Decimal("0.01")
IDENTITY_TOLERANCE = Decimal("0.01")     # one cent, per the rounding boundary
PAYMENT_TOLERANCE = Decimal("0.01")
APR_TOLERANCE = Decimal("0.125")         # 12 CFR 1026.22(a)(1), regular transaction


def _payment_unrounded(principal: Decimal, note_rate_pct: Decimal, term: int) -> Decimal:
    """Level payment on the FULL principal at the note rate.

    Zero-rate loans take principal/term -- the amortization formula divides by
    zero when the monthly rate is zero, so it must never be reached for them.
    """
    i = note_rate_pct / 100 / 12
    if i == 0:
        return principal / term
    factor = (1 + i) ** term
    return principal * i * factor / (factor - 1)


def _actuarial_apr(amount_financed: Decimal, payment: Decimal, term: int) -> Decimal:
    """Solve for the rate where the payment stream's present value equals the
    amount financed. Bisection, independent of disclosure-service."""
    lo, hi = Decimal(0), Decimal(1)
    for _ in range(400):
        mid = (lo + hi) / 2
        pv = payment * (1 - (1 + mid) ** -term) / mid if mid > 0 else payment * term
        if pv > amount_financed:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2 * 12 * 100


def _actuarial_apr_from_sequence(amount_financed: Decimal, payments: list) -> Decimal:
    """Solve the rate over the ACTUAL Model B payment sequence.

    _actuarial_apr above assumes every payment is identical. Under Model B the
    final one is not, so that solve prices a cash flow nobody receives -- it is
    close enough to pass a loose tolerance, which is exactly why it is not good
    enough to verify a disclosed APR with. This one takes the real sequence and
    lets the seeded value be asserted to the last published digit.
    """
    lo, hi = Decimal(0), Decimal(1)
    for _ in range(400):
        mid = (lo + hi) / 2
        if mid > 0:
            pv = sum(p / (1 + mid) ** k for k, p in enumerate(payments, 1))
        else:
            pv = sum(payments)
        if pv > amount_financed:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2 * 12 * 100


def _model_b_payments(o: dict) -> list:
    """The stored contract as a payment sequence: count x regular, then final."""
    return (
        [Decimal(str(o["monthly_payment"]))] * int(o["regular_payment_count"])
        + [Decimal(str(o["final_payment"]))]
    )


SCHEMA = "seed_offer_consistency"
INIT_DIR = pathlib.Path(__file__).resolve().parents[1] / "init"
# Every file docker-compose's fresh Postgres volume runs, in filename order --
# the same set, so this suite validates the seeds as actually shipped.
INIT_FILES = (
    "001_schema.sql", "002_seed.sql", "003_seed_bulk.sql",
    "004_decision_events.sql", "005_manual_reviews.sql", "006_decision_attempts.sql", "007_ledger_opening_balances.sql",
)


@pytest.fixture(scope="module")
def seeded_db():
    """A throwaway schema built from db/init, not the live public schema.

    Reading `public` would mix in offers created by whatever ran against the
    database last (an e2e pass, a manual click-through) and would depend on the
    volume being freshly seeded. Building the schema here means this suite tests
    the seed DEFINITIONS, deterministically, on any Postgres.
    """
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
    conn.commit()
    for name in INIT_FILES:
        path = INIT_DIR / name
        if not path.exists():
            continue
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {SCHEMA}")
            # The seeds' own DO-block assertion raises here if a (fee, note rate,
            # term) combination has no verified APR in the lookup table.
            cur.execute(path.read_text())
        conn.commit()
    yield conn
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    conn.commit()
    conn.close()


@pytest.fixture(scope="module")
def seeded_offers(seeded_db):
    """Every seeded offer joined to its application terms and boarded loan."""
    with seeded_db.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute(
            "SELECT o.app_id, a.amount, a.term_months, o.note_rate_pct, o.apr, "
            "       o.monthly_payment, o.amount_financed, o.finance_charge, "
            "       o.total_of_payments, o.fee_pct_used, l.apr AS loan_rate, "
            "       o.regular_payment_count, o.final_payment, o.schedule_version, "
            "       o.term_months AS offer_term_months "
            "FROM offers o "
            "JOIN applications a ON a.id = o.app_id "
            "LEFT JOIN loans l ON l.app_id = o.app_id "
            "ORDER BY o.app_id"
        )
        cols = [c.name for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    assert rows, "no seeded offers found -- the seed did not run"
    return rows


def test_the_seed_produced_offers_at_all(seeded_offers):
    """Guards the DO-block assertion in 003_seed_bulk.sql: a missing APR lookup
    value would silently seed fewer offers than there are funded loans."""
    assert len(seeded_offers) >= 184, f"only {len(seeded_offers)} offers seeded"


def test_every_offer_stores_a_note_rate_and_a_fee_snapshot(seeded_offers):
    """Both are required to board: accept_offer refuses rather than infer a
    contractual rate, and fee_pct_used is what proves which fee produced the
    disclosure."""
    missing_rate = [o["app_id"] for o in seeded_offers if o["note_rate_pct"] is None]
    missing_fee = [o["app_id"] for o in seeded_offers if o["fee_pct_used"] is None]
    assert not missing_rate, f"offers with no note_rate_pct: {missing_rate[:10]}"
    assert not missing_fee, f"offers with no fee_pct_used: {missing_fee[:10]}"


def test_note_rate_and_disclosed_apr_are_separate_values(seeded_offers):
    """Two different regulated figures. If a seed ever set them equal, the
    monotonicity assertion below would pass vacuously."""
    for o in seeded_offers:
        note, apr_v = Decimal(str(o["note_rate_pct"])), Decimal(str(o["apr"]))
        assert note != apr_v, (
            f"app {o['app_id']}: note rate and APR are both {note} -- with a "
            f"positive prepaid fee they cannot be equal"
        )


def test_a_positive_prepaid_fee_puts_the_apr_above_the_note_rate(seeded_offers):
    """Direction only. note_rate < APR is CORRECT here, not a defect: the
    borrower receives less than the principal the payments are priced on."""
    for o in seeded_offers:
        fee = Decimal(str(o["fee_pct_used"]))
        if fee <= 0:
            continue
        note, apr_v = Decimal(str(o["note_rate_pct"])), Decimal(str(o["apr"]))
        assert apr_v > note, (
            f"app {o['app_id']}: APR {apr_v} is not above note rate {note} "
            f"despite a {fee} prepaid fee"
        )


def test_the_payment_is_reproduced_from_principal_note_rate_and_term(seeded_offers):
    """Recomputed from the application's own amount and term -- not read back
    from the stored payment, and not from loans.apr."""
    for o in seeded_offers:
        expected = _payment_unrounded(
            Decimal(str(o["amount"])), Decimal(str(o["note_rate_pct"])), o["term_months"]
        ).quantize(CENT, ROUND_HALF_UP)
        stored = Decimal(str(o["monthly_payment"]))
        assert abs(stored - expected) <= PAYMENT_TOLERANCE, (
            f"app {o['app_id']}: stored payment {stored}, but {o['amount']} at "
            f"{o['note_rate_pct']}% over {o['term_months']}mo bills {expected}"
        )


def test_amount_financed_is_principal_less_the_fee(seeded_offers):
    for o in seeded_offers:
        principal, fee = Decimal(str(o["amount"])), Decimal(str(o["fee_pct_used"]))
        expected = (principal - principal * fee).quantize(CENT, ROUND_HALF_UP)
        stored = Decimal(str(o["amount_financed"]))
        assert abs(stored - expected) <= CENT, (
            f"app {o['app_id']}: amount financed {stored}, expected {expected} "
            f"({principal} less {fee} fee)"
        )


def test_finance_charge_equals_total_less_amount_financed(seeded_offers):
    """Which is also what makes the fee part of the cost of credit: interest
    alone would leave the prepaid fee out, the original defect."""
    for o in seeded_offers:
        expected = (
            Decimal(str(o["total_of_payments"])) - Decimal(str(o["amount_financed"]))
        ).quantize(CENT, ROUND_HALF_UP)
        stored = Decimal(str(o["finance_charge"]))
        assert abs(stored - expected) <= CENT, (
            f"app {o['app_id']}: finance charge {stored}, expected {expected}"
        )


def test_the_tila_box_foots_for_every_seeded_offer(seeded_offers):
    """amount financed + finance charge = total of payments, within one cent.
    183 of 188 rows failed this before the seeds were derived."""
    failures = []
    for o in seeded_offers:
        af = Decimal(str(o["amount_financed"]))
        fc = Decimal(str(o["finance_charge"]))
        tot = Decimal(str(o["total_of_payments"]))
        if abs(af + fc - tot) > IDENTITY_TOLERANCE:
            failures.append((o["app_id"], af, fc, tot, af + fc - tot))
    assert not failures, "TILA box does not foot for: " + "; ".join(
        f"app {a}: {x}+{y}={x + y} vs {t} (off {d})" for a, x, y, t, d in failures[:8]
    )


def test_the_disclosed_apr_is_actuarial_not_an_add_on_ratio(seeded_offers):
    """Re-derives every seeded APR from its own payment stream. This is what
    would have caught the original add-on formula.

    Kept at the Reg Z tolerance because it solves over a LEVEL payment, which
    Model B's cash flow is not. The exact check is the test below; this one
    remains as the coarse, independent statement that the stored value is an
    actuarial rate at all rather than a ratio.
    """
    for o in seeded_offers:
        af = Decimal(str(o["amount_financed"]))
        payment = _payment_unrounded(
            Decimal(str(o["amount"])), Decimal(str(o["note_rate_pct"])), o["term_months"]
        )
        expected = _actuarial_apr(af, payment, o["term_months"])
        stored = Decimal(str(o["apr"]))
        assert abs(stored - expected) <= APR_TOLERANCE, (
            f"app {o['app_id']}: stored APR {stored}, actuarially {expected:.4f} "
            f"-- outside the {APR_TOLERANCE}pp Reg Z tolerance"
        )


def test_every_seeded_apr_is_solved_from_its_own_model_b_cash_flow(seeded_offers):
    """The exact check: each stored APR re-solved over that row's ACTUAL payment
    sequence, and asserted to the published 3dp with no tolerance.

    A tolerance is the right thing for the Reg Z comparison above and the wrong
    thing here. These are seeded literals; there is no measurement error to
    absorb, so anything other than equality means the committed value and the
    generator disagree. A 0.125pp tolerance would happily accept an APR
    belonging to a different row -- which is precisely the failure mode the old
    (fee, rate, term) lookup had, since it gave every row in a group the same
    APR while their cash flows differ.
    """
    for o in seeded_offers:
        af = Decimal(str(o["amount_financed"]))
        expected = _actuarial_apr_from_sequence(af, _model_b_payments(o))
        stored = Decimal(str(o["apr"]))
        assert stored == expected.quantize(Decimal("0.001"), ROUND_HALF_UP), (
            f"app {o['app_id']}: stored APR {stored} but its own payment "
            f"sequence solves to {expected:.6f}. Regenerate with "
            f"python db/tools/regenerate_seed_offers.py --write"
        )


def test_every_seeded_offer_records_a_complete_model_b_schedule(seeded_offers):
    """No seeded row may be a legacy row.

    Legacy rows are legitimate in a deployed database -- 0030 does not
    back-fill -- but a FRESH volume has no history to be missing. A seeded offer
    without a stored schedule cannot be boarded, so the demo would present
    offers that refuse to fund.
    """
    incomplete = [
        o["app_id"] for o in seeded_offers
        if o["schedule_version"] is None or o["final_payment"] is None
        or o["regular_payment_count"] is None or o["offer_term_months"] is None
    ]
    assert not incomplete, f"{len(incomplete)} seeded offer(s) have no schedule: {incomplete[:5]}"
    assert all(o["schedule_version"] == "B1" for o in seeded_offers)


def test_the_stored_total_is_the_sum_of_the_stored_payments(seeded_offers):
    """count x regular + final == total_of_payments, for every row.

    This is the identity the pre-Model-B seed failed: it derived the total from
    the UNROUNDED payment times the term, so the total disagreed with the
    schedule printed beside it. Asserted from the stored schedule rather than
    recomputed from principal and rate, so it tests the row and not the
    generator.
    """
    for o in seeded_offers:
        total = Decimal(str(o["total_of_payments"]))
        summed = sum(_model_b_payments(o))
        assert summed == total, (
            f"app {o['app_id']}: payments sum to {summed} but total_of_payments "
            f"is {total}"
        )


def test_the_payment_count_agrees_with_the_offers_own_term(seeded_offers):
    """regular_payment_count + 1 == term_months. Also a CHECK constraint; here
    so a failure names the offending applications instead of aborting the seed
    on the first bad insert."""
    bad = [
        o["app_id"] for o in seeded_offers
        if int(o["regular_payment_count"]) + 1 != int(o["offer_term_months"])
    ]
    assert not bad, f"payment count contradicts the term for: {bad[:5]}"


def test_the_offer_term_matches_the_application_it_was_written_for(seeded_offers):
    """The contractual term and the requested term agree across the seed.

    They are separate columns because they answer different questions and a
    counteroffer could legitimately part them. Nothing in the seed is a
    counteroffer, so a divergence here means a generated row was filed against
    the wrong application.
    """
    mismatched = [
        o["app_id"] for o in seeded_offers
        if int(o["offer_term_months"]) != int(o["term_months"])
    ]
    assert not mismatched, f"offer term differs from the application term for: {mismatched[:5]}"


def test_the_seed_covers_exactly_the_expected_population(seeded_offers):
    """180 bulk rows and 4 curated anchors.

    Pinned as exact counts because both directions are bugs: fewer means the
    generated block fell out of step with the application formulas, and more
    means rows were seeded for applications that were never funded. An earlier
    count of 188 in this project's own notes was runtime e2e rows being mistaken
    for seed data, which is the mistake a `>=` assertion invites.
    """
    bulk = [o for o in seeded_offers if 7000 <= o["app_id"] <= 7299]
    curated = [o for o in seeded_offers if o["app_id"] < 7000]
    assert len(bulk) == 180, f"{len(bulk)} bulk offers, expected 180"
    assert len(curated) == 4, f"{len(curated)} curated offers, expected 4"
    assert sorted(o["app_id"] for o in curated) == [4471, 5582, 6011, 6014]


def test_each_bulk_apr_belongs_to_its_own_row_not_its_rate_term_group(seeded_offers):
    """The reason the (fee, rate, term) APR lookup was retired.

    Under Model B the final payment absorbs a residue whose size depends on the
    principal, so rows sharing a rate and term have different cash flows. This
    asserts the seeded APRs actually reflect that -- at least one group must
    contain more than one distinct APR. If they were all equal within a group,
    the data would have silently reverted to the old lookup's behaviour while
    every other test here still passed.
    """
    groups: dict = {}
    for o in seeded_offers:
        if not (7000 <= o["app_id"] <= 7299):
            continue
        key = (Decimal(str(o["note_rate_pct"])), int(o["offer_term_months"]))
        groups.setdefault(key, set()).add(Decimal(str(o["apr"])))
    multi = {k: v for k, v in groups.items() if len(v) > 1}
    assert multi, (
        "every (rate, term) group has a single APR -- the seed looks like it was "
        "generated from a per-group lookup rather than per application"
    )


def test_loans_apr_holds_the_note_rate_not_the_disclosed_apr(seeded_offers):
    """`loans.apr` is the contractual rate despite its name (D19). Asserted
    against the OFFER's note rate rather than inferred from a payment generated
    from loans.apr itself -- otherwise the check would be circular."""
    for o in seeded_offers:
        if o["loan_rate"] is None:
            continue
        loan_rate = Decimal(str(o["loan_rate"]))
        note = Decimal(str(o["note_rate_pct"]))
        apr_v = Decimal(str(o["apr"]))
        assert abs(loan_rate - note) <= Decimal("0.001"), (
            f"app {o['app_id']}: loans.apr is {loan_rate} but the offer's note "
            f"rate is {note} -- servicing would bill terms nobody disclosed"
        )
        assert abs(loan_rate - apr_v) > Decimal("0.001"), (
            f"app {o['app_id']}: loans.apr equals the disclosed APR {apr_v} -- "
            f"the disclosed APR must never reach servicing as a billing rate"
        )


def test_servicing_reproduces_the_disclosed_payment_from_the_boarded_rate(seeded_offers):
    """The property that actually matters to a borrower: whatever rate reached
    `loans`, amortizing it must bill the payment on the disclosure."""
    for o in seeded_offers:
        if o["loan_rate"] is None:
            continue
        billed = _payment_unrounded(
            Decimal(str(o["amount"])), Decimal(str(o["loan_rate"])), o["term_months"]
        ).quantize(CENT, ROUND_HALF_UP)
        disclosed = Decimal(str(o["monthly_payment"]))
        assert abs(billed - disclosed) <= PAYMENT_TOLERANCE, (
            f"app {o['app_id']}: servicing bills {billed} against a disclosed "
            f"{disclosed} at loans.apr={o['loan_rate']}"
        )
