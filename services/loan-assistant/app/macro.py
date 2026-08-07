"""One grounded external signal for the officer summary.

Week 1-4 client review: the summary "collects almost everything from inside the
application". Everything it says is derived from what the applicant themselves
typed, so it can restate the file but cannot contextualise it. This module adds
exactly one signal from outside: the current US unemployment rate, published by
the Bureau of Labor Statistics.

Why unemployment, and why national:

  * It is the macro variable most directly tied to the risk this summary is
    about -- an applicant's ability to keep earning. Employment stability is
    already one of the two fields the summary refuses to assign a risk tier
    without (`_RISK_GROUNDING_FIELDS`), and this puts that field in context.
  * National, not state-level, because there is no state column in the schema.
    `applicants` stores a free-text `address` plus `zip_code`; deriving a state
    from a ZIP3 prefix is ambiguous at state boundaries, and inventing a
    900-row prefix table to paper over that would be guessing dressed up as
    data. State-level LAUS series exist and are the obvious upgrade the day a
    real `state` column does.

Design choices worth stating, because two of them are the opposite of what the
other external calls in this codebase do:

**Fails OPEN, deliberately.** The bureau pull and the AI scorer fail closed --
they are decision inputs, and a missing one means the decision cannot be made
honestly. This is not a decision input. It is context printed next to a summary,
and a BLS outage must never stop a loan officer seeing their applicant's file.
When the fetch fails the summary is produced without the signal, and the absence
is visible rather than silent (no citation appears).

**Caching is mandatory, not an optimisation.** The BLS public API v1 allows 25
queries per day per IP without a registered key. One uncached call per summary
would exhaust that in a morning and then fail for everyone. The series updates
monthly, so a long TTL costs nothing in freshness.

**No applicant data is sent anywhere.** The request carries a fixed series ID
and nothing else -- no name, no ZIP, no amount, not even an application id. That
is a property of choosing a national series, and it is worth keeping in mind if
anyone later swaps in a geographic one: a state-level request leaks coarse
location to a third party for every summary viewed.

**The model never authors the citation.** `fetch()` returns the value, and the
citation on the response is built from that return value server-side -- the same
rule `_applicant_name` follows. The rate is put in the prompt as context so the
model can reason about it, but if the model were to restate the number
differently, the number the officer sees still comes from BLS.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Protocol

import httpx

from .config import (
    MACRO_CACHE_TTL_SECONDS,
    MACRO_ENABLED,
    MACRO_FAILURE_TTL_SECONDS,
    MACRO_SERIES_ID,
    MACRO_STALE_SERVE_SECONDS,
    MACRO_TIMEOUT_SECONDS,
)
log = logging.getLogger(__name__)

_BLS_V1_URL = "https://api.bls.gov/publicAPI/v1/timeseries/data"


@dataclass(frozen=True)
class MacroSignal:
    """One published statistic, with everything needed to cite it."""

    source: str          # "U.S. Bureau of Labor Statistics"
    series_id: str       # "LNS14000000"
    label: str           # "US unemployment rate (seasonally adjusted)"
    value: float         # 4.2
    unit: str            # "percent"
    period: str          # "June 2026" -- the period the figure describes
    url: str             # where a reader can verify it

    def cite(self) -> str:
        return (
            f"{self.label}: {self.value}{'%' if self.unit == 'percent' else ' ' + self.unit} "
            f"({self.period}). Source: {self.source}, series {self.series_id}."
        )


class MacroProvider(Protocol):
    def fetch(self) -> MacroSignal | None: ...


class StubMacroProvider:
    """Deterministic stand-in for dev/test. Returns a fixed, obviously-marked
    figure so a test never depends on what the real economy did this month, and
    so a stub value can never be mistaken for a live one in a screenshot."""

    def __init__(self, value: float = 4.0):
        self._value = value
        self.fetch_count = 0

    def fetch(self) -> MacroSignal | None:
        self.fetch_count += 1
        return MacroSignal(
            source="U.S. Bureau of Labor Statistics (STUB — not live data)",
            series_id=MACRO_SERIES_ID,
            label="US unemployment rate (seasonally adjusted)",
            value=self._value,
            unit="percent",
            period="stub period",
            url=f"{_BLS_V1_URL}/{MACRO_SERIES_ID}",
        )


class BlsMacroProvider:
    """Live BLS public API v1. No API key: v1 is unauthenticated and capped at
    25 requests/day per IP, which is why the cache below is not optional.

    CONCURRENCY, and the outage this used to cause
    ----------------------------------------------
    The first version held one process-wide mutex across the outbound
    `httpx.get`. Reviewed and correct: during a BLS outage that turns an
    optional citation into a service-wide stall. N concurrent staff summaries
    queue on the lock, and because a failure was never recorded each one then
    spends its own full MACRO_TIMEOUT_SECONDS discovering the same outage --
    roughly N x timeout of serialized delay before the LLM call even starts,
    with a FastAPI worker thread blocked for every one of them.

    The fix is not to drop the lock. That trades a stall for a thundering herd:
    every concurrent request firing its own request at an API with a 25-per-day
    cap, which would exhaust the quota during the one outage where retrying is
    least useful.

    So: single-flight, fail-open, and never blocking.

      * `_state_lock` guards in-memory state ONLY and is never held across IO.
        Every critical section under it is a few field reads.
      * `_refresh_lock` is a single-flight token acquired with blocking=False.
        Exactly one caller at a time performs the network call. Everyone else
        returns IMMEDIATELY with whatever is known -- they never queue.
      * A failure is recorded and suppresses further attempts for
        MACRO_FAILURE_TTL_SECONDS. During an outage the steady state is one
        attempt per window, not one per request.

    So N concurrent requests against a slow-failing BLS cost approximately ONE
    timeout in total, not N. That is the property
    test_macro_concurrency.py asserts directly.

    On serving a previously-fetched value past its TTL
    --------------------------------------------------
    An earlier revision refused to, reasoning that "a stale figure presented
    with a current-looking period would be worse than no figure". That reasoning
    does not survive inspection: MacroSignal carries its own `period`, and the
    citation prints it, so a figure fetched yesterday still reads "June 2026"
    and claims nothing about when it was retrieved. Withholding it during an
    outage removes true, correctly-labelled context for no gain. It is served
    within MACRO_STALE_SERVE_SECONDS and dropped after that.
    """

    def __init__(self):
        # Guards state. NEVER held across a network call.
        self._state_lock = threading.Lock()
        # Single-flight token. Acquired non-blocking; a caller that misses it
        # does not wait, it answers from what is already known.
        self._refresh_lock = threading.Lock()
        self._cached: MacroSignal | None = None
        self._cached_at: float = 0.0
        self._failed_at: float = 0.0
        # Observability: an outage should be visible in metrics and logs, not
        # only in the absence of a citation nobody was looking for.
        self.fetch_count = 0          # outbound calls actually attempted
        self.failure_count = 0        # of those, how many failed
        self.suppressed_count = 0     # requests that skipped the call entirely
        self.stale_served_count = 0   # requests answered with a past-TTL value
        self.degraded_since: float | None = None

    def _fresh(self) -> bool:
        return (
            self._cached is not None
            and (time.monotonic() - self._cached_at) < MACRO_CACHE_TTL_SECONDS
        )

    def _suppressed(self) -> bool:
        """Whether a recent failure is still suppressing outbound attempts."""
        return (
            self._failed_at > 0.0
            and (time.monotonic() - self._failed_at) < MACRO_FAILURE_TTL_SECONDS
        )

    def _servable_stale(self) -> MacroSignal | None:
        """A previously-fetched figure still inside the stale-serve window."""
        if self._cached is None:
            return None
        if (time.monotonic() - self._cached_at) < MACRO_STALE_SERVE_SECONDS:
            return self._cached
        return None

    def _answer_without_calling(self) -> MacroSignal | None:
        """What to return when no outbound call will be made. Caller must NOT
        hold _state_lock."""
        with self._state_lock:
            stale = self._servable_stale()
            if stale is not None:
                self.stale_served_count += 1
        if stale is not None:
            log.info(
                "macro signal served from cache past its TTL "
                "(refresh in flight or suppressed) period=%s", stale.period,
            )
        return stale

    def fetch(self) -> MacroSignal | None:
        with self._state_lock:
            if self._fresh():
                return self._cached
            suppressed = self._suppressed()
            if suppressed:
                self.suppressed_count += 1
        if suppressed:
            # A recent failure is still being remembered. Do not call BLS, do
            # not wait for anyone: answer now. This is the branch that turns
            # N x timeout into one timeout per failure window.
            return self._answer_without_calling()

        if not self._refresh_lock.acquire(blocking=False):
            # Another thread is already refreshing. Blocking here would rebuild
            # exactly the queue this class exists to avoid.
            return self._answer_without_calling()

        try:
            # Deliberately outside _state_lock: this is the only slow operation
            # in the class, and holding a lock across it was the defect.
            signal = self._fetch_uncached()
        finally:
            self._refresh_lock.release()

        now = time.monotonic()
        with self._state_lock:
            if signal is not None:
                self._cached = signal
                self._cached_at = now
                self._failed_at = 0.0
                recovered_from = self.degraded_since
                self.degraded_since = None
            else:
                self.failure_count += 1
                self._failed_at = now
                recovered_from = None
                if self.degraded_since is None:
                    self.degraded_since = now
                began = self.degraded_since
        if signal is None:
            log.warning(
                "macro signal degraded: suppressing BLS calls for %.0fs "
                "degraded_for=%.1fs failures=%d",
                MACRO_FAILURE_TTL_SECONDS, now - began, self.failure_count,
            )
            return self._answer_without_calling()
        if recovered_from is not None:
            log.info(
                "macro signal recovered after %.1fs degraded", now - recovered_from
            )
        return signal

    def _fetch_uncached(self) -> MacroSignal | None:
        self.fetch_count += 1
        url = f"{_BLS_V1_URL}/{MACRO_SERIES_ID}"
        try:
            resp = httpx.get(url, timeout=MACRO_TIMEOUT_SECONDS)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001 -- fails open, see module docstring
            log.warning(
                "macro signal unavailable, summary will omit it error_type=%s",
                type(exc).__name__,
            )
            return None

        try:
            if payload.get("status") != "REQUEST_SUCCEEDED":
                raise ValueError(f"BLS status {payload.get('status')!r}")
            series = payload["Results"]["series"][0]
            latest = series["data"][0]
            return MacroSignal(
                source="U.S. Bureau of Labor Statistics",
                series_id=series["seriesID"],
                label="US unemployment rate (seasonally adjusted)",
                value=float(latest["value"]),
                unit="percent",
                period=f"{latest['periodName']} {latest['year']}",
                url=url,
            )
        except Exception as exc:  # noqa: BLE001
            # A shape change upstream must not surface as a 500 on a summary.
            log.warning(
                "macro signal response not understood, omitting it error_type=%s",
                type(exc).__name__,
            )
            return None


# Module-level provider so the cache is shared across requests. Swapped in tests.
provider: MacroProvider = BlsMacroProvider() if MACRO_ENABLED else StubMacroProvider()


def current_signal() -> MacroSignal | None:
    """The signal to attach to a summary, or None if it is unavailable or off."""
    if not MACRO_ENABLED:
        return None
    try:
        return provider.fetch()
    except Exception as exc:  # noqa: BLE001 -- belt and braces; never break a summary
        log.warning("macro provider raised error_type=%s", type(exc).__name__)
        return None
