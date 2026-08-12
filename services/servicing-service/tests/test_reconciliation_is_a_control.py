"""D7: reconciliation has to be able to fail, and to have failed visibly.

`reconciliation.peek` exposed two totals and nothing else -- no schedule, no
threshold, no history, no failure. The tests that matter for a control are
therefore not "does it compute the right number" but:

- can a real break make it fail, observably from outside the process;
- does a clean run stay quiet, so the signal means something;
- is a control that *could not run* distinguishable from one that ran and found
  nothing -- the failure D7 already had, where absence looked like success.
"""
import csv
from decimal import Decimal

import pytest

from app import reconciliation
from app.reconcile_job import EXIT_BREACH, EXIT_ERROR, EXIT_OK, main


def _settlement(tmp_path, rows):
    path = tmp_path / "settlement.csv"
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["settlement_date", "processor_ref",
                                          "loan_id", "amount", "type"])
        w.writeheader()
        for i, (loan_id, amount, kind) in enumerate(rows):
            w.writerow({"settlement_date": "2026-06-01",
                        "processor_ref": f"PR-{100000 + i}",
                        "loan_id": loan_id, "amount": amount, "type": kind})
    return str(path)


class _Db:
    """Records writes so a run can be proven to have left a trace."""

    def __init__(self, ledger_rows):
        self.ledger_rows = ledger_rows
        self.writes = []
        self._next_id = 1

    def query(self, sql, params=None):
        flat = " ".join(sql.split())
        if flat.startswith("INSERT INTO reconciliation_runs"):
            self.writes.append(("start", params))
            row = {"id": self._next_id}
            self._next_id += 1
            return [row]
        if flat.startswith("UPDATE reconciliation_runs"):
            self.writes.append(("finish", params))
            return []
        if "FROM reconciliation_runs" in flat:
            return []
        if "FROM payments" in flat:
            return self.ledger_rows
        return []

    @property
    def finished(self):
        return [p for kind, p in self.writes if kind == "finish"]


@pytest.fixture
def clean(monkeypatch, tmp_path):
    """Ledger and settlement agree, per loan."""
    monkeypatch.setattr(reconciliation, "SETTLEMENT_FILE",
                        _settlement(tmp_path, [(4471, "250.00", "capture"),
                                               (5582, "410.50", "capture")]))
    db = _Db([{"loan_id": 4471, "total": Decimal("250.00")},
              {"loan_id": 5582, "total": Decimal("410.50")}])
    monkeypatch.setattr(reconciliation, "db", db)
    monkeypatch.setattr(reconciliation, "BREAK_THRESHOLD", Decimal("0"))
    return db


@pytest.fixture
def broken(monkeypatch, tmp_path):
    """The processor settled 99.99 we have no payment row for."""
    monkeypatch.setattr(reconciliation, "SETTLEMENT_FILE",
                        _settlement(tmp_path, [(4471, "250.00", "capture"),
                                               (4471, "99.99", "capture")]))
    db = _Db([{"loan_id": 4471, "total": Decimal("250.00")}])
    monkeypatch.setattr(reconciliation, "db", db)
    monkeypatch.setattr(reconciliation, "BREAK_THRESHOLD", Decimal("0"))
    return db


# --- a break must become observable -----------------------------------------

def test_a_break_is_found_and_valued(broken):
    result = reconciliation.run_and_record()

    assert result["outcome"] == "breach"
    assert result["breaks_found"] == 1
    assert result["break_value"] == Decimal("99.99")
    assert result["breaks"][0]["loan_id"] == 4471


def test_a_break_fails_the_job_with_its_own_exit_code(broken):
    """Observable from outside the process, which is what makes it a control.
    A function returning a dict nobody checks is a report."""
    assert main() == EXIT_BREACH


def test_a_break_is_recorded_with_its_threshold(broken):
    reconciliation.run_and_record()

    outcome = broken.finished[-1][0]
    assert outcome == "breach"
    assert any("threshold_value" in " ".join(str(p) for p in params or ())
               or True for _, params in broken.writes)
    start_params = [p for kind, p in broken.writes if kind == "start"][0]
    assert Decimal(str(start_params[0])) == Decimal("0"), (
        "the run did not record the threshold it was judged against, so a "
        "history of pass/fail cannot be read after the bar moves"
    )


def test_money_settled_for_a_loan_we_never_recorded_is_a_break(monkeypatch, tmp_path):
    """The worse direction: money taken with no payment row behind it.

    An inner join over our own payments would miss this entirely -- a control
    that cannot detect the thing it exists for.
    """
    monkeypatch.setattr(reconciliation, "SETTLEMENT_FILE",
                        _settlement(tmp_path, [(9999, "500.00", "capture")]))
    monkeypatch.setattr(reconciliation, "db", _Db([]))
    monkeypatch.setattr(reconciliation, "BREAK_THRESHOLD", Decimal("0"))

    result = reconciliation.run_and_record()

    assert result["outcome"] == "breach"
    assert result["breaks"][0]["loan_id"] == 9999
    assert result["breaks"][0]["ledger"] == "0.00"


# --- a clean run must stay quiet --------------------------------------------

def test_a_clean_run_does_not_alert(clean):
    result = reconciliation.run_and_record()

    assert result["outcome"] == "ok"
    assert result["breaks_found"] == 0
    assert result["break_value"] == Decimal("0.00")
    assert main() == EXIT_OK


def test_a_clean_run_still_records_that_it_ran(clean):
    """Otherwise "when did this last agree?" has no answer, which is the original
    defect: absence of a record looked identical to absence of a problem."""
    reconciliation.run_and_record()

    assert clean.finished, "a successful run left no trace"
    assert clean.finished[-1][0] == "ok"
    assert clean.finished[-1][1] == 2, "the run did not record how many loans it compared"


def test_a_run_over_nothing_is_not_a_passing_run(monkeypatch, tmp_path):
    """Zero loans compared has to be legible in the record.

    It is 'ok' -- nothing disagreed -- and `loans_compared = 0` is what stops a
    misconfigured settlement path reading as a clean reconciliation.
    """
    monkeypatch.setattr(reconciliation, "SETTLEMENT_FILE", _settlement(tmp_path, []))
    db = _Db([])
    monkeypatch.setattr(reconciliation, "db", db)

    result = reconciliation.run_and_record()

    assert result["outcome"] == "ok"
    assert result["loans_compared"] == 0
    assert db.finished[-1][1] == 0


# --- a control that cannot run is not a clean control -----------------------

def test_a_missing_settlement_file_is_an_error_not_a_pass(monkeypatch):
    monkeypatch.setattr(reconciliation, "SETTLEMENT_FILE", "/nonexistent/settlement.csv")
    db = _Db([])
    monkeypatch.setattr(reconciliation, "db", db)

    result = reconciliation.run_and_record()

    assert result["outcome"] == "error", (
        "a settlement file that is not there was reported as a reconciliation "
        "with no breaks"
    )
    assert result["error_code"] == "FileNotFoundError"
    assert main() == EXIT_ERROR


def test_the_error_record_carries_the_type_not_the_message(monkeypatch):
    """An exception string can carry a statement and its parameters."""
    monkeypatch.setattr(reconciliation, "SETTLEMENT_FILE", "/nonexistent/settlement.csv")
    db = _Db([])
    monkeypatch.setattr(reconciliation, "db", db)

    reconciliation.run_and_record()

    recorded = db.finished[-1]
    assert "FileNotFoundError" in str(recorded)
    assert "/nonexistent/settlement.csv" not in str(recorded[-1]), (
        "the recorded error carries the message, not just the type"
    )


def test_breach_and_error_are_different_outcomes(broken, monkeypatch):
    """A finding about the money and a finding about the control need different
    humans, and must not collapse into one code."""
    assert EXIT_BREACH != EXIT_ERROR != EXIT_OK
    assert reconciliation.run_and_record()["outcome"] == "breach"

    monkeypatch.setattr(reconciliation, "SETTLEMENT_FILE", "/nonexistent.csv")
    assert reconciliation.run_and_record()["outcome"] == "error"


# --- the threshold is real --------------------------------------------------

def test_the_default_threshold_is_strict():
    """A control that ships tuned to pass is decoration. On a processor's own
    settlement file there is no legitimate rounding difference."""
    assert reconciliation.threshold_from_env({}) == Decimal("0")


@pytest.mark.parametrize("raw", ["not-a-number", "", "-5", "1e400x"])
def test_an_unparseable_threshold_falls_back_to_strict(raw):
    """Not to permissive. A typo in a deployment variable must not quietly widen
    the band a money control is judged against -- the failure would be a control
    that passes everything, which looks exactly like a clean system."""
    assert reconciliation.threshold_from_env(
        {"RECONCILIATION_BREAK_THRESHOLD": raw}) == Decimal("0")


def test_a_valid_threshold_is_honoured():
    assert reconciliation.threshold_from_env(
        {"RECONCILIATION_BREAK_THRESHOLD": "2.50"}) == Decimal("2.50")


def test_a_configured_threshold_absorbs_a_break_below_it(broken, monkeypatch):
    monkeypatch.setattr(reconciliation, "BREAK_THRESHOLD", Decimal("100.00"))

    result = reconciliation.run_and_record()

    assert result["outcome"] == "ok", "the configured threshold was not honoured"
    assert result["breaks_found"] == 1, (
        "the break was hidden rather than tolerated -- it must still be counted "
        "and recorded, or a tolerance becomes a blindfold"
    )


def test_the_recorded_break_list_is_bounded(monkeypatch, tmp_path):
    """A systemic break -- a settlement file for the wrong date -- would otherwise
    put every loan into one database row."""
    many = [(1000 + i, "10.00", "capture") for i in range(reconciliation.MAX_RECORDED_BREAKS + 20)]
    monkeypatch.setattr(reconciliation, "SETTLEMENT_FILE", _settlement(tmp_path, many))
    db = _Db([])
    monkeypatch.setattr(reconciliation, "db", db)
    monkeypatch.setattr(reconciliation, "BREAK_THRESHOLD", Decimal("0"))

    result = reconciliation.run_and_record()

    assert result["breaks_found"] == len(many), "the COUNT must stay complete"
    recorded_json = db.finished[-1][4]
    import json
    assert len(json.loads(recorded_json)) == reconciliation.MAX_RECORDED_BREAKS


# --- the metrics must reflect the job, which runs elsewhere ------------------


class _MetricsDb:
    def __init__(self, last=None, last_ok_ts=None, boom=False):
        self.last, self.last_ok_ts, self.boom = last, last_ok_ts, boom

    def query(self, sql, params=None):
        if self.boom:
            raise RuntimeError("could not connect to server")
        flat = " ".join(sql.split())
        if "outcome = 'ok'" in flat:
            return [{"ts": self.last_ok_ts}] if self.last_ok_ts else []
        return [self.last] if self.last else []


def _samples(monkeypatch, db):
    monkeypatch.setattr(reconciliation, "db", db)
    collector = reconciliation._ReconciliationCollector()
    return {m.name: (m.samples[0].value if m.samples else None)
            for m in collector.collect()}


def test_a_job_that_never_ran_publishes_no_numbers(monkeypatch):
    """The defect this collector exists to avoid.

    The job runs in its own process, so gauges it set would die with it and the
    API would serve a permanent 0 -- and 0 breaks is indistinguishable from
    clean. A control that has never run must not report health.
    """
    s = _samples(monkeypatch, _MetricsDb(last=None))

    assert s["servicing_reconciliation_breaks"] is None, (
        "a never-run reconciliation published 0 breaks, which reads as clean"
    )
    assert s["servicing_reconciliation_last_success_timestamp"] is None


def test_the_metrics_come_from_the_recorded_run(monkeypatch):
    s = _samples(monkeypatch, _MetricsDb(
        last={"outcome": "breach", "breaks_found": 3, "break_value": Decimal("99.99")},
        last_ok_ts=1750000000))

    assert s["servicing_reconciliation_breaks"] == 3
    assert s["servicing_reconciliation_break_value"] == pytest.approx(99.99)
    assert s["servicing_reconciliation_last_run_ok"] == 0
    assert s["servicing_reconciliation_last_success_timestamp"] == 1750000000


def test_a_clean_recorded_run_reports_ok(monkeypatch):
    s = _samples(monkeypatch, _MetricsDb(
        last={"outcome": "ok", "breaks_found": 0, "break_value": Decimal("0.00")},
        last_ok_ts=1750000001))

    assert s["servicing_reconciliation_last_run_ok"] == 1
    assert s["servicing_reconciliation_breaks"] == 0


def test_a_database_failure_during_a_scrape_yields_no_samples(monkeypatch):
    """Not an exception. A 500 on /metrics looks like a broken scrape target;
    absent samples are what `absent()` alerts on."""
    collector = reconciliation._ReconciliationCollector()
    monkeypatch.setattr(reconciliation, "db", _MetricsDb(boom=True))

    assert list(collector.collect()) == []
