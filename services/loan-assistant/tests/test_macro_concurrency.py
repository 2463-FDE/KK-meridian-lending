"""A BLS outage must not serialize the officer summary path.

The reviewed defect, exactly: `BlsMacroProvider.fetch()` held one process-wide
mutex across the outbound `httpx.get`, and a failure was never recorded. So
during a BLS outage N concurrent staff summaries queued on the lock and each one
then spent its own full `MACRO_TIMEOUT_SECONDS` rediscovering the same outage --
about N x timeout of serialized delay before the LLM call even started, with a
FastAPI worker thread blocked throughout. An optional citation could take down
the feature it was decorating.

These tests measure the property rather than inspecting the implementation. A
test that asserted "the lock is not held here" would pass on any refactor that
moved the stall somewhere else; asserting WALL-CLOCK TIME under a slow failing
transport fails no matter how the stall is reintroduced.

The two things being proven:

  1. N concurrent requests against a slow-failing BLS complete in approximately
     ONE timeout, not N.
  2. Inside the negative-cache window, repeated requests make NO further calls
     at all -- which is what stops an outage from also burning the 25-per-day
     unauthenticated quota.

Note on the timing assertions: they compare against a multiple of the simulated
delay rather than an absolute wall-clock budget, so a slow CI box scales both
sides together. The old behaviour fails them by a factor of N, which is far
outside any scheduling noise these bounds have to tolerate.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from app import macro
from app.macro import BlsMacroProvider

# Long enough that serialized behaviour is unmistakable, short enough that the
# suite stays fast: 8 threads x 0.4s serialized is 3.2s, versus ~0.4s correct.
SLOW = 0.4
THREADS = 8


class _Transport:
    """A stand-in for httpx.get that is slow and always fails, and counts how
    many times it was actually entered."""

    def __init__(self, delay: float = SLOW):
        self.delay = delay
        self.calls = 0
        self.concurrent = 0
        self.max_concurrent = 0
        self._lock = threading.Lock()

    def __call__(self, *a, **k):
        with self._lock:
            self.calls += 1
            self.concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            time.sleep(self.delay)
            raise httpx.ConnectError("simulated BLS outage")
        finally:
            with self._lock:
                self.concurrent -= 1


def _fetch_all(provider, n=THREADS):
    with ThreadPoolExecutor(max_workers=n) as pool:
        started = time.monotonic()
        results = list(pool.map(lambda _: provider.fetch(), range(n)))
        return results, time.monotonic() - started


def test_concurrent_summaries_cost_one_timeout_not_n(monkeypatch):
    """The headline property. Eight simultaneous requests, one outage.

    Under the old implementation this took 8 x SLOW because every request
    queued on the mutex and then made its own failing call. The bound here is
    3 x SLOW -- generous enough to absorb thread scheduling on a loaded CI box,
    and still less than half of the 8 x SLOW the defect produced.
    """
    transport = _Transport()
    monkeypatch.setattr(httpx, "get", transport)
    provider = BlsMacroProvider()

    results, elapsed = _fetch_all(provider)

    assert all(r is None for r in results), "no signal was ever available"
    assert elapsed < SLOW * 3, (
        f"{THREADS} concurrent requests took {elapsed:.2f}s against a {SLOW}s "
        f"failing call -- they are serializing behind the macro provider"
    )


def test_only_one_outbound_call_is_made_for_a_burst(monkeypatch):
    """Single-flight, not a thundering herd.

    Dropping the lock would also fix the timing test above, by letting all
    eight requests hit BLS at once -- against an API capped at 25 requests per
    day, during the one outage where retrying is least useful. So the call
    count is asserted, not just the elapsed time.
    """
    transport = _Transport()
    monkeypatch.setattr(httpx, "get", transport)
    provider = BlsMacroProvider()

    _fetch_all(provider)

    assert transport.calls == 1, (
        f"{transport.calls} outbound calls for one burst -- the refresh is not "
        f"single-flight"
    )
    assert transport.max_concurrent <= 1, "two refreshes were in flight at once"


def test_requests_inside_the_negative_cache_window_make_no_calls(monkeypatch):
    """A recorded failure suppresses further attempts for a short TTL.

    Without this the steady state during an outage is one failing call per
    request: each one rediscovers the outage, pays the timeout, and tells the
    next request nothing.
    """
    transport = _Transport(delay=0.05)
    monkeypatch.setattr(httpx, "get", transport)
    monkeypatch.setattr(macro, "MACRO_FAILURE_TTL_SECONDS", 30.0, raising=False)
    provider = BlsMacroProvider()

    assert provider.fetch() is None
    assert transport.calls == 1

    for _ in range(20):
        assert provider.fetch() is None
    assert transport.calls == 1, (
        f"{transport.calls} calls -- the failure was not remembered, so every "
        f"request pays its own timeout"
    )
    assert provider.suppressed_count == 20


def test_the_negative_cache_expires_so_recovery_is_automatic(monkeypatch):
    """Suppression is a short window, not a latch. Once it lapses the next
    request tries again and the signal comes back on its own."""
    transport = _Transport(delay=0.01)
    monkeypatch.setattr(httpx, "get", transport)
    monkeypatch.setattr(macro, "MACRO_FAILURE_TTL_SECONDS", 0.0, raising=False)
    provider = BlsMacroProvider()

    assert provider.fetch() is None
    assert transport.calls == 1
    assert provider.fetch() is None
    assert transport.calls == 2, "a zero-length window must not suppress anything"


def test_suppressed_requests_return_immediately(monkeypatch):
    """The point of the negative cache is latency, not just call count.

    A suppressed request must not wait on anything -- not the network, and not
    another thread's refresh.
    """
    transport = _Transport(delay=SLOW)
    monkeypatch.setattr(httpx, "get", transport)
    monkeypatch.setattr(macro, "MACRO_FAILURE_TTL_SECONDS", 30.0, raising=False)
    provider = BlsMacroProvider()

    provider.fetch()          # pays the timeout once, records the failure

    t0 = time.monotonic()
    for _ in range(50):
        provider.fetch()
    elapsed = time.monotonic() - t0
    assert elapsed < SLOW, (
        f"50 suppressed requests took {elapsed:.2f}s -- they are not returning "
        f"from cached state"
    )


def test_degradation_is_observable(monkeypatch):
    """An outage that is invisible until someone notices a missing citation is
    an outage nobody will notice. The provider records it."""
    transport = _Transport(delay=0.01)
    monkeypatch.setattr(httpx, "get", transport)
    monkeypatch.setattr(macro, "MACRO_FAILURE_TTL_SECONDS", 0.0, raising=False)
    provider = BlsMacroProvider()

    assert provider.degraded_since is None
    provider.fetch()
    assert provider.degraded_since is not None, "entering degradation is not recorded"
    assert provider.failure_count == 1

    # Recovery clears it, so the field means "currently degraded" and not
    # "was ever degraded".
    monkeypatch.setattr(httpx, "get",
                        lambda *a, **k: _FakeOk())
    assert provider.fetch() is not None
    assert provider.degraded_since is None


class _FakeOk:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "status": "REQUEST_SUCCEEDED",
            "Results": {"series": [{
                "seriesID": "LNS14000000",
                "data": [{"value": "4.2", "periodName": "June", "year": "2026"}],
            }]},
        }


def test_a_cached_value_is_served_while_a_refresh_is_in_flight(monkeypatch):
    """Fail-open under contention: a request that arrives mid-refresh answers
    from the last known figure instead of waiting for the network."""
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeOk())
    provider = BlsMacroProvider()
    assert provider.fetch().value == 4.2      # populate

    monkeypatch.setattr(macro, "MACRO_CACHE_TTL_SECONDS", 0, raising=False)
    slow = _Transport(delay=SLOW)
    monkeypatch.setattr(httpx, "get", slow)

    results, elapsed = _fetch_all(provider, n=4)

    assert all(r is not None and r.value == 4.2 for r in results), (
        "a refresh in flight must not blank out a figure already known"
    )
    assert elapsed < SLOW * 3


def test_no_applicant_data_is_sent_to_bls(monkeypatch):
    """Unchanged property, asserted under the new control flow.

    The request must carry a fixed series ID and nothing else. Re-checked here
    because the refactor moved where the call is made, and this is the kind of
    invariant that quietly stops holding when call sites move.
    """
    seen = {}

    def capture(url, *a, **k):
        seen["url"] = url
        seen["kwargs"] = k
        raise httpx.ConnectError("stop here")

    monkeypatch.setattr(httpx, "get", capture)
    BlsMacroProvider().fetch()

    assert seen["url"].endswith(macro.MACRO_SERIES_ID)
    assert "params" not in seen["kwargs"] or not seen["kwargs"]["params"]
    assert "json" not in seen["kwargs"]
    assert "data" not in seen["kwargs"]
