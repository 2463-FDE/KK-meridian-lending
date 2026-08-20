"""Reconciliation reads, compares and records no card data.

Step 7 of `docs/PAN-CVV-DATA-FLOW.md`. The client's question at the 2026-08-19
demo was about the whole payment path, and reconciliation is the part of it that
is easiest to forget: it reads a file supplied by someone else, joins it against
`payments`, and writes what it found into a table an operator later reads. Three
chances for a card number to arrive somewhere nobody looked.

The settlement file is the interesting one, because it is EXTERNAL INPUT. This
service does not choose its columns. A processor that starts emitting a `pan`
column -- or an operator who exports one by mistake -- hands this parser a card
number, and a `csv.DictReader` reads every column it is given. So the test does
not ask "does our code mention a PAN"; it feeds a file that genuinely contains
one and then looks at everything the run produced.

Three surfaces, because they fail differently:

  1. the SQL this module sends (a `SELECT *` would pull columns that do not
     exist today but might tomorrow);
  2. the comparison result, which is what a break record is built from;
  3. the parameters written to `reconciliation_runs`, which outlive the run.

**Synthetic data only.** `4111111111111111` is the published Visa test number.
"""
import csv
import re
from decimal import Decimal

import pytest

from app import reconciliation

SYNTHETIC_PAN = "4111111111111111"
SYNTHETIC_CVV = "123"
PAN_FRAGMENTS = (SYNTHETIC_PAN, SYNTHETIC_PAN[:12], SYNTHETIC_PAN[:6])

# Substring matching, the same rule `db/tests/test_no_card_data_on_either_schema
# _path.py` uses on columns: `card_number`, `pan_encrypted` and `cvv2` are one
# defect wearing three names, and an exact-match check passes on a rename.
FORBIDDEN_COLUMN_SUBSTRINGS = ("pan", "cvv", "cvc", "card_number", "cardnumber",
                               "security_code")


def _assert_no_card_data(surface: str, where: str):
    for fragment in PAN_FRAGMENTS:
        assert fragment not in surface, (
            f"a card number reached {where} -- found {fragment!r}")
    lowered = surface.lower()
    for phrasing in ("cvv", "cvc", "security code"):
        if phrasing in lowered:
            index = lowered.index(phrasing)
            window = surface[index:index + 60]
            assert SYNTHETIC_CVV not in window, (
                f"a security code reached {where}: {window!r}")


class _Db:
    """The grouped payments rows, plus every statement the run sent."""

    def __init__(self, ledger_rows):
        self.ledger_rows = ledger_rows
        self.statements = []
        self.finished = []
        self._next_id = 1

    def query(self, sql, params=None):
        flat = " ".join(sql.split())
        self.statements.append((flat, params))
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


def _settlement_with_card_columns(tmp_path):
    """A settlement file that carries card data in columns nobody asked for.

    The four columns this control uses, plus three it does not. A processor
    feed is not under our control, so the case worth testing is the one where
    the file contains more than the contract.
    """
    path = tmp_path / "settlement.csv"
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "settlement_date", "processor_ref", "loan_id", "amount", "type",
            # Not part of the contract. Present anyway, which is the point.
            "pan", "cvv", "cardholder_name",
        ])
        w.writeheader()
        w.writerow({"settlement_date": "2026-06-01", "processor_ref": "PR-100231",
                    "loan_id": 4471, "amount": "250.00", "type": "capture",
                    "pan": SYNTHETIC_PAN, "cvv": SYNTHETIC_CVV,
                    "cardholder_name": "Rowan Fictional-Cardholder"})
        # A second reference that we never recorded, so the run produces a real
        # break record -- an empty break list would make every assertion below
        # pass without comparing anything.
        w.writerow({"settlement_date": "2026-06-01", "processor_ref": "PR-100244",
                    "loan_id": 4471, "amount": "99.99", "type": "capture",
                    "pan": SYNTHETIC_PAN, "cvv": SYNTHETIC_CVV,
                    "cardholder_name": "Rowan Fictional-Cardholder"})
    return str(path)


@pytest.fixture
def run(monkeypatch, tmp_path):
    db = _Db([{"loan_id": 4471, "processor_ref": "PR-100231",
               "total": Decimal("250.00")}])
    monkeypatch.setattr(reconciliation, "SETTLEMENT_FILE",
                        _settlement_with_card_columns(tmp_path))
    monkeypatch.setattr(reconciliation, "db", db)
    monkeypatch.setattr(reconciliation, "BREAK_THRESHOLD", Decimal("0"))
    return db


def test_the_sweep_would_catch_a_planted_value():
    """Guard the guard: the helper has to be able to fail."""
    with pytest.raises(AssertionError):
        _assert_no_card_data(f"loan 4471 {SYNTHETIC_PAN}", "a planted surface")


def test_the_file_really_does_contain_a_card_number(run):
    """And the fixture has to be the hard case, or nothing below is a test."""
    with open(reconciliation.SETTLEMENT_FILE) as f:
        assert SYNTHETIC_PAN in f.read()


def test_the_comparison_reads_no_card_column(run):
    """The SELECT list, not just this run's values.

    A `SELECT *` would satisfy every value assertion in this file today and
    start returning card columns the moment one was added back.
    """
    reconciliation.run_and_record()

    payments_reads = [sql for sql, _ in run.statements if "FROM payments" in sql]
    assert payments_reads, "the run never read the payments table"
    for sql in payments_reads:
        assert "SELECT *" not in sql, (
            "a wildcard select cannot promise what it returns")
        selected = sql.split("SELECT", 1)[1].split("FROM", 1)[0].lower()
        for forbidden in FORBIDDEN_COLUMN_SUBSTRINGS:
            assert forbidden not in selected, (
                f"reconciliation selects a {forbidden} column: {sql}")


def test_the_parsed_settlement_carries_nothing_but_money_and_keys(run):
    """The parser reads a file it does not control; it must keep only its own
    four columns."""
    totals, window, identity = reconciliation._settlement_by_ref()

    assert totals, "the file parsed to nothing, so this proves nothing"
    _assert_no_card_data(repr(totals), "the parsed settlement totals")
    _assert_no_card_data(repr(window), "the settlement window")
    _assert_no_card_data(repr(identity), "the settlement file identity")


def test_a_break_record_names_the_transaction_and_nothing_else(run):
    """A break is what an operator reads, and it is retained.

    The keys are asserted as an exact set rather than swept for card data: a
    break that gained a `last4` field would carry no card NUMBER and still put
    card data somewhere the data-flow statement says there is none.
    """
    result = reconciliation.compare()

    assert result["breaks"], "no break was produced, so no break was checked"
    for record in result["breaks"]:
        assert set(record) == {"loan_id", "processor_ref", "kind", "ledger",
                               "settlement", "difference"}
    _assert_no_card_data(repr(result), "the comparison result")


def test_the_recorded_run_carries_no_card_data(run):
    """What outlives the run: the row a later reader opens."""
    reconciliation.run_and_record()

    assert run.finished, "the run recorded no finish, so nothing was retained"
    for sql, params in run.finished:
        columns = re.findall(r"(\w+)\s*=\s*%s", sql)
        for column in columns:
            for forbidden in FORBIDDEN_COLUMN_SUBSTRINGS:
                assert forbidden not in column.lower(), (
                    f"the run records a {forbidden} column")
        _assert_no_card_data(repr(params), "a reconciliation_runs parameter")
