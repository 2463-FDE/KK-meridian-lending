"""Two settled captures on one loan raise no break -- by decision, not by omission.

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

**Whether it SHOULD was a product decision, and the client made it on
2026-08-24 -- the opposite way round from how this file was written to expect.**

The answer was not a fifth break kind. It was to FLAG the payment for human
reconciliation review and never to treat the flag as a duplicate conclusion, a
validity conclusion, or permission to move money. So the shape is detected now
-- `payment-service/app/review_signals.py` raises a signal on it, and a person
answers it in the in-app queue -- while **reconciliation still raises no break,
and that is the decided behaviour rather than a pending gap.**

Which means the assertions below are unchanged and still pass, and a reader must
not take that for "nobody noticed the double-fund". They say something narrower
and still worth saying: the COMPARISON is about one reference at a time, a break
means the books disagree, and two settled captures that each match their own
settlement line do not make the books disagree. Turning these round would assert
a break the client did not ask for.

What this file stopped being is a tripwire for an unmade decision. It is now the
statement of a boundary: if a fifth break kind ever does appear here, it was not
this decision that authorised it, and `docs/DEBT.md` D22 would need the new one
recorded before these tests are rewritten.
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


def test_a_double_fund_produces_no_break(double_capture):
    """The decided behaviour, as an assertion rather than a sentence in a
    document.

    Named `..._today` while it was pinning an unmade decision. The decision is
    made -- review, not a break -- so the "today" is gone and this asserts the
    answer rather than the wait.
    """
    result = reconciliation.compare()

    assert result["breaks_found"] == 0, (
        "reconciliation now raises %r on two settled captures for one loan. The "
        "client's decision of 2026-08-24 was that this shape is FLAGGED FOR "
        "HUMAN REVIEW and not raised as a break, so a break here contradicts "
        "docs/DEBT.md D22 rather than fulfilling it. If a later decision "
        "authorised one, record it in D22 first." % (result["breaks"],)
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
