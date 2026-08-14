"""Reconciliation round 2: compare the same period, and never claim ok unrecorded.

Two defects, both of which made the control report something other than what it
had actually established.

**The window.** The ledger side summed EVERY captured payment ever and compared it
against one settlement file. Against a daily file that makes any loan with history
look like a break -- and a control that reports a mismatch for almost every loan
reports nothing at all, because nobody can read it. On seeded data it produced 183
breaks and I reported that number as a real finding rather than as my own
comparison error.

**The evidence.** If the start record could not be written, the run proceeded
anyway and could return `ok` with nothing durable behind it. "When did this last
agree?" would then be answered by a run that left no trace, which is D7's original
defect reached from the other side.
"""
import csv
from decimal import Decimal

import pytest

from app import reconciliation
from app.reconcile_job import EXIT_ERROR, main


class _WindowDb:
    """Answers the ledger query the way Postgres would, honouring the window.

    The fake models the WHERE clause on purpose: the defect lives in it, and a
    fake that ignored the bounds would pass whether or not the code applied them.
    """

    def __init__(self, payments, start_fails=False, finish_fails=False):
        # [(loan_id, processor_ref, amount, "YYYY-MM-DD"), ...]
        self.payments = payments
        self.start_fails = start_fails
        self.finish_fails = finish_fails
        self.finished = []
        self._next_id = 1

    def query(self, sql, params=None):
        flat = " ".join(sql.split())
        if flat.startswith("INSERT INTO reconciliation_runs"):
            if self.start_fails:
                raise RuntimeError("permission denied for table reconciliation_runs")
            row = {"id": self._next_id}
            self._next_id += 1
            return [row]
        if flat.startswith("UPDATE reconciliation_runs"):
            if self.finish_fails:
                raise RuntimeError("could not write the result")
            self.finished.append(params)
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
            since = params[0] if params and len(params) > 0 else None
            until = params[1] if params and len(params) > 1 else None
            # GROUP BY loan_id, processor_ref, like the real query. Grouping on
            # the loan alone would net two transactions together inside the
            # fake, which is the exact defect the comparison was fixed for.
            totals = {}
            for loan_id, ref, amount, day in self.payments:
                if since and day < since:
                    continue
                if until and day > until:
                    continue
                key = (loan_id, ref)
                totals[key] = totals.get(key, Decimal("0")) + Decimal(amount)
            return [{"loan_id": loan_id, "processor_ref": ref, "total": total}
                    for (loan_id, ref), total in totals.items()]
        return []


def _settlement(tmp_path, rows, name="settlement.csv"):
    """rows: [(date, loan_id, amount, type, processor_ref), ...]"""
    path = tmp_path / name
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["settlement_date", "processor_ref",
                                          "loan_id", "amount", "type"])
        w.writeheader()
        for day, loan_id, amount, kind, ref in rows:
            w.writerow({"settlement_date": day, "processor_ref": ref,
                        "loan_id": loan_id, "amount": amount, "type": kind})
    return str(path)


def _install(monkeypatch, settlement, db):
    monkeypatch.setattr(reconciliation, "SETTLEMENT_FILE", settlement)
    monkeypatch.setattr(reconciliation, "db", db)
    monkeypatch.setattr(reconciliation, "BREAK_THRESHOLD", Decimal("0"))


# --- the false-mismatch regression ------------------------------------------

def test_a_daily_file_is_not_compared_against_lifetime_totals(monkeypatch, tmp_path):
    """The defect that produced 183 breaks on seeded data."""
    settlement = _settlement(tmp_path, [("2026-06-02", 4471, "250.00", "capture", "PR-1")])
    db = _WindowDb([
        (4471, "PR-0", "1000.00", "2026-05-01"),   # history, outside the window
        (4471, "PR-1", "250.00", "2026-06-02"),    # the day being reconciled
    ])
    _install(monkeypatch, settlement, db)

    result = reconciliation.run_and_record()

    assert result["outcome"] == "ok", (
        "a correct day was reported as a break (%s) -- the ledger side is not "
        "scoped to the settlement window" % (result.get("breaks"),)
    )
    assert result["window_start"] == "2026-06-02"
    assert result["window_end"] == "2026-06-02"


def test_a_normal_daily_file_reconciles(monkeypatch, tmp_path):
    settlement = _settlement(tmp_path, [
        ("2026-06-02", 4471, "250.00", "capture", "PR-1"),
        ("2026-06-02", 5582, "410.50", "capture", "PR-2"),
    ])
    db = _WindowDb([(4471, "PR-1", "250.00", "2026-06-02"),
                    (5582, "PR-2", "410.50", "2026-06-02"),
                    (4471, "PR-0", "999.00", "2026-01-01")])
    _install(monkeypatch, settlement, db)

    result = reconciliation.run_and_record()

    assert result["outcome"] == "ok"
    assert result["loans_compared"] == 2


def test_a_backfilled_file_spanning_days_uses_its_whole_range(monkeypatch, tmp_path):
    """The window is read from the file rather than configured, so the same
    command handles a daily file and a back-fill without a flag."""
    settlement = _settlement(tmp_path, [
        ("2026-06-01", 4471, "100.00", "capture", "PR-1"),
        ("2026-06-03", 4471, "150.00", "capture", "PR-2"),
    ])
    db = _WindowDb([
        (4471, "PR-1", "100.00", "2026-06-01"),
        (4471, "PR-2", "150.00", "2026-06-03"),
        (4471, "PR-3", "500.00", "2026-05-30"),    # before the range
        (4471, "PR-4", "700.00", "2026-06-05"),    # after it
    ])
    _install(monkeypatch, settlement, db)

    result = reconciliation.run_and_record()

    assert result["outcome"] == "ok", "breaks: %s" % (result.get("breaks"),)
    assert (result["window_start"], result["window_end"]) == ("2026-06-01", "2026-06-03")


def test_a_break_inside_the_window_is_still_found(monkeypatch, tmp_path):
    """Guards the guard: windowing must narrow the comparison, not disable it.

    An implementation that scoped the ledger to nothing would pass every test
    above and detect no break ever.
    """
    settlement = _settlement(tmp_path, [("2026-06-02", 4471, "250.00", "capture", "PR-1")])
    db = _WindowDb([(4471, "PR-1", "175.00", "2026-06-02")])
    _install(monkeypatch, settlement, db)

    result = reconciliation.run_and_record()

    assert result["outcome"] == "breach"
    assert result["break_value"] == Decimal("75.00")


# --- file identity ----------------------------------------------------------

def test_rerunning_the_same_file_is_recognisable(monkeypatch, tmp_path):
    settlement = _settlement(tmp_path, [("2026-06-02", 4471, "250.00", "capture", "PR-1")])
    db = _WindowDb([(4471, "PR-1", "250.00", "2026-06-02")])
    _install(monkeypatch, settlement, db)

    first = reconciliation.run_and_record()
    second = reconciliation.run_and_record()

    assert first["source"] == second["source"], "the same file produced two identities"
    assert first["source"]["sha256"]
    assert first["source"]["rows"] == 1
    assert len(db.finished) == 2, "the re-run was not recorded as its own run"


def test_a_different_file_has_a_different_identity(monkeypatch, tmp_path):
    a = _settlement(tmp_path, [("2026-06-02", 4471, "250.00", "capture", "PR-1")], "a.csv")
    b = _settlement(tmp_path, [("2026-06-02", 4471, "251.00", "capture", "PR-1")], "b.csv")
    db = _WindowDb([(4471, "PR-1", "250.00", "2026-06-02")])

    _install(monkeypatch, a, db)
    first = reconciliation.run_and_record()
    monkeypatch.setattr(reconciliation, "SETTLEMENT_FILE", b)
    second = reconciliation.run_and_record()

    assert first["source"]["sha256"] != second["source"]["sha256"]


def test_the_window_and_source_are_persisted(monkeypatch, tmp_path):
    settlement = _settlement(tmp_path, [("2026-06-02", 4471, "250.00", "capture", "PR-1")])
    db = _WindowDb([(4471, "PR-1", "250.00", "2026-06-02")])
    _install(monkeypatch, settlement, db)

    reconciliation.run_and_record()

    recorded = " ".join(str(p) for p in db.finished[-1])
    assert "2026-06-02" in recorded, "the run did not record the window it covered"
    assert "sha256" in recorded, "the run did not record which file it read"


# --- never report ok without durable evidence --------------------------------

def test_a_run_whose_start_record_fails_does_not_run_at_all(monkeypatch, tmp_path):
    settlement = _settlement(tmp_path, [("2026-06-02", 4471, "250.00", "capture", "PR-1")])
    db = _WindowDb([(4471, "PR-1", "250.00", "2026-06-02")], start_fails=True)
    _install(monkeypatch, settlement, db)

    result = reconciliation.run_and_record()

    assert result["outcome"] == "error", "a run with no record reported a result"
    assert result["error_code"] == "RunRecordUnavailable"
    assert main() == EXIT_ERROR, "the job exited zero without durable evidence"


def test_a_run_whose_result_cannot_be_recorded_is_not_ok(monkeypatch, tmp_path):
    """The same rule at the other end: the comparison succeeded and its result is
    not durable, so it must not be reported as success."""
    settlement = _settlement(tmp_path, [("2026-06-02", 4471, "250.00", "capture", "PR-1")])
    db = _WindowDb([(4471, "PR-1", "250.00", "2026-06-02")], finish_fails=True)
    _install(monkeypatch, settlement, db)

    result = reconciliation.run_and_record()

    assert result["outcome"] == "error"
    assert result["error_code"] == "RunRecordUnavailable"
    assert main() == EXIT_ERROR


def test_a_comparison_failure_is_still_distinct_from_a_recording_failure(monkeypatch):
    """Three outcomes, three meanings. A missing settlement file is a finding
    about the input; an unwritable record is a finding about the control."""
    monkeypatch.setattr(reconciliation, "SETTLEMENT_FILE", "/nonexistent/settlement.csv")
    db = _WindowDb([])
    monkeypatch.setattr(reconciliation, "db", db)

    result = reconciliation.run_and_record()

    assert result["outcome"] == "error"
    assert result["error_code"] == "FileNotFoundError"
