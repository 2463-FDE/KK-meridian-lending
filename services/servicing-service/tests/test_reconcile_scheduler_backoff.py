"""The scheduler's wait after a cycle that did not run (D7).

`reconcile_scheduler` used to sleep `INTERVAL_SECONDS` after every cycle,
whatever the cycle did. That is right for a cycle that ran and wrong for one that
did not: a control failure means no comparison happened, and a day is a long time
to wait before finding out whether the condition has cleared.

The condition that made this concrete is startup. Postgres answers `pg_isready`
while `db/init` is still creating the schema, so the first cycle's start-record
INSERT can hit a missing table and exit 2. Reproduced on a fresh
`docker compose up`: the stack came up healthy, the first cycle exited 2, and
`reconciliation_runs` stayed empty -- the next attempt being 24 hours away.

The distinction these tests protect is the one the module is about:

  * a cycle that RAN -- clean or breach -- waits the normal interval;
  * a cycle that DID NOT run backs off over seconds and keeps retrying.

A breach is deliberately on the first side. The control compared the records and
they disagreed; that is a finding, not a failure of the control.
"""
import pytest

from app import reconcile_job, reconcile_scheduler


@pytest.fixture
def waits(monkeypatch):
    """Run the loop for a fixed number of cycles, recording each wait.

    `_sleep_interruptibly` is replaced rather than shortened: the loop's decision
    is the subject, and a test that actually slept would be measuring the clock.
    """
    recorded = []

    def run(exit_codes):
        remaining = list(exit_codes)

        def fake_job(_argv):
            code = remaining.pop(0)
            if isinstance(code, Exception):
                raise code
            return code

        def fake_sleep(seconds):
            recorded.append(seconds)
            if not remaining:
                # Nothing left to run, so stop the loop the way a signal would.
                monkeypatch.setattr(reconcile_scheduler, "_stopping", True)

        monkeypatch.setattr(reconcile_scheduler, "_stopping", False)
        monkeypatch.setattr(reconcile_job, "main", fake_job)
        monkeypatch.setattr(reconcile_scheduler, "_sleep_interruptibly", fake_sleep)
        recorded.clear()
        reconcile_scheduler.main([])
        return recorded

    return run


def test_a_first_control_failure_retries_in_seconds_not_a_day(waits):
    recorded = waits([reconcile_job.EXIT_ERROR, reconcile_job.EXIT_OK])

    assert recorded[0] == reconcile_scheduler.RETRY_BACKOFF_SECONDS[0]
    assert recorded[0] < reconcile_scheduler.INTERVAL_SECONDS


def test_repeated_control_failures_back_off_in_order(waits):
    failures = [reconcile_job.EXIT_ERROR] * 4 + [reconcile_job.EXIT_OK]
    recorded = waits(failures)

    assert recorded[:4] == list(reconcile_scheduler.RETRY_BACKOFF_SECONDS)


def test_the_backoff_caps_and_keeps_retrying_rather_than_giving_up(waits):
    # Eight consecutive failures against a four-step ladder. The cap must repeat,
    # and must never fall back to the daily interval -- an attempt limit would
    # hide a still-broken control for a day, which is the defect being removed.
    recorded = waits([reconcile_job.EXIT_ERROR] * 8 + [reconcile_job.EXIT_OK])

    cap = max(reconcile_scheduler.RETRY_BACKOFF_SECONDS)
    assert recorded[:8] == [10, 30, 60, 300, 300, 300, 300, 300]
    assert max(recorded[:8]) == cap
    assert reconcile_scheduler.INTERVAL_SECONDS not in recorded[:8]


def test_a_clean_cycle_waits_the_normal_interval(waits):
    recorded = waits([reconcile_job.EXIT_OK])

    assert recorded == [reconcile_scheduler.INTERVAL_SECONDS]


def test_a_breach_is_a_completed_cycle_and_waits_the_normal_interval(waits):
    # The distinction this whole change turns on: a breach RAN.
    recorded = waits([reconcile_job.EXIT_BREACH])

    assert recorded == [reconcile_scheduler.INTERVAL_SECONDS]


@pytest.mark.parametrize("success", [reconcile_job.EXIT_OK, reconcile_job.EXIT_BREACH])
def test_a_cycle_that_runs_resets_the_backoff(waits, success):
    recorded = waits([
        reconcile_job.EXIT_ERROR,
        reconcile_job.EXIT_ERROR,
        success,
        reconcile_job.EXIT_ERROR,
        reconcile_job.EXIT_OK,
    ])

    first, second, after_success, after_reset = recorded[0], recorded[1], recorded[2], recorded[3]
    assert [first, second] == list(reconcile_scheduler.RETRY_BACKOFF_SECONDS[:2])
    assert after_success == reconcile_scheduler.INTERVAL_SECONDS
    # Back to the start of the ladder, not to where it left off.
    assert after_reset == reconcile_scheduler.RETRY_BACKOFF_SECONDS[0]


def test_a_raising_cycle_backs_off_too(waits):
    # A crash means no comparison happened, so it is a cycle that did not run --
    # the loop already refused to die on it; now it also refuses to wait a day.
    recorded = waits([RuntimeError("job blew up"), reconcile_job.EXIT_OK])

    assert recorded[0] == reconcile_scheduler.RETRY_BACKOFF_SECONDS[0]


def test_a_stop_signal_interrupts_the_retry_wait(monkeypatch):
    # The real `_sleep_interruptibly` is under test here, not a fake: a backoff
    # that ignored SIGTERM would make `docker compose down` wait out the cap.
    monkeypatch.setattr(reconcile_scheduler, "_stopping", False)
    slept = []
    monkeypatch.setattr(reconcile_scheduler.time, "sleep", lambda s: slept.append(s))

    def stop_after_first_slice(_seconds):
        monkeypatch.setattr(reconcile_scheduler, "_stopping", True)

    monkeypatch.setattr(reconcile_scheduler.time, "sleep", stop_after_first_slice)
    reconcile_scheduler._sleep_interruptibly(max(reconcile_scheduler.RETRY_BACKOFF_SECONDS))

    # It returned instead of waiting out the cap.
    assert reconcile_scheduler._stopping is True


def test_stopping_before_the_wait_ends_the_loop_without_sleeping(waits, monkeypatch):
    # A signal during the cycle must not be followed by a backoff wait.
    def fake_job(_argv):
        monkeypatch.setattr(reconcile_scheduler, "_stopping", True)
        return reconcile_job.EXIT_ERROR

    recorded = []
    monkeypatch.setattr(reconcile_scheduler, "_stopping", False)
    monkeypatch.setattr(reconcile_job, "main", fake_job)
    monkeypatch.setattr(reconcile_scheduler, "_sleep_interruptibly",
                        lambda s: recorded.append(s))
    reconcile_scheduler.main([])

    assert recorded == []


def test_no_retry_wait_exceeds_the_configured_interval(monkeypatch):
    """A failed cycle must never be retried more slowly than a healthy one.

    `RECONCILE_INTERVAL_SECONDS` is overridable so a demo or an integration test
    can run the control often. The ladder is written for the daily default, so at
    a short interval its later steps would overshoot: at 30s, an unclamped ladder
    waits 60s and then 300s after a failure -- scheduling a control that did NOT
    run less often than one that did, which is the opposite of the guarantee.
    """
    monkeypatch.setattr(reconcile_scheduler, "INTERVAL_SECONDS", 30)

    waits = [reconcile_scheduler._retry_wait(n) for n in range(8)]

    assert waits == [10, 30, 30, 30, 30, 30, 30, 30]
    assert max(waits) <= 30


@pytest.mark.parametrize("interval", [5, 30, 120, 3600, 86400])
def test_the_clamp_holds_at_every_plausible_interval(monkeypatch, interval):
    monkeypatch.setattr(reconcile_scheduler, "INTERVAL_SECONDS", interval)

    for failures in (0, 1, 2, 3, 25, 10_000):
        assert reconcile_scheduler._retry_wait(failures) <= interval


def test_the_ladder_is_ascending_and_bounded():
    ladder = reconcile_scheduler.RETRY_BACKOFF_SECONDS
    assert list(ladder) == sorted(ladder), "a backoff that goes backwards is not a backoff"
    assert ladder[0] > 0
    assert reconcile_scheduler._retry_wait(10_000) <= reconcile_scheduler.INTERVAL_SECONDS, (
        "the cap must stay at or below the normal interval, or the backoff is not a retry"
    )


def test_retry_wait_never_indexes_past_the_ladder():
    # Directly, because the loop only ever reaches the high end after a long
    # outage and an IndexError there would kill the scheduler.
    cap = max(reconcile_scheduler.RETRY_BACKOFF_SECONDS)
    for failures in (0, 1, 2, 3, 4, 50, 10_000):
        assert reconcile_scheduler._retry_wait(failures) <= cap
    assert reconcile_scheduler._retry_wait(10_000) == cap
