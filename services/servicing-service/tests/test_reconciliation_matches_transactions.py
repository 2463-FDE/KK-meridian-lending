"""Two offsetting defects on one loan must not cancel into a passing run.

The reported defect. Reconciliation aggregated both sides to one net amount per
loan and compared those. So an unrecorded processor capture of 99.99 and an
equally sized missing refund, both on loan 4471, produce exactly the totals a
correct day produces: `within_threshold` is true, the run records `outcome='ok'`,
`last_successful_run` advances and the Prometheus success timestamp is refreshed.

The control did not merely fail to notice. It published a success for having
netted its own errors away, which is worse than no control: the monitoring built
on top of it goes quiet in the way that means "healthy".

The fix is a join key. `payments.processor_ref` (db/migrations/0041) stores the
processor's own settlement reference, so both sides are keyed on the transaction
and `break_value` sums ABSOLUTE differences across references. Two findings can
no longer cancel by construction -- they add.

These tests state the scenario in both directions: that the per-loan totals
really are equal (so the old aggregation really would have passed), and that the
run is nonetheless a breach.
"""
import csv
import re
from decimal import Decimal

import pytest

from app import reconciliation
from app.reconcile_job import EXIT_BREACH, EXIT_ERROR, main


def _settlement(tmp_path, rows, name="settlement.csv"):
    """rows: [(loan_id, amount, type, processor_ref), ...] all on one day."""
    path = tmp_path / name
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["settlement_date", "processor_ref",
                                          "loan_id", "amount", "type"])
        w.writeheader()
        for loan_id, amount, kind, ref in rows:
            w.writerow({"settlement_date": "2026-06-01", "processor_ref": ref,
                        "loan_id": loan_id, "amount": amount, "type": kind})
    return str(path)


class _Db:
    """Returns the grouped payments rows and records what the run wrote."""

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
            # `_out_of_scope_captures`: captures the comparison excludes because
            # no processor was involved. These fakes model the processor-backed
            # ledger only, so the answer is zero -- and answering it explicitly
            # keeps this fake from handing the count query a list of ledger rows.
            return [{"n": 0}]
        if "FROM payments" in flat:
            return self.ledger_rows
        return []


def _recorded(db):
    """The last finish UPDATE as {column: value}.

    By NAME, not by position. An earlier version of this assertion indexed the
    parameter tuple from the end, and adding one column to the statement moved it
    silently onto a different value -- a test that keeps passing while checking
    something else is worse than one that fails.
    """
    sql, params = db.finished[-1]
    columns = re.findall(r"(\w+)\s*=\s*%s", sql)
    assert len(columns) == len(params), (
        f"parsed {len(columns)} columns from the finish statement but got "
        f"{len(params)} parameters"
    )
    return dict(zip(columns, params))


def _install(monkeypatch, settlement, db):
    monkeypatch.setattr(reconciliation, "SETTLEMENT_FILE", settlement)
    monkeypatch.setattr(reconciliation, "db", db)
    monkeypatch.setattr(reconciliation, "BREAK_THRESHOLD", Decimal("0"))


# The reviewer's exact scenario, on one loan.
#
# * PR-100244 -- the processor captured 99.99 and we have no payment row for it.
# * PR-100231 -- we recorded 250.00; the settlement file shows the capture AND a
#   refund of 99.99 against a reference we never recorded a refund for, so its
#   settled total is 150.01.
#
# Per loan: ours 250.00, theirs 99.99 + 250.00 - 99.99 = 250.00. Equal.
OFFSETTING_SETTLEMENT = [
    (4471, "250.00", "capture", "PR-100231"),
    (4471, "99.99", "capture", "PR-100244"),
    (4471, "99.99", "refund", "PR-100231"),
]
OFFSETTING_LEDGER = [
    {"loan_id": 4471, "processor_ref": "PR-100231", "total": Decimal("250.00")},
]


@pytest.fixture
def offsetting(monkeypatch, tmp_path):
    db = _Db(list(OFFSETTING_LEDGER))
    _install(monkeypatch, _settlement(tmp_path, OFFSETTING_SETTLEMENT), db)
    return db


def test_the_per_loan_totals_really_do_cancel(offsetting):
    """Establishes that this scenario is the defect, not a strawman.

    If the two sides did not agree per loan, the netting bug would not have
    reported ok on it and the breach below would prove nothing.
    """
    settlement, window, _identity = reconciliation._settlement_by_ref()
    ledger = reconciliation._ledger_by_loan(window)

    settled_for_the_loan = sum(
        (amount for (loan_id, _ref), amount in settlement.items() if loan_id == 4471),
        Decimal("0.00"),
    )
    assert settled_for_the_loan == Decimal("250.00")
    assert ledger[4471] == Decimal("250.00")


def test_two_offsetting_defects_cannot_produce_a_successful_run(offsetting):
    """The regression. Restore per-loan aggregation and this returns 'ok'."""
    result = reconciliation.run_and_record()

    assert result["outcome"] == "breach", (
        "two wrong transactions on one loan cancelled and the run reported "
        "%r -- the control netted its own errors away and published a success "
        "for it" % result["outcome"]
    )
    assert result["breaks_found"] == 2, (
        "expected both defects to surface separately, got %s" % (result["breaks"],)
    )
    assert result["break_value"] == Decimal("199.98"), (
        "the two differences were netted rather than summed as absolute values"
    )


def test_the_offsetting_run_fails_the_job(offsetting):
    """Observable outside the process, which is what makes it a control."""
    assert main() == EXIT_BREACH


def test_each_break_names_the_transaction_and_its_direction(offsetting):
    """A break that names only the loan cannot be investigated: the operator has
    to re-derive which capture is responsible, which is what the per-loan
    comparison forced and why breaks went unread."""
    result = reconciliation.run_and_record()
    by_ref = {b["processor_ref"]: b for b in result["breaks"]}

    assert set(by_ref) == {"PR-100231", "PR-100244"}
    # We recorded 250.00 against a reference the file settles at 150.01.
    assert by_ref["PR-100231"]["kind"] == "amount_mismatch"
    assert by_ref["PR-100231"]["difference"] == "99.99"
    # The processor settled money we have no payment row for at all.
    assert by_ref["PR-100244"]["kind"] == "settlement_only"
    assert by_ref["PR-100244"]["ledger"] == "0.00"


def test_the_recorded_run_carries_the_transaction_level_counts(offsetting):
    """The evidence a later reader needs to tell a fine comparison from a coarse
    one: a run over many loans and no references compared totals."""
    reconciliation.run_and_record()

    recorded = _recorded(offsetting)
    assert recorded["outcome"] == "breach"
    assert recorded["references_compared"] == 2, (
        "the run did not record how many references it compared, so a later "
        "reader cannot tell a transaction-level run from the per-loan one it "
        "replaced"
    )
    assert recorded["unreferenced_captures"] == 0
    assert recorded["out_of_scope_captures"] == 0


# --- legitimate netting must survive -----------------------------------------

def test_a_capture_and_its_own_refund_still_net_within_one_reference(monkeypatch, tmp_path):
    """Guard the guard.

    Netting ACROSS references was the defect. Netting WITHIN one reference is a
    single transaction's own history: a capture that was refunded settles at
    zero, and we have no payment row for it either. An implementation that
    refused all netting would report this correct day as two breaks.
    """
    db = _Db([])
    _install(monkeypatch, _settlement(tmp_path, [
        (4471, "250.00", "capture", "PR-100231"),
        (4471, "250.00", "refund", "PR-100231"),
        (5582, "410.50", "capture", "PR-100232"),
    ]), db)
    db.ledger_rows = [
        {"loan_id": 5582, "processor_ref": "PR-100232", "total": Decimal("410.50")},
    ]

    result = reconciliation.run_and_record()

    assert result["outcome"] == "ok", "breaks: %s" % (result["breaks"],)


def test_a_matching_day_is_still_clean(monkeypatch, tmp_path):
    """The other half of guarding the guard: a comparison keyed so finely that
    nothing ever matches would pass every test above and fail every real day."""
    db = _Db([
        {"loan_id": 4471, "processor_ref": "PR-100231", "total": Decimal("250.00")},
        {"loan_id": 5582, "processor_ref": "PR-100232", "total": Decimal("410.50")},
    ])
    _install(monkeypatch, _settlement(tmp_path, [
        (4471, "250.00", "capture", "PR-100231"),
        (5582, "410.50", "capture", "PR-100232"),
    ]), db)

    result = reconciliation.run_and_record()

    assert result["outcome"] == "ok", "breaks: %s" % (result["breaks"],)
    assert result["references_compared"] == 2


def test_the_same_reference_on_a_different_loan_is_two_breaks(monkeypatch, tmp_path):
    """A capture recorded against the wrong loan. The money is right in total and
    on the wrong borrower's balance, which per-loan netting across the two loans
    would show as two breaks only by accident and a global total would hide
    entirely."""
    db = _Db([
        {"loan_id": 5582, "processor_ref": "PR-100231", "total": Decimal("250.00")},
    ])
    _install(monkeypatch, _settlement(tmp_path, [
        (4471, "250.00", "capture", "PR-100231"),
    ]), db)

    result = reconciliation.run_and_record()

    assert result["outcome"] == "breach"
    kinds = {(b["loan_id"], b["kind"]) for b in result["breaks"]}
    assert kinds == {(4471, "settlement_only"), (5582, "ledger_only")}


# --- captures with no reference are reported, never skipped ------------------

def test_an_unreferenced_capture_is_a_break_not_a_silent_pass(monkeypatch, tmp_path):
    """A row captured before db/migrations/0041 carries no reference, so no
    settlement line can corroborate it.

    Skipping it would understate our own side of the comparison and turn a known
    blind spot into a clean run -- the same shape of lie as the netting.
    """
    db = _Db([
        {"loan_id": 4471, "processor_ref": "PR-100231", "total": Decimal("250.00")},
        {"loan_id": 4471, "processor_ref": None, "total": Decimal("75.00")},
    ])
    _install(monkeypatch, _settlement(tmp_path, [
        (4471, "250.00", "capture", "PR-100231"),
    ]), db)

    result = reconciliation.run_and_record()

    assert result["outcome"] == "breach"
    assert result["unreferenced_captures"] == 1
    unreferenced = [b for b in result["breaks"] if b["kind"] == "unreferenced_capture"]
    assert len(unreferenced) == 1
    assert unreferenced[0]["ledger"] == "75.00"
    assert result["break_value"] == Decimal("75.00")


# --- a file that cannot be matched fails closed ------------------------------

def test_a_settlement_file_with_no_references_fails_rather_than_netting(monkeypatch, tmp_path):
    """The netting defect's own guard rail.

    A file whose lines carry no reference can only be compared per loan. Falling
    back to that quietly would restore the defect at exactly the moment the input
    is already known to be malformed, so the run is an ERROR -- a finding about
    the control, never an 'ok' and never a 'breach'.
    """
    path = tmp_path / "settlement.csv"
    path.write_text(
        "settlement_date,loan_id,amount,type\n"
        "2026-06-01,4471,250.00,capture\n",
        encoding="utf-8",
    )
    db = _Db([
        {"loan_id": 4471, "processor_ref": "PR-100231", "total": Decimal("250.00")},
    ])
    _install(monkeypatch, str(path), db)

    result = reconciliation.run_and_record()

    assert result["outcome"] == "error"
    assert result["error_code"] == "UnreferencedSettlementRows"
    assert main() == EXIT_ERROR


def test_one_unreferenced_line_is_enough_to_fail_the_file(monkeypatch, tmp_path):
    """Partial evidence presented as whole evidence is the failure mode. A file
    where most lines are matchable and one is not would otherwise report on the
    matchable ones and say nothing about the money it could not place."""
    path = tmp_path / "settlement.csv"
    path.write_text(
        "settlement_date,processor_ref,loan_id,amount,type\n"
        "2026-06-01,PR-100231,4471,250.00,capture\n"
        "2026-06-01,,4471,99.99,capture\n",
        encoding="utf-8",
    )
    db = _Db([
        {"loan_id": 4471, "processor_ref": "PR-100231", "total": Decimal("250.00")},
    ])
    _install(monkeypatch, str(path), db)

    result = reconciliation.run_and_record()

    assert result["outcome"] == "error"
    assert result["error_code"] == "UnreferencedSettlementRows"


def test_an_empty_file_is_still_reported_as_empty(monkeypatch, tmp_path):
    """Ordering: the new check must not take over the codes that route to
    different people. An empty feed is a feed problem, not a format problem."""
    path = tmp_path / "settlement.csv"
    path.write_text("settlement_date,processor_ref,loan_id,amount,type\n",
                    encoding="utf-8")
    _install(monkeypatch, str(path), _Db([]))

    result = reconciliation.run_and_record()

    assert result["error_code"] == "EmptySettlementFile"


# --- a row this parser cannot read is bad evidence, not money ----------------

def _raw_settlement(tmp_path, body, name="settlement.csv"):
    path = tmp_path / name
    path.write_text(
        "settlement_date,processor_ref,loan_id,amount,type\n" + body,
        encoding="utf-8",
    )
    return str(path)


@pytest.mark.parametrize("row,why", [
    ("2026-06-01,PR-1,4471,250.00,chargeback\n", "a type nobody taught this code about"),
    ("2026-06-01,PR-1,4471,250.00,\n", "a blank type"),
    ("2026-06-01,PR-1,4471,250.00,CAPTURE ADJUSTMENT\n", "a feed schema change"),
    ("2026-06-01,PR-1,4471,-250.00,refund\n", "a negative amount under a refund type"),
    ("2026-06-01,PR-1,4471,0.00,capture\n", "a zero amount"),
    ("2026-06-01,PR-1,4471,not-a-number,capture\n", "an unparseable amount"),
])
def test_a_row_whose_direction_cannot_be_established_fails_the_run(
        monkeypatch, tmp_path, row, why):
    """The reported defect: the sign came from `type == 'capture'` with
    EVERYTHING else treated as a refund.

    So each of these became negative money, flowed into `break_value`, and the
    threshold judged a number the parser had invented -- turning a feed-integrity
    problem into a money finding, in whichever direction the bad row happened to
    point.
    """
    db = _Db([{"loan_id": 4471, "processor_ref": "PR-1", "total": Decimal("250.00")}])
    _install(monkeypatch, _raw_settlement(tmp_path, row), db)

    result = reconciliation.run_and_record()

    assert result["outcome"] == "error", (
        f"{why} was interpreted rather than refused, and the run reported "
        f"{result['outcome']!r} on money whose direction is unknown"
    )
    assert result["error_code"] == "MalformedSettlementRows"
    assert main() == EXIT_ERROR


def test_a_malformed_row_contributes_no_money_at_all(monkeypatch, tmp_path):
    """Not merely flagged: its amount must not reach any total. A run that failed
    the file AND accumulated the row would still be carrying an invented sign in
    the numbers a human then reads off the failed run."""
    db = _Db([])
    _install(monkeypatch, _raw_settlement(
        tmp_path,
        "2026-06-01,PR-1,4471,250.00,capture\n"
        "2026-06-01,PR-2,4471,999.00,chargeback\n",
    ), db)

    settlement, _window, identity = reconciliation._settlement_by_ref()

    assert identity["malformed_rows"] == 1
    assert identity["rows"] == 2, "the malformed row must still be counted as read"
    assert (4471, "PR-2") not in settlement
    assert settlement[(4471, "PR-1")] == Decimal("250.00")


def test_a_well_formed_refund_still_subtracts(monkeypatch, tmp_path):
    """Guard the guard. An allowlist that rejected refunds would pass every test
    above and break the one legitimate negative the file carries."""
    db = _Db([])
    _install(monkeypatch, _raw_settlement(
        tmp_path,
        "2026-06-01,PR-1,4471,250.00,capture\n"
        "2026-06-01,PR-1,4471,100.00,refund\n",
    ), db)

    settlement, _window, identity = reconciliation._settlement_by_ref()

    assert identity["malformed_rows"] == 0
    assert settlement[(4471, "PR-1")] == Decimal("150.00")


def test_the_type_is_matched_case_insensitively_and_trimmed(monkeypatch, tmp_path):
    """A feed that starts sending `Capture ` is a formatting change, not a new
    transaction kind, and failing the run on it would be the strictness that
    teaches operators to disable the control."""
    db = _Db([])
    _install(monkeypatch, _raw_settlement(
        tmp_path, "2026-06-01,PR-1,4471, 250.00 , Capture \n"), db)

    settlement, _window, identity = reconciliation._settlement_by_ref()

    assert identity["malformed_rows"] == 0
    assert settlement[(4471, "PR-1")] == Decimal("250.00")
