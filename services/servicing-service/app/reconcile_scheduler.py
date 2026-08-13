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


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO)
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    log.info("reconcile scheduler starting -- interval %ss", INTERVAL_SECONDS)

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
        except Exception:                                    # noqa: BLE001
            # A crash in the job must not kill the schedule. The next cycle may
            # well succeed, and a dead scheduler is worse than a failed run --
            # the failed run is recorded and alertable, the dead one is not.
            log.exception("reconciliation cycle raised -- continuing on schedule")

        if _stopping:
            break
        _sleep_interruptibly(INTERVAL_SECONDS)

    log.info("reconcile scheduler stopped")
    return 0


if __name__ == "__main__":                                   # pragma: no cover
    sys.exit(main())
