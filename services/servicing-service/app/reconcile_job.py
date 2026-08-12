"""The scheduled entry point for reconciliation. Exit code is the contract.

    python -m app.reconcile_job

    0  ran, everything within threshold
    1  ran, breaks exceeded the threshold          -- a finding about the money
    2  could not run                               -- a finding about the control

Deliberately a separate process rather than a thread inside the API. An
in-process scheduler dies with its web worker and nothing reports that it stopped,
which is the failure mode D7 already had once: a control that silently is not
running looks exactly like one that is running and finding nothing.

Two exit codes rather than one, because "the reconciliation found a break" and
"the reconciliation could not run" need different humans. Cron mails on any
non-zero, so both get noticed; a scheduler that distinguishes them can route them
differently.

See docs/runbook.md for the cron and compose wiring, and for what this does NOT
do -- there is no alerting integration in this build, only the exit code, the
`reconciliation_runs` row and the Prometheus gauges on /metrics.
"""
import logging
import sys

from . import reconciliation
from .logging_config import get_logger

log = get_logger("servicing.reconcile_job")

EXIT_OK = 0
EXIT_BREACH = 1
EXIT_ERROR = 2


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO)
    result = reconciliation.run_and_record()
    outcome = result.get("outcome")

    if outcome == "ok":
        log.info("reconciliation clean: %s loan(s) compared, no breaks",
                 result.get("loans_compared"))
        return EXIT_OK

    if outcome == "breach":
        log.error(
            "reconciliation breach: %s break(s), %s total, threshold %s",
            result.get("breaks_found"), result.get("break_value"),
            result.get("threshold"),
        )
        # Loan ids and amounts. A break is a money fact; there is nothing about
        # the cardholder in it to leak into a log an operator will read.
        for b in result.get("breaks", [])[:reconciliation.MAX_RECORDED_BREAKS]:
            log.error("  loan %s ledger=%s settlement=%s difference=%s",
                      b["loan_id"], b["ledger"], b["settlement"], b["difference"])
        return EXIT_BREACH

    log.error("reconciliation could not run (%s)", result.get("error_code"))
    return EXIT_ERROR


if __name__ == "__main__":                                   # pragma: no cover
    sys.exit(main())
