"""Reconciliation: compare what we recorded against what the processor settled.

D7. This module used to expose two totals and nothing else -- no schedule, no
history, no threshold, no failure. "Finance can eyeball a number" is not a
control, and the absence was silent: `/reconciliation/peek` returned 200 whether
reconciliation had happened yesterday or never.

What a control needs, and what this now has:

- **an execution path that runs on a schedule** -- `app.reconcile_job`, invoked by
  cron or a compose one-shot (see docs/runbook.md). Not an in-process scheduler:
  a job that dies with its web worker is a job nobody notices stopping;
- **a measured result** -- per-TRANSACTION comparison, keyed on the processor's
  own settlement reference, with a break count and a break value rather than one
  global total;
- **a threshold that fails** -- `RECONCILIATION_BREAK_THRESHOLD`, exceeded means a
  non-zero exit and a metric, both observable from outside the process;
- **a record of every run** -- `reconciliation_runs` (db/migrations/0034), so
  "when did this last agree?" has an answer.

**Per TRANSACTION, never per loan.** A global sum is the check that reports "in
balance" while two loans are wrong by equal and opposite amounts, and a per-loan
sum is the same check one level down.
 Comparing net totals per loan was the
previous behaviour and it was a hole in the middle of the control: two wrong
transactions on one loan cancel. An unrecorded capture of 99.99 and a missing
refund of 99.99 on loan 4471 produce exactly the totals a correct day produces,
so the run recorded `outcome = 'ok'` -- and published a success timestamp for
having netted its own errors away. Both sides are now keyed by the processor's
own settlement reference (`payments.processor_ref`, db/migrations/0041), so
those two defects surface as two breaks instead of none, and a break says
*which* capture is responsible rather than only which loan.

## What this cannot do, stated rather than discovered

**A capture written before migration 0041 has no reference, and cannot be
matched.** There was nothing to back-fill from -- `authorization_id` is minted by
our own authorization call and appears in no settlement file. Such a row is
reported as an `unreferenced_capture` break: money we recorded that no settlement
line can corroborate. It is NOT silently excluded, because excluding it would
understate our own side and hide a real difference. Those breaks are finite and
self-clearing: the window moves, and every capture written after 0041 carries the
reference.

**Only processor-backed captures are compared.** `payments` has a second live
writer -- servicing-service's legacy `POST /payments`, retired with D2 -- which called no processor
and therefore produces rows no settlement file can contain. Comparing them was a
category error that made this control breach on our own writes, permanently.
`capture_source` (db/migrations/0042) makes the provenance a stored fact, the
comparison reads `capture_source = 'processor'`, and everything excluded is
COUNTED on the run (`out_of_scope_captures`) so the narrowing is visible rather
than silent. That the legacy route moves a balance with no processor behind it at
all is D2, and it is not this control's to fix.

**A settlement file with no usable reference column cannot be reconciled at all,
and the run fails rather than falling back.** Per-loan netting is what this
control was fixed for; quietly reverting to it when the file is malformed would
reintroduce the defect at exactly the moment nobody is looking. The run records
`UnreferencedSettlementRows` and exits non-zero.

**There is no alerting integration.** This build has no pager, no incident tool
and no Alertmanager. What it has is a contract those things consume: a non-zero
exit code, a `reconciliation_runs` row, and Prometheus gauges on the existing
`/metrics` endpoint, plus the rules in `monitoring/alerts.yml` that a real
Alertmanager would route. Without an Alertmanager deployed, **nothing is
notified** -- the rules evaluate and fire in Prometheus and reach no human.
Calling that "alerting" would be the overclaim this module is being fixed for.
"""
import csv
import datetime as dt
import hashlib
import io
import json
import pathlib
import logging
import os
from decimal import Decimal, InvalidOperation

from . import db
from .config import SETTLEMENT_FILE

log = logging.getLogger("servicing.reconciliation")

CENT = Decimal("0.01")

#: The settlement row types this control understands, and the sign each one
#: contributes. An allowlist rather than "capture, else negative": every value
#: that is not exactly one of these is a row whose meaning is unknown, and a
#: money control must not guess the direction of money it cannot read.
_SETTLEMENT_SIGNS = {
    "capture": Decimal("1"),
    "refund": Decimal("-1"),
}


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


def _ledger_rows(window=(None, None)) -> list[dict]:
    """One row per (loan_id, processor_ref) we recorded as captured in the window.

    The window is the whole point. An earlier version summed every captured
    payment ever and compared it against one day's settlement file, so every loan
    with any history looked like a break -- a control that reports a mismatch for
    almost every loan reports nothing at all, because nobody can read it. That
    was not a subtle bug: it produced 183 breaks on seeded data and I read the
    number as a real finding.

    Bounds are inclusive on both ends and interpreted as calendar dates against
    `payments.captured_at` -- when the processor CONFIRMED the capture. A
    settlement file is produced from the processor's own view of the same period,
    so the two sides must be scoped by the same event.

    Not `created_at`, which was the previous behaviour and was wrong. That column
    is stamped at INSERT, while the row is still `auth_status = 'pending'` and
    before the processor has been called. An authorization that is slow, retried,
    or recovered after a crash can be created on one day and captured on the
    next, so a capture the processor settles on the 9th could carry a
    `created_at` of the 8th -- excluded from the 9th's window and reported as a
    money break when nothing is wrong.

    That is the worst kind of false positive for a money control. A reviewer who
    learns the breaks are usually spurious stops reading them, and a control
    nobody believes has stopped working.

    Only `auth_status = 'captured'`. A 'pending' row is an authorization that
    never confirmed and a 'failed' row never took money, so counting either would
    manufacture a break against a settlement file that is correct.

    Grouped by `processor_ref` as well as `loan_id`, because that is what makes
    the comparison transaction-level. Rows whose reference is NULL come back as
    their own group per loan and the caller reports them as
    `unreferenced_capture` breaks -- see `_ledger_by_ref`.
    """
    since, until = window
    # `capture_source = 'processor'` is the scope of this control, and leaving it
    # out was a real defect rather than a refinement.
    #
    # `payments` has a second live writer: servicing-service's legacy
    # `POST /payments`, which inserts with no `auth_status` at all and therefore
    # takes the column default of 'captured'. It calls no processor, so no
    # settlement file has ever contained a line for it -- and the previous
    # version of this query swept every one of those rows in and reported it as
    # an `unreferenced_capture` break. The control breached on our own writes,
    # permanently, over money it could not have corroborated under any
    # implementation.
    #
    # 'unknown' rows are excluded for the same reason and a weaker one: we cannot
    # show a processor was involved, and admitting a row to a money comparison on
    # the strength of missing evidence manufactures breaks. They are counted by
    # `_out_of_scope_captures` so the exclusion is reported rather than silent.
    sql = ("SELECT loan_id, processor_ref, COALESCE(SUM(amount), 0) AS total "
           "FROM payments "
           "WHERE auth_status = 'captured' AND loan_id IS NOT NULL "
           "AND capture_source = 'processor'")
    params: list = []
    # COALESCE for one reason only: rows captured before migration 0040 were
    # back-filled from created_at, and a row that somehow escaped the back-fill
    # must still be compared rather than silently dropped from the ledger side.
    # Dropping it would UNDERSTATE our total and produce a break in the other
    # direction -- a missing row is not a safe default for a money control.
    if since:
        sql += " AND COALESCE(captured_at, created_at) >= %s::date"
        params.append(since)
    if until:
        sql += " AND COALESCE(captured_at, created_at) < (%s::date + INTERVAL '1 day')"
        params.append(until)
    sql += " GROUP BY loan_id, processor_ref"
    return db.query(sql, tuple(params))


def _out_of_scope_captures(window=(None, None)) -> int:
    """Captures in the window that this control does not compare, counted.

    The rows `_ledger_rows` excludes: captures written by servicing-service's
    legacy `POST /payments` (no processor was called, so no settlement line can
    exist) and rows whose provenance predates db/migrations/0042 and cannot be
    established.

    That route is retired (D2), so this population is closed rather than growing
    -- but the rows it already wrote are real money history and are still counted
    and excluded here. Nothing about their handling changed with the deletion,
    which is deliberate: retiring a writer must not invalidate what it wrote.

    Counted rather than ignored. An exclusion nobody can see is how a comparison
    quietly narrows until it is comparing nothing -- the same failure the vacuity
    checks exist for, arriving through the WHERE clause instead of the file.
    """
    since, until = window
    sql = ("SELECT COUNT(*) AS n FROM payments "
           "WHERE auth_status = 'captured' AND loan_id IS NOT NULL "
           "AND capture_source <> 'processor'")
    params: list = []
    if since:
        sql += " AND COALESCE(captured_at, created_at) >= %s::date"
        params.append(since)
    if until:
        sql += " AND COALESCE(captured_at, created_at) < (%s::date + INTERVAL '1 day')"
        params.append(until)
    rows = db.query(sql, tuple(params))
    return int(rows[0]["n"]) if rows else 0


def _ledger_by_ref(window=(None, None)):
    """Captured money keyed by (loan_id, processor_ref), plus what cannot be keyed.

    Returns `(by_ref, unreferenced)`. `unreferenced` holds one entry per loan
    whose captured rows in this window carry no `processor_ref` -- rows written
    before migration 0041, or captured against a processor that reported no
    settlement reference. They are returned SEPARATELY rather than folded into
    `by_ref` under a None key, so the caller has to decide what to do about them
    instead of comparing them against a settlement line that cannot exist.
    """
    by_ref: dict[tuple[int, str], Decimal] = {}
    unreferenced: list[dict] = []
    for r in _ledger_rows(window):
        loan_id = int(r["loan_id"])
        total = Decimal(str(r["total"])).quantize(CENT)
        ref = (r.get("processor_ref") or "").strip()
        if not ref:
            unreferenced.append({"loan_id": loan_id, "amount": total})
            continue
        key = (loan_id, ref)
        by_ref[key] = (by_ref.get(key, Decimal("0")) + total).quantize(CENT)
    return by_ref, unreferenced


def _ledger_by_loan(window=(None, None)) -> dict[int, Decimal]:
    """Captured money per loan, as we recorded it, WITHIN THE SAME WINDOW.

    Derived from the same rows the transaction-level comparison reads, so the
    two can never disagree about what the window contained. Retained because
    `/reconciliation/peek` and the capture-window tests ask this question
    directly; the CONTROL no longer compares on it, because per-loan totals let
    two offsetting defects cancel (see the module docstring).
    """
    totals: dict[int, Decimal] = {}
    for r in _ledger_rows(window):
        loan_id = int(r["loan_id"])
        totals[loan_id] = (totals.get(loan_id, Decimal("0"))
                           + Decimal(str(r["total"]))).quantize(CENT)
    return totals


def _settlement_by_ref(path=None):
    """Settled money keyed by (loan_id, processor_ref), plus window and identity.

    Returns (totals, window, identity). The window is the min and max
    `settlement_date` the file actually contains -- so a daily file yields one
    day and a back-filled file yields the range it covers, without either being
    configured. The identity is what makes a run reproducible: which file, how
    many rows, and a digest, because "reconciliation passed" is worthless if
    nobody can say what it read.

    A refund line subtracts, so a reference that was captured and then refunded
    inside the same file nets to zero. That is legitimate netting -- it is one
    transaction's own history. Netting ACROSS references was the defect: two
    different broken transactions on one loan cancelled each other out.

    A row with no usable `processor_ref` is counted in `unreferenced_rows` and
    its money is deliberately NOT accumulated anywhere. There is no key to
    accumulate it under, and inventing one (loan-level, say) is precisely the
    fallback the vacuity check refuses: the run fails instead.
    """
    path = path or SETTLEMENT_FILE
    if not os.path.exists(path):
        raise FileNotFoundError(path)

    raw = pathlib.Path(path).read_bytes()
    totals: dict[tuple[int, str], Decimal] = {}
    dates: list[str] = []
    rows_read = 0
    undated = 0
    unreferenced = 0
    malformed = 0
    for row in csv.DictReader(io.StringIO(raw.decode("utf-8"))):
        rows_read += 1
        loan_id = int(row["loan_id"])

        # The date FIRST, and independently of everything below.
        #
        # A row with no usable settlement_date is COUNTED but cannot be SCOPED.
        # Undated rows used to be added to `totals` and skipped for the window,
        # so a file with one dated row and one undated row compared both rows'
        # money against a window derived from the dated one alone. An
        # unknown-period capture or refund could then invent a break or hide a
        # real one -- and the run still recorded a completed comparison, which
        # is the vacuous-success defect in a subtler form: partially scoped
        # evidence presented as whole evidence.
        #
        # Read before the money checks below, not after, because the date is a
        # property of the row whether or not its amount can be interpreted. When
        # this came last, a file whose only row had an unreadable type never
        # reached it, so the window came back empty and the run blamed
        # `IncompleteSettlementWindow` -- naming the wrong problem to the wrong
        # person while the actual defect was the type.
        settlement_date = (row.get("settlement_date") or "").strip()
        if not settlement_date:
            undated += 1
        else:
            try:
                dt.date.fromisoformat(settlement_date)
            except ValueError:
                undated += 1
            else:
                dates.append(settlement_date)

        # The sign comes from the row's TYPE, and the type has to be one this
        # control understands.
        #
        # This read `amount if row["type"] == "capture" else -amount`, so every
        # value that was not exactly 'capture' became a refund: a feed schema
        # change, a typo, a blank cell, a transaction kind nobody has taught this
        # code about. A feed-integrity problem was silently converted into
        # negative money, which then flowed into `break_value` and into the
        # comparison the threshold judges -- a fake break in one direction, and
        # in the other a real capture cancelling a real refund because the parser
        # inverted one of them.
        #
        # A row this cannot interpret is not money, it is bad evidence, and bad
        # evidence fails the run (`MalformedSettlementRows`). The amount must
        # also be positive, because the sign is the type's job: a negative amount
        # on a refund line would double-negate into a capture.
        kind = (row.get("type") or "").strip().lower()
        sign = _SETTLEMENT_SIGNS.get(kind)
        try:
            amount = Decimal(row["amount"])
        except (InvalidOperation, TypeError, ValueError):
            amount = None
        # And the amount has to be money this system can hold exactly.
        #
        # Every amount here is compared against `payments.amount`, which is
        # NUMERIC(14,2). A settlement row of 10.004 was accepted, rounded to
        # 10.00 by the quantize below, and matched a 10.00 capture -- so the run
        # recorded `ok` while the file carried four tenths of a cent this system
        # cannot represent, and cannot therefore have agreed with. Repeated
        # across a day's file that rounding is real money, and it is invisible
        # precisely because the control reports success.
        #
        # `amount != amount.quantize(CENT)` and not an exponent test: 10.000 is
        # a formatting variance and holds exactly in cents, while 10.004 does
        # not. The question is representability, not how the feed spells it.
        if (sign is None or amount is None or amount <= 0
                or amount != amount.quantize(CENT)):
            malformed += 1
            continue
        signed = amount * sign

        ref = (row.get("processor_ref") or "").strip()
        if ref:
            key = (loan_id, ref)
            totals[key] = (totals.get(key, Decimal("0")) + signed).quantize(CENT)
        else:
            unreferenced += 1

    window = (min(dates), max(dates)) if dates else (None, None)
    identity = {
        "file": os.path.basename(path),
        "rows": rows_read,
        # Surfaced so the vacuity check can fail the run rather than silently
        # comparing money it cannot place in a period.
        "undated_rows": undated,
        # Same, for the other axis: money the file cannot identify per
        # transaction. A run that met this and fell back to per-loan totals
        # would silently restore the netting defect.
        "unreferenced_rows": unreferenced,
        # Rows whose type is not in the allowlist, or whose amount is not a
        # positive number. Their money is accumulated nowhere -- the run fails
        # instead, because a parser that guesses at a row it cannot read turns a
        # feed problem into a money finding.
        "malformed_rows": malformed,
        "sha256": hashlib.sha256(raw).hexdigest()[:16],
    }
    return totals, window, identity


def compare() -> dict:
    """Transaction-level comparison. Pure: reads, computes, records nothing.

    Every (loan, processor reference) appearing on EITHER side is compared. A
    reference settled but not recorded is the worse direction -- money taken with
    no payment row behind it -- and an outer comparison is the only way to see
    it. An inner join over our own payments would be a control that cannot
    detect the thing it exists for.

    Keyed on the reference rather than on the loan, which is the fix for the
    netting defect: an unrecorded capture and an equally sized missing refund on
    the same loan now produce two breaks whose values ADD, where per-loan totals
    produced none at all. `break_value` sums absolute differences, so no two
    findings can cancel by construction.
    """
    settlement, window, identity = _settlement_by_ref()
    ledger, unreferenced = _ledger_by_ref(window)
    out_of_scope = _out_of_scope_captures(window)

    breaks = []
    for loan_id, ref in sorted(set(ledger) | set(settlement)):
        ours = ledger.get((loan_id, ref), Decimal("0.00"))
        theirs = settlement.get((loan_id, ref), Decimal("0.00"))
        if ours != theirs:
            breaks.append({
                "loan_id": loan_id,
                "processor_ref": ref,
                # Which side is missing, because the three cases need different
                # answers: money settled we never recorded is a possible
                # unrecorded charge, money recorded the processor never settled
                # is a possible phantom capture, and a differing amount on a
                # reference both sides know is a posting error.
                "kind": ("settlement_only" if ours == 0
                         else "ledger_only" if theirs == 0
                         else "amount_mismatch"),
                "ledger": str(ours),
                "settlement": str(theirs),
                "difference": str(ours - theirs),
            })

    # Captures we cannot match at all. Reported, never skipped: skipping them
    # would understate our own side of the comparison and turn a known blind
    # spot into a clean run.
    for row in sorted(unreferenced, key=lambda r: r["loan_id"]):
        breaks.append({
            "loan_id": row["loan_id"],
            "processor_ref": None,
            "kind": "unreferenced_capture",
            "ledger": str(row["amount"]),
            "settlement": "0.00",
            "difference": str(row["amount"]),
        })

    break_value = sum(
        (abs(Decimal(b["difference"])) for b in breaks), Decimal("0.00")
    ).quantize(CENT)

    loans = ({loan_id for loan_id, _ in ledger}
             | {loan_id for loan_id, _ in settlement}
             | {row["loan_id"] for row in unreferenced})

    return {
        "loans_compared": len(loans),
        # The number that says how fine the comparison actually was. A run with
        # loans_compared > 0 and references_compared == 0 compared nothing at
        # transaction level, which is what the vacuity check looks for.
        "references_compared": len(set(ledger) | set(settlement)),
        "unreferenced_captures": len(unreferenced),
        # Captures this control did not compare, because no processor was
        # involved or none can be shown to have been. Reported so the scope of
        # the comparison is legible next to its result.
        "out_of_scope_captures": out_of_scope,
        "breaks_found": len(breaks),
        "break_value": break_value,
        "breaks": breaks,
        "threshold": BREAK_THRESHOLD,
        "within_threshold": break_value <= BREAK_THRESHOLD,
        # Recorded with the run: a result is only interpretable next to the
        # period it covers and the file it read.
        "window_start": window[0],
        "window_end": window[1],
        "source": identity,
    }


# A run that compared nothing proved nothing.
#
# `within_threshold` was computed purely as `break_value <= BREAK_THRESHOLD`.
# An empty settlement file, a file with no usable settlement_date, or one whose
# loans match nothing on the ledger all produce zero breaks and therefore zero
# break value -- so the run recorded `outcome='ok'`, stamped a
# `last_successful_run`, and published a fresh success timestamp.
#
# That is the worst possible failure for this control: a broken or empty
# settlement feed becomes indistinguishable from a clean reconciliation, and the
# monitoring built on top of it goes quiet in exactly the way that means
# "healthy". D7 exists because a control that silently is not running looks like
# one that is running and finding nothing; recording success for a comparison
# that never happened is the same defect one layer up.
#
# Each condition is a separate code because they need different humans: an empty
# file is a feed problem, a missing window is a format problem, and zero
# comparable loans with rows present is a scope or identifier mismatch.
_VACUITY_CHECKS = (
    (
        "EmptySettlementFile",
        lambda r: (r.get("source") or {}).get("rows", 0) == 0,
        "the settlement file contained no rows",
    ),
    (
        "IncompleteSettlementWindow",
        lambda r: r.get("window_start") is None or r.get("window_end") is None,
        "no usable settlement_date, so the period covered is unknown",
    ),
    (
        "NothingCompared",
        lambda r: r.get("loans_compared", 0) == 0,
        "no loan appeared on either side of the comparison",
    ),
    (
        "PartiallyDatedSettlementFile",
        lambda r: (r.get("source") or {}).get("undated_rows", 0) > 0,
        "some settlement rows have no usable settlement_date, so their money "
        "cannot be placed in the window the rest of the file defines",
    ),
    (
        # A row this parser cannot read is not money, it is bad evidence.
        #
        # The sign used to come from `type == 'capture'` with everything else
        # treated as a refund, so a feed schema change, a typo or a blank cell
        # became negative money -- and flowed into `break_value`, the number the
        # threshold judges. Guessing the direction of money it cannot read is the
        # one thing a money control must not do, so the run fails instead.
        "MalformedSettlementRows",
        lambda r: (r.get("source") or {}).get("malformed_rows", 0) > 0,
        "some settlement rows carry a transaction type this control does not "
        "recognise, an amount that is not a positive number, or an amount with "
        "sub-cent precision this system cannot hold exactly -- so their "
        "direction or their value cannot be established",
    ),
    (
        # The netting defect's own guard rail. This control's whole correction
        # was to stop comparing net totals per loan; a file whose lines carry no
        # processor reference cannot be compared any other way, and falling back
        # to per-loan totals would restore the defect silently, at exactly the
        # moment the input is already known to be malformed. Failing is the only
        # answer that does not lie.
        "UnreferencedSettlementRows",
        lambda r: (r.get("source") or {}).get("unreferenced_rows", 0) > 0,
        "some settlement rows carry no processor_ref, so they cannot be matched "
        "to a capture and the comparison would have to fall back to the per-loan "
        "netting this control exists to replace",
    ),
)


def vacuity_error(result: dict) -> tuple[str, str] | None:
    """The error code and reason for a run that compared nothing, or None.

    Separate from `compare()` on purpose. `compare()` stays pure and reports
    what it saw; deciding that what it saw does not constitute evidence is a
    control decision, and it belongs next to the code that records outcomes.
    """
    for code, is_vacuous, reason in _VACUITY_CHECKS:
        if is_vacuous(result):
            return code, reason
    return None


def run_and_record() -> dict:
    """Run the comparison, record it, publish the metrics. Never raises.

    Returns the result with an `outcome` of 'ok', 'breach' or 'error'. The caller
    decides what to do about it -- `app.reconcile_job` turns it into an exit code.

    'breach' and 'error' stay distinct all the way through. A breach is a finding
    about the money; an error is a finding about the control, and a control that
    cannot run must not look like one that ran and found nothing.
    """
    run_id = _start_run()
    if run_id is None:
        # FAIL CLOSED. Without a start record there is nowhere to write the
        # result, so a run that proceeded could compare cleanly and report "ok"
        # with no durable evidence that it ever happened -- and "when did this
        # last agree?" would answer with a run that left no trace. A control
        # whose output is not recorded is not a control; it is a log line.
        #
        # This is the same failure D7 describes, reached from the other side: the
        # first version could not tell "never ran" from "ran and was fine", and a
        # silent start-record failure would have reintroduced exactly that.
        log.error("reconciliation refused to run: its start record could not be "
                  "written, so no result could be durably recorded")
        return {"outcome": "error", "error_code": "RunRecordUnavailable",
                "run_id": None}

    try:
        result = compare()
    except Exception as e:  # noqa
        code = type(e).__name__
        log.error("reconciliation could not complete (%s)", code)
        _finish_run(run_id, outcome="error", error_code=code)
        return {"outcome": "error", "error_code": code, "run_id": run_id}

    # Fail closed before any outcome is assigned. A vacuous run is an ERROR --
    # a finding about the control -- never an 'ok' and never a 'breach', because
    # it is not a statement about the money at all.
    vacuous = vacuity_error(result)
    if vacuous is not None:
        code, reason = vacuous
        log.error(
            "reconciliation compared nothing (%s): %s -- recording an error, not "
            "a success", code, reason,
        )
        _finish_run(
            run_id,
            outcome="error",
            error_code=code,
            loans_compared=result["loans_compared"],
            references_compared=result["references_compared"],
            unreferenced_captures=result["unreferenced_captures"],
            out_of_scope_captures=result["out_of_scope_captures"],
            window=(result["window_start"], result["window_end"]),
            source=result["source"],
        )
        # No last_successful_run, no success timestamp: the API derives both
        # from rows with outcome='ok', and this is not one.
        return {**result, "outcome": "error", "error_code": code,
                "error_reason": reason, "run_id": run_id}

    outcome = "ok" if result["within_threshold"] else "breach"
    recorded = _finish_run(
        run_id,
        outcome=outcome,
        loans_compared=result["loans_compared"],
        references_compared=result["references_compared"],
        unreferenced_captures=result["unreferenced_captures"],
        out_of_scope_captures=result["out_of_scope_captures"],
        breaks_found=result["breaks_found"],
        break_value=result["break_value"],
        breaks=result["breaks"][:MAX_RECORDED_BREAKS],
        window=(result["window_start"], result["window_end"]),
        source=result["source"],
    )
    if not recorded:
        # The comparison succeeded and its result is not durable. Same rule as a
        # missing start record: never report ok without evidence.
        log.error("reconciliation completed but its result could not be recorded")
        return {**result, "outcome": "error", "error_code": "RunRecordUnavailable",
                "run_id": run_id}

    # No gauge writes here. This runs in its own process, so anything set in its
    # registry dies with it -- the durable record IS the metric, and the API
    # derives the gauges from it at scrape time (see _ReconciliationCollector).
    if outcome == "ok":
        log.info("reconciliation ok loans=%s refs=%s out_of_scope=%s",
                 result["loans_compared"], result["references_compared"],
                 result["out_of_scope_captures"])
    else:
        # Loan ids, settlement references and amounts only. That is what a break
        # IS; there is nothing about the cardholder here to leak.
        log.error(
            "reconciliation BREACH loans=%s refs=%s unreferenced=%s breaks=%s "
            "value=%s threshold=%s",
            result["loans_compared"], result["references_compared"],
            result["unreferenced_captures"], result["breaks_found"],
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
                break_value=Decimal("0.00"), breaks=(), error_code=None,
                window=(None, None), source=None,
                references_compared=0, unreferenced_captures=0,
                out_of_scope_captures=0) -> bool:
    """Record the result. Returns whether it was durably written.

    The caller turns a False into a non-zero exit: a run whose RESULT could not
    be recorded is in the same position as one whose start could not be, and
    reporting success for it would be the defect this module exists to remove.

    `references_compared` and `unreferenced_captures` are appended to the
    statement rather than slotted in beside `loans_compared`, which they belong
    with logically. The parameter tuple's positions are asserted directly by the
    tests that prove a run recorded its threshold and its bounded break list, and
    silently renumbering them would move those assertions onto different values
    while they kept passing.
    """
    if run_id is None:
        return False
    try:
        db.query(
            "UPDATE reconciliation_runs SET finished_at = now(), outcome = %s, "
            "loans_compared = %s, breaks_found = %s, break_value = %s, "
            "breaks = %s::jsonb, error_code = %s, "
            "window_start = %s::date, window_end = %s::date, source = %s::jsonb, "
            "references_compared = %s, unreferenced_captures = %s, "
            "out_of_scope_captures = %s "
            "WHERE id = %s",
            (outcome, loans_compared, breaks_found, break_value,
             json.dumps(list(breaks)), error_code,
             window[0], window[1], json.dumps(source or {}),
             references_compared, unreferenced_captures, out_of_scope_captures,
             run_id),
        )
        return True
    except Exception as e:  # noqa
        log.error("could not record the result of reconciliation run %s (%s)",
                  run_id, type(e).__name__)
        return False


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
