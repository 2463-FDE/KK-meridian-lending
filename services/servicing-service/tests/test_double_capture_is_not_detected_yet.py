"""Two settled captures on one loan raise no break. Stated, not implied.

`docs/DEBT.md` D22. The client reported the shape at the 2026-08-19 demo: two
captures for the same loan and the same amount, both settled, inside a short
window. Reconciliation reports a clean run on it, and that is not a bug in the
comparison -- each capture carries its own `processor_ref` and each matches its
own settlement line exactly. All four break kinds are about ONE reference:

  * `settlement_only`   -- a reference the processor settled and we never recorded
  * `ledger_only`       -- a reference we recorded and the processor never settled
  * `amount_mismatch`   -- both sides know the reference and disagree on the money
  * `unreferenced_capture` -- our capture carries no reference at all

Nothing compares two references to each other, so nothing can notice that the
same loan was funded twice.

**Whether it SHOULD is a product decision this repository will not make.** The
window, and what counts as a false positive, are the client's to set: a
legitimate repeat payment has exactly the same shape as a double-fund, and no
field in the data distinguishes them. D22 records the question, the owner and
the follow-up.

So this file is a tripwire, not a control. It pins today's behaviour and cites
the deferral. If someone builds detection without settling the decision, these
tests fail and the failure points at the entry that has to be updated -- which
is the whole reason to write a deferral down rather than to leave a gap
undescribed.

The seeded shape here is the one to reuse when the fifth break kind is built:
it is already the demo scenario, and a detection test should turn these
assertions round rather than invent a new fixture.
"""
import csv
from decimal import Decimal

import pytest

from app import reconciliation

LOAN = 4471
AMOUNT = "250.00"
# Two captures, same loan, same amount, same day, different references -- which
# is what a double-fund looks like in the data, and also what a borrower paying
# the same amount twice on purpose looks like.
FIRST_REF = "PR-100231"
SECOND_REF = "PR-100232"


def _settlement(tmp_path, rows):
    path = tmp_path / "settlement.csv"
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["settlement_date", "processor_ref",
                                          "loan_id", "amount", "type"])
        w.writeheader()
        for ref, amount in rows:
            w.writerow({"settlement_date": "2026-06-01", "processor_ref": ref,
                        "loan_id": LOAN, "amount": amount, "type": "capture"})
    return str(path)


class _Db:
    def __init__(self, ledger_rows):
        self.ledger_rows = ledger_rows
        self.finished = []
        self._next_id = 1

    def query(self, sql, params=None):
        flat = " ".join(sql.split())
        if flat.startswith("INSERT INTO reconciliation_runs"):
            row = {"id": self._next_id}
            self._next_id += 1
            return [row]
        if flat.startswith("UPDATE reconciliation_runs"):
            self.finished.append((flat, params))
            return []
        if "FROM reconciliation_runs" in flat:
            return []
        if "COUNT(*)" in flat and "FROM payments" in flat:
            return [{"n": 0}]
        if "FROM payments" in flat:
            return self.ledger_rows
        return []


@pytest.fixture
def double_capture(monkeypatch, tmp_path):
    """Both captures recorded by us AND settled by the processor.

    This is the honest version of the scenario. A test that recorded only one
    side would produce a `settlement_only` break and look like detection while
    proving the opposite -- the break would be about a missing payment row, not
    about the loan being funded twice.
    """
    db = _Db([
        {"loan_id": LOAN, "processor_ref": FIRST_REF, "total": Decimal(AMOUNT)},
        {"loan_id": LOAN, "processor_ref": SECOND_REF, "total": Decimal(AMOUNT)},
    ])
    monkeypatch.setattr(reconciliation, "SETTLEMENT_FILE",
                        _settlement(tmp_path, [(FIRST_REF, AMOUNT), (SECOND_REF, AMOUNT)]))
    monkeypatch.setattr(reconciliation, "db", db)
    monkeypatch.setattr(reconciliation, "BREAK_THRESHOLD", Decimal("0"))
    return db


def test_the_fixture_really_is_two_settled_captures_on_one_loan(double_capture):
    """Guard the guard. If the run compared one reference, or none, every
    assertion below would hold for the wrong reason."""
    result = reconciliation.compare()

    assert result["references_compared"] == 2
    assert result["loans_compared"] == 1
    assert result["unreferenced_captures"] == 0
    assert result["out_of_scope_captures"] == 0


def test_a_double_fund_produces_no_break_today(double_capture):
    """The gap itself, as an assertion rather than a sentence in a document.

    Fails the day detection is built -- deliberately. Update D22 and turn this
    round; do not delete it.
    """
    result = reconciliation.compare()

    assert result["breaks_found"] == 0, (
        "reconciliation now raises %r on two settled captures for one loan. If "
        "that is the fifth break kind, docs/DEBT.md D22 is out of date: settle "
        "the window and the false-positive appetite there, then replace this "
        "test with one that asserts the break." % (result["breaks"],)
    )
    assert result["break_value"] == Decimal("0.00")


def test_the_run_records_ok_on_a_double_fund(double_capture):
    """What an operator would actually see, which is the part that matters.

    Not merely "no break found" internally: the control writes a clean run and
    advances its own last-success timestamp. Anyone reading the reconciliation
    table after a double-fund sees a healthy day.
    """
    result = reconciliation.run_and_record()

    assert result["outcome"] == "ok"
    assert double_capture.finished, "the run recorded nothing"


def test_a_legitimate_repeat_payment_is_the_same_shape(double_capture):
    """Why this is a product decision and not an engineering one.

    A borrower who deliberately pays the same amount twice in one day produces
    byte-identical evidence to the double-fund above. Asserted here so the next
    reader does not have to take the claim on trust: whatever rule is built, it
    will flag both of these or neither, unless the client supplies a window and
    an appetite that separates them.
    """
    settled, _window, _identity = reconciliation._settlement_by_ref()
    ledger, unreferenced = reconciliation._ledger_by_ref(_window)

    assert not unreferenced
    assert set(settled) == {(LOAN, FIRST_REF), (LOAN, SECOND_REF)}
    assert set(ledger) == set(settled)
    # Same loan, same money, both sides agreeing on each reference. There is no
    # field left to distinguish intent.
    assert len({amount for amount in settled.values()}) == 1
