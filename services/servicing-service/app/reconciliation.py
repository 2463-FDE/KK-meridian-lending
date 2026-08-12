"""Reconciliation: compare what we recorded against what the processor settled.

D7. This module used to expose two totals and nothing else -- no schedule, no
history, no threshold, no failure. "Finance can eyeball a number" is not a
control, and the absence was silent: `/reconciliation/peek` returned 200 whether
reconciliation had happened yesterday or never.

What a control needs, and what this now has:

- **an execution path that runs on a schedule** -- `app.reconcile_job`, invoked by
  cron or a compose one-shot (see docs/runbook.md). Not an in-process scheduler:
  a job that dies with its web worker is a job nobody notices stopping;
- **a measured result** -- per-loan comparison, with a break count and a break
  value, not one global total;
- **a threshold that fails** -- `RECONCILIATION_BREAK_THRESHOLD`, exceeded means a
  non-zero exit and a metric, both observable from outside the process;
- **a record of every run** -- `reconciliation_runs` (db/migrations/0034), so
  "when did this last agree?" has an answer.

**Per loan, never one total.** A global sum is the check that reports "in balance"
while two loans are wrong by equal and opposite amounts. The same reasoning as
the ledger parity check in ADR 0010, for the same reason.

## What this cannot do, stated rather than discovered

**There is no per-transaction matching, because the join key is not stored.** The
settlement file identifies each capture by the processor's own reference
(`processor_ref`, e.g. `PR-100231`). `payments.authorization_id` holds a
*different* identifier minted by our own authorization call -- verified against
the live database: zero payment rows carry a `PR-` reference. So a payment cannot
be matched to a settlement line, and this compares per-loan totals instead.

That means a break is detected but not attributed: this control can say "loan 4471
disagrees by 99.99" and cannot say which capture is responsible. Closing that gap
needs `processor_ref` persisted on the payment at capture time, which is a change
to payment-service and its schema, and it is the obvious next piece of work.

**There is no alerting integration.** This build has no pager, no incident tool
and no alertmanager. What it has is a contract those things consume: a non-zero
exit code, a `reconciliation_runs` row, and Prometheus gauges on the existing
`/metrics` endpoint. Calling that "alerting" would be the overclaim this module is
being fixed for.
"""
import csv
import json
import logging
import os
from decimal import Decimal

from . import db
from .config import SETTLEMENT_FILE

log = logging.getLogger("servicing.reconciliation")

CENT = Decimal("0.01")

def threshold_from_env(env=None) -> Decimal:
    """Total absolute break value tolerated before a run is a breach.

    Zero by default: on a card processor's own settlement file there is no
    legitimate rounding difference, so any disagreement is a finding. Configurable
    because a real deployment reconciling against a file with known timing lag may
    need a band -- but the default has to be the strict one, or the control starts
    life tuned to pass.

    A function rather than an inline expression so it can be tested without
    reloading the module: a reload re-registers the Prometheus gauges below and
    raises `Duplicated timeseries`, which is a fragility not worth building a test
    on.

    An unparseable or negative value falls back to the STRICT default rather than
    to permissive. A typo in a deployment variable must not quietly widen the band
    a money control is judged against -- that failure would look like a clean
    system.
    """
    raw = (env or os.environ).get("RECONCILIATION_BREAK_THRESHOLD", "0")
    try:
        value = Decimal(raw)
    except Exception:  # noqa
        log.error("RECONCILIATION_BREAK_THRESHOLD is not a number (%r) -- "
                  "using the strict default of 0", raw)
        return Decimal("0")
    if value < 0:
        log.error("RECONCILIATION_BREAK_THRESHOLD is negative (%s) -- using 0", value)
        return Decimal("0")
    return value


BREAK_THRESHOLD = threshold_from_env()

#: A systemic break -- a settlement file for the wrong date, say -- would put
#: every loan in the report. The stored list is capped so one bad run cannot
#: write an unbounded row; the COUNT and VALUE are always complete.
MAX_RECORDED_BREAKS = 50

class _ReconciliationCollector:
    """Publishes the reconciliation state by READING reconciliation_runs.

    Gauges set by the job process would never appear here. `app.reconcile_job` is
    a separate process by design -- an in-process scheduler dies with its web
    worker -- so anything it sets in its own registry dies with it, and the API's
    /metrics would serve a permanent zero. A zero on
    `servicing_reconciliation_breaks` is indistinguishable from "clean", which is
    the exact confusion this control exists to end: it would report health for a
    job that has never run.

    So the metrics are derived from the table at scrape time. The table is the
    durable record; the gauges are a view of it, and there is no second copy to
    drift.

    A scrape must never take the service down, so a database failure yields no
    samples rather than an exception. Absent samples are what `absent()` alerts
    on in Prometheus; a 500 on /metrics just looks like the scrape target is
    broken.
    """

    def collect(self):
        from prometheus_client.core import GaugeMetricFamily

        breaks = GaugeMetricFamily(
            "servicing_reconciliation_breaks",
            "Loans whose ledger total disagreed with settlement on the last completed run")
        value = GaugeMetricFamily(
            "servicing_reconciliation_break_value",
            "Absolute money value of reconciliation breaks on the last completed run")
        ok = GaugeMetricFamily(
            "servicing_reconciliation_last_run_ok",
            "1 if the last completed run was within threshold, 0 otherwise")
        last_ok = GaugeMetricFamily(
            "servicing_reconciliation_last_success_timestamp",
            "Unix time of the last run that completed within threshold. "
            "ALERT ON STALENESS: a job that stops running produces no failures at all")

        try:
            rows = db.query(
                "SELECT outcome, breaks_found, break_value FROM reconciliation_runs "
                "WHERE finished_at IS NOT NULL ORDER BY started_at DESC LIMIT 1"
            )
            success = db.query(
                "SELECT EXTRACT(EPOCH FROM started_at) AS ts FROM reconciliation_runs "
                "WHERE outcome = 'ok' ORDER BY started_at DESC LIMIT 1"
            )
        except Exception as e:  # noqa
            log.error("could not read reconciliation state for /metrics (%s)",
                      type(e).__name__)
            return

        if rows:
            r = rows[0]
            breaks.add_metric([], float(r["breaks_found"]))
            value.add_metric([], float(r["break_value"]))
            ok.add_metric([], 1.0 if r["outcome"] == "ok" else 0.0)
        if success:
            last_ok.add_metric([], float(success[0]["ts"]))

        # No rows means the job has never completed. Emitting nothing is the
        # honest answer -- a fabricated 0 would read as a clean reconciliation.
        yield from (breaks, value, ok, last_ok)


def register_metrics(registry=None) -> None:
    """Called once at service start. Idempotent enough to survive a reimport."""
    from prometheus_client import REGISTRY

    target = registry or REGISTRY
    try:
        target.register(_ReconciliationCollector())
    except ValueError:                       # already registered
        pass


def ledger_total() -> float:
    """Kept: `/reconciliation/peek` still answers with it. It is a number to look
    at, which is all it ever was."""
    rows = db.query("SELECT COALESCE(SUM(amount), 0) AS total FROM payments")
    return float(rows[0]["total"]) if rows else 0.0


def settlement_total() -> float:
    if not os.path.exists(SETTLEMENT_FILE):
        return 0.0
    total = Decimal("0")
    with open(SETTLEMENT_FILE) as f:
        for row in csv.DictReader(f):
            amt = Decimal(row["amount"])
            total += amt if row["type"] == "capture" else -amt
    return float(total)


def _ledger_by_loan() -> dict[int, Decimal]:
    """Captured money per loan, as we recorded it.

    Only `auth_status = 'captured'`. A 'pending' row is an authorization that
    never confirmed and a 'failed' row never took money, so counting either would
    manufacture a break against a settlement file that is correct.
    """
    rows = db.query(
        "SELECT loan_id, COALESCE(SUM(amount), 0) AS total FROM payments "
        "WHERE auth_status = 'captured' AND loan_id IS NOT NULL "
        "GROUP BY loan_id"
    )
    return {int(r["loan_id"]): Decimal(str(r["total"])).quantize(CENT) for r in rows}


def _settlement_by_loan() -> dict[int, Decimal]:
    """Settled money per loan, as the processor reports it.

    A refund line subtracts, so this is net settled -- the same sign convention
    `_ledger_by_loan` produces, or the comparison would be meaningless.
    """
    totals: dict[int, Decimal] = {}
    if not os.path.exists(SETTLEMENT_FILE):
        raise FileNotFoundError(SETTLEMENT_FILE)
    with open(SETTLEMENT_FILE) as f:
        for row in csv.DictReader(f):
            loan_id = int(row["loan_id"])
            amount = Decimal(row["amount"])
            signed = amount if row["type"] == "capture" else -amount
            totals[loan_id] = (totals.get(loan_id, Decimal("0")) + signed).quantize(CENT)
    return totals


def compare() -> dict:
    """Per-loan comparison. Pure: reads, computes, records nothing.

    Every loan appearing on EITHER side is compared. A loan settled but not
    recorded is the worse direction -- money taken with no payment row behind it --
    and an outer comparison is the only way to see it. An inner join over our own
    payments would be a control that cannot detect the thing it exists for.
    """
    ledger = _ledger_by_loan()
    settlement = _settlement_by_loan()

    breaks = []
    for loan_id in sorted(set(ledger) | set(settlement)):
        ours = ledger.get(loan_id, Decimal("0.00"))
        theirs = settlement.get(loan_id, Decimal("0.00"))
        if ours != theirs:
            breaks.append({
                "loan_id": loan_id,
                "ledger": str(ours),
                "settlement": str(theirs),
                "difference": str(ours - theirs),
            })

    break_value = sum(
        (abs(Decimal(b["difference"])) for b in breaks), Decimal("0.00")
    ).quantize(CENT)

    return {
        "loans_compared": len(set(ledger) | set(settlement)),
        "breaks_found": len(breaks),
        "break_value": break_value,
        "breaks": breaks,
        "threshold": BREAK_THRESHOLD,
        "within_threshold": break_value <= BREAK_THRESHOLD,
    }


def run_and_record() -> dict:
    """Run the comparison, record it, publish the metrics. Never raises.

    Returns the result with an `outcome` of 'ok', 'breach' or 'error'. The caller
    decides what to do about it -- `app.reconcile_job` turns it into an exit code.

    'breach' and 'error' stay distinct all the way through. A breach is a finding
    about the money; an error is a finding about the control, and a control that
    cannot run must not look like one that ran and found nothing.
    """
    run_id = _start_run()
    try:
        result = compare()
    except Exception as e:  # noqa
        code = type(e).__name__
        log.error("reconciliation could not complete (%s)", code)
        _finish_run(run_id, outcome="error", error_code=code)
        return {"outcome": "error", "error_code": code, "run_id": run_id}

    outcome = "ok" if result["within_threshold"] else "breach"
    _finish_run(
        run_id,
        outcome=outcome,
        loans_compared=result["loans_compared"],
        breaks_found=result["breaks_found"],
        break_value=result["break_value"],
        breaks=result["breaks"][:MAX_RECORDED_BREAKS],
    )

    # No gauge writes here. This runs in its own process, so anything set in its
    # registry dies with it -- the durable record IS the metric, and the API
    # derives the gauges from it at scrape time (see _ReconciliationCollector).
    if outcome == "ok":
        log.info("reconciliation ok loans=%s", result["loans_compared"])
    else:
        # Loan ids and amounts only. That is what a break IS; there is nothing
        # about the cardholder here to leak.
        log.error(
            "reconciliation BREACH loans=%s breaks=%s value=%s threshold=%s",
            result["loans_compared"], result["breaks_found"],
            result["break_value"], BREAK_THRESHOLD,
        )

    return {**result, "outcome": outcome, "run_id": run_id}


def _start_run() -> int | None:
    """Record the attempt before doing the work.

    So a run that dies -- OOM, a killed container -- leaves a row with a NULL
    `finished_at` rather than no row at all. A control whose failures are
    invisible is the defect being fixed, and "no row" is exactly how it looked.
    """
    try:
        rows = db.query(
            "INSERT INTO reconciliation_runs (outcome, threshold_value) "
            "VALUES ('error', %s) RETURNING id",
            (BREAK_THRESHOLD,),
        )
        return int(rows[0]["id"]) if rows else None
    except Exception as e:  # noqa
        log.error("could not record the start of a reconciliation run (%s)",
                  type(e).__name__)
        return None


def _finish_run(run_id, *, outcome, loans_compared=0, breaks_found=0,
                break_value=Decimal("0.00"), breaks=(), error_code=None) -> None:
    if run_id is None:
        return
    try:
        db.query(
            "UPDATE reconciliation_runs SET finished_at = now(), outcome = %s, "
            "loans_compared = %s, breaks_found = %s, break_value = %s, "
            "breaks = %s::jsonb, error_code = %s WHERE id = %s",
            (outcome, loans_compared, breaks_found, break_value,
             json.dumps(list(breaks)), error_code, run_id),
        )
    except Exception as e:  # noqa
        log.error("could not record the result of reconciliation run %s (%s)",
                  run_id, type(e).__name__)


def last_successful_run():
    """When reconciliation last agreed. None means never -- which is the honest
    answer for a system that has just deployed this, and the answer the old
    `peek` endpoint could not give at all."""
    rows = db.query(
        "SELECT id, started_at, finished_at, loans_compared FROM reconciliation_runs "
        "WHERE outcome = 'ok' ORDER BY started_at DESC LIMIT 1"
    )
    return rows[0] if rows else None


def recent_failures(limit: int = 10):
    rows = db.query(
        "SELECT id, started_at, outcome, breaks_found, break_value, error_code "
        "FROM reconciliation_runs WHERE outcome <> 'ok' "
        "ORDER BY started_at DESC LIMIT %s",
        (limit,),
    )
    return rows
