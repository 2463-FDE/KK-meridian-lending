"""The scheduler that actually runs the reconciliation control (D7).

Before this, the repository shipped `python -m app.reconcile_job` and a runbook
telling operators to wire cron themselves. A normal `docker compose up` kept
answering /health while reconciliation never ran once -- which is exactly the
failure D7 names: a control that silently is not running looks identical to one
that is running and finding nothing.

Run as the `reconciliation` service in docker-compose.yml, in the default
services list rather than behind a profile. A control an operator has to
remember to enable is the same defect with an extra step.

**It does not stop on a non-zero exit.** The job exits 1 on a breach and 2 on a
control failure, and both are findings to be read -- not reasons to stop
comparing the ledger to the processor. Exiting here would turn one bad day into
a silently dead control, so the loop logs the code and sleeps.

**A breach and a control failure are not the same wait.** A breach is the control
working: it compared the records and they disagreed, so the next comparison
belongs on the normal schedule. A control failure means the comparison did not
happen at all, and waiting a day to try again turns a transient condition into a
day without the control. The commonest such condition is startup: Postgres
answers `pg_isready` while `db/init` is still creating the schema, so the first
cycle's start-record INSERT can hit a table that does not exist yet. Observed on
a fresh `docker compose up`: the first cycle exits 2 and, before this change, the
next attempt was 24 hours away -- the same "silently not running" failure D7
names, reached by a different route.

So a cycle that did not run backs off over seconds rather than a day, and keeps
retrying at a capped interval until it runs or the service stops. There is no
attempt limit on purpose: giving up would return to the daily sleep while the
control is still unavailable, which is the state this change exists to prevent.
A cycle that DID run -- clean or breach -- resets the backoff and returns to the
normal interval.

**Nothing here decides whether the run was good.** That judgement lives in
`reconciliation.run_and_record`, is recorded in `reconciliation_runs`, and is
published through the metrics the alert rules read. This module only decides
*when*, so a scheduler bug cannot manufacture a success.
"""
import logging
import os
import signal
import sys
import time

from .logging_config import get_logger
from . import reconcile_job

log = get_logger("servicing.reconcile_scheduler")

# Daily by default. Overridable so a demo or an integration test can run it
# often enough to observe without waiting a day.
INTERVAL_SECONDS = int(os.getenv("RECONCILE_INTERVAL_SECONDS", "86400"))

#: Waits between attempts after a cycle that did not run, in order. The last
#: value repeats for as long as the failure lasts -- it is a cap, not a final
#: attempt. Short at the start because the common cause (a schema still being
#: created) clears in seconds; capped because a control that has been failing for
#: an hour is an operator problem, and retrying hard makes it harder to read.
RETRY_BACKOFF_SECONDS = (10, 30, 60, 300)

# Set by SIGTERM so `docker compose down` stops the loop promptly instead of
# waiting out a sleep that may be a day long.
_stopping = False


def _request_stop(signum, _frame):
    global _stopping
    _stopping = True
    log.info("reconcile scheduler received signal %s -- stopping after this cycle", signum)


def _sleep_interruptibly(seconds: int) -> None:
    """Sleep in short slices so a stop signal is noticed quickly.

    A single long sleep would make container shutdown wait up to the full
    interval, and an operator who cannot stop a service cleanly will eventually
    kill it in a way that loses the run in progress.
    """
    deadline = time.monotonic() + seconds
    while not _stopping and time.monotonic() < deadline:
        time.sleep(min(5.0, deadline - time.monotonic()))


def _retry_wait(consecutive_failures: int) -> int:
    """How long to wait before re-attempting a cycle that did not run.

    Walks `RETRY_BACKOFF_SECONDS` and then stays on its last value. Staying is
    the point: an attempt limit that fell back to `INTERVAL_SECONDS` would hide a
    still-broken control for a day, which is the defect this backoff exists to
    remove rather than reschedule.

    **Never longer than the configured interval.** The ladder is written for the
    daily default, but `RECONCILE_INTERVAL_SECONDS` is overridable so a demo or
    an integration test can run the control often. At a 30s interval an unclamped
    ladder would wait 60s and then 300s after a failure -- retrying a control
    that did NOT run more slowly than a healthy one is scheduled, which inverts
    the guarantee this backoff exists to make. Clamping keeps the ladder a retry
    at every configured cadence rather than only at the default one.
    """
    index = min(consecutive_failures, len(RETRY_BACKOFF_SECONDS) - 1)
    return min(RETRY_BACKOFF_SECONDS[index], INTERVAL_SECONDS)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO)
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    log.info("reconcile scheduler starting -- interval %ss", INTERVAL_SECONDS)

    consecutive_failures = 0

    while not _stopping:
        try:
            code = reconcile_job.main([])
            # Logged at the level the outcome deserves, so an operator reading
            # container logs sees a breach or a control failure without having
            # to query the database.
            if code == reconcile_job.EXIT_OK:
                log.info("reconciliation cycle complete: clean")
            elif code == reconcile_job.EXIT_BREACH:
                log.error("reconciliation cycle complete: BREACH (exit %s)", code)
            else:
                log.error("reconciliation cycle complete: CONTROL FAILURE (exit %s)", code)
            # A breach RAN. The records disagreed, which is the control reporting
            # a finding, not the control failing -- so it waits like any other
            # completed cycle.
            ran = code in (reconcile_job.EXIT_OK, reconcile_job.EXIT_BREACH)
        except Exception:                                    # noqa: BLE001
            # A crash in the job must not kill the schedule. The next cycle may
            # well succeed, and a dead scheduler is worse than a failed run --
            # the failed run is recorded and alertable, the dead one is not.
            log.exception("reconciliation cycle raised -- continuing on schedule")
            # A raise means no comparison happened, so it backs off like any
            # other cycle that did not run.
            ran = False

        if _stopping:
            break

        if ran:
            consecutive_failures = 0
            wait = INTERVAL_SECONDS
        else:
            wait = _retry_wait(consecutive_failures)
            consecutive_failures += 1
            log.warning(
                "reconciliation did not run -- retrying in %ss "
                "(consecutive failures: %s)", wait, consecutive_failures)

        _sleep_interruptibly(wait)

    log.info("reconcile scheduler stopped")
    return 0


if __name__ == "__main__":                                   # pragma: no cover
    sys.exit(main())
