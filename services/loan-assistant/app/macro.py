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
    MACRO_SERIES_ID,
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
    25 requests/day per IP, which is why the cache below is not optional."""

    def __init__(self):
        self._lock = threading.Lock()
        self._cached: MacroSignal | None = None
        self._cached_at: float = 0.0
        self.fetch_count = 0

    def _fresh(self) -> bool:
        return (
            self._cached is not None
            and (time.monotonic() - self._cached_at) < MACRO_CACHE_TTL_SECONDS
        )

    def fetch(self) -> MacroSignal | None:
        with self._lock:
            if self._fresh():
                return self._cached
            signal = self._fetch_uncached()
            if signal is not None:
                self._cached = signal
                self._cached_at = time.monotonic()
            # On failure the previous value is deliberately NOT returned: a
            # stale figure presented with a current-looking period would be
            # worse than no figure. Returning None omits the citation instead.
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
