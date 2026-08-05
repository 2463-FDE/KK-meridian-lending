"""PR #8 review -- the reconciler task must survive for the process lifetime
and stop cleanly.

The first version of the worker did `asyncio.create_task(...)` and threw the
handle away. The event loop holds only a WEAK reference to a running task, so a
task nothing else references is eligible for garbage collection while it is
awaiting -- CPython documents exactly this. The drain would then stop with no
exception, no log line, and no failed health check, and the only visible
symptom would be the thing it exists to prevent: captured payments sitting
uncredited.

That is untestable by inspection and trivially testable by running it, which is
what this file does. It also covers the other half -- shutdown must cancel and
await the task, so a pass in flight is not abandoned and the loop is really gone
before the event loop is torn down.

No database needed: the loop is prevented from ever completing a pass (its very
first action is a sleep, and reconcile_once is stubbed anyway), because what is
under test is the task's lifecycle, not what a pass does.
"""
import asyncio
import gc

import pytest
from fastapi.testclient import TestClient

from app import config, main


@pytest.fixture
def running_reconciler(monkeypatch):
    """A payment-service app with the in-process worker switched on, and its
    drain stubbed out so no pass ever touches a database."""
    monkeypatch.setattr(config, "RECONCILE_INTERVAL_SECONDS", 0.01, raising=False)
    monkeypatch.setattr(main.config, "RECONCILE_INTERVAL_SECONDS", 0.01, raising=False)
    monkeypatch.setattr(main.reconcile, "reconcile_once",
                        lambda *_a, **_k: {"claimed": 0, "applied": 0, "still_pending": 0})
    monkeypatch.setattr(main, "_publish_unreconciled_gauges", lambda: None)
    return main.app


def test_the_reconciler_task_is_held_for_the_application_lifetime(running_reconciler):
    """A strong reference must exist on app.state -- not just inside the event
    loop, which is exactly the reference that is too weak to rely on."""
    with TestClient(running_reconciler) as client:
        assert client.get("/health").status_code == 200

        task = running_reconciler.state.reconciler_task
        assert task is not None, "no reconciler task was started"
        assert isinstance(task, asyncio.Task)
        assert task.get_name() == "payment-reconciler"
        assert not task.done(), "the reconciler stopped on its own"

        # The failure mode being guarded against, forced: a full collection
        # while the task is parked in `await asyncio.sleep(...)`. With only the
        # loop's weak reference this is where the task could disappear.
        gc.collect()
        assert not task.done(), "the reconciler task was collected mid-flight"
        assert running_reconciler.state.reconciler_task is task


def test_shutdown_cancels_and_awaits_the_reconciler(running_reconciler):
    with TestClient(running_reconciler):
        task = running_reconciler.state.reconciler_task
        assert not task.done()

    # Outside the context manager, shutdown has run to completion.
    assert task.done(), "the task outlived the application"
    assert task.cancelled(), "the task ended some way other than a clean cancel"


def test_shutdown_is_clean_when_the_worker_is_disabled(monkeypatch):
    """interval=0 must start nothing -- and shutdown must not trip over the
    absent task. This is the configuration the rest of the suite runs under."""
    monkeypatch.setattr(main.config, "RECONCILE_INTERVAL_SECONDS", 0, raising=False)

    with TestClient(main.app) as client:
        assert client.get("/health").status_code == 200
        assert main.app.state.reconciler_task is None


def test_a_failing_pass_does_not_kill_the_loop(running_reconciler, monkeypatch):
    """A reconciliation pass that raises must be logged and retried, never
    terminate the worker -- otherwise one bad pass silently disables the drain
    for the rest of the process's life."""
    calls = {"n": 0}

    def boom(*_a, **_k):
        calls["n"] += 1
        raise RuntimeError("servicing exploded")

    monkeypatch.setattr(main.reconcile, "reconcile_once", boom)

    with TestClient(running_reconciler) as client:
        task = running_reconciler.state.reconciler_task
        # Let several ticks go by (interval is 10ms).
        for _ in range(50):
            if calls["n"] >= 2:
                break
            client.get("/health")
        assert calls["n"] >= 2, f"the loop stopped after {calls['n']} failing pass(es)"
        assert not task.done(), "a failing pass killed the reconciler"
