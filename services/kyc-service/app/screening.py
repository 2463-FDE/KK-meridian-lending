"""Sanctions-screening boundary, behind an interface. MECHANISM ONLY.

Spec 0004 §4 and ADR 0012. This module is the seam a real screening vendor slots
behind. It performs no screening of its own, it is wired into no route, and
nothing in this repository calls it yet -- the enforcement step (a screen gating
`cip_passed`) is deliberately separate, because it changes who can onboard and
that decision depends on answers this repository does not have.

**What is buildable now, and why the vendor block does not stop it.** Spec 0003
had to separate a mapping's blocked CONTENT from its buildable MECHANISM, and the
same split applies here: the provider taxonomy, the match thresholds and the
disposition of a hit are all blocked (VENDOR-BLOCKED, COMPLIANCE-BLOCKED), while
a seam whose default is to refuse needs no vendor knowledge at all. Building it
now means the eventual integration is a provider implementation rather than a
redesign, and it means the two defects the credit-bureau boundary shipped with
cannot recur here:

  * an SSN in a URL query string, which lands in the provider's access logs and
    every proxy in between -- identity data goes in the request body, and this
    module has no code path that puts it anywhere else;
  * no idempotency key, so an ambiguous timeout started a second, independently
    billed operation. Every screen carries the caller's `request_key`, and a
    replay returns the ORIGINAL screen rather than performing a new one.

**Three outcomes, not two.** `clear`, `potential_match`, `error`. A boolean would
force this module to decide what a possible hit means, and that is exactly the
decision spec 0004 §3.2 records as COMPLIANCE-BLOCKED: who may clear a potential
match, on what evidence, and what the applicant is told while it is pending. This
module reports; it does not dispose.

**No thresholds anywhere.** No match score, no name-distance metric, no
transliteration rule. `match_count` is how many candidates the provider returned
-- a count, not a score and not a verdict. Choosing a cutoff is
COMPLIANCE-BLOCKED on the false-negative appetite and VENDOR-BLOCKED on the
provider's scoring semantics, and a number here would look like a control while
being a guess with a compliance consequence in both directions.

HONEST LIMITATION -- read before trusting this anywhere real. There is no
screening provider in this repository: no vendor is selected, `SCREENING_BASE_URL`
points at a placeholder, and every local and test run uses
`StubScreeningProvider`. The idempotency and list-version contracts below are
what this repository would REQUIRE of a provider, verified only against our own
stub. Real-provider behaviour is UNVERIFIED and no compliance claim is made.
`HttpScreeningProvider` exists to pin the shape that implementation must take,
not to prove it works. CIP-only onboarding is unchanged and `docs/DEBT.md` D11
stays open.
"""
from dataclasses import dataclass
from typing import Protocol

import httpx

from .config import ALLOW_SCREENING_STUB, SCREENING_BASE_URL, SCREENING_KEY
from .logging_config import get_logger

log = get_logger("kyc.screening")

#: The only three answers a screen may produce.
CLEAR = "clear"
POTENTIAL_MATCH = "potential_match"
ERROR = "error"
OUTCOMES = frozenset({CLEAR, POTENTIAL_MATCH, ERROR})

#: The stub's trigger for a potential match. Deliberately not a name: spec 0004
#: §4 requires that nothing in this repository resemble sanctions-list data, and
#: a plausible-looking name in a fixture is the file people mistake for the list.
STUB_MATCH_MARKER = "ZZ-SCREENING-STUB-MATCH"


class ScreeningUnavailable(RuntimeError):
    """The screen could not be completed, so no verdict may be inferred.

    Raised rather than returned so a caller cannot treat it as a value with a
    falsy meaning. The fail-closed rule (spec 0004 §5, ADR 0012 §3) is that a
    provider failure blocks; there is no degraded mode in which screening is
    skipped and onboarding continues, and "the vendor was down" is not a reason
    to onboard an unscreened party.
    """


@dataclass(frozen=True)
class ScreeningResult:
    """What the screening boundary returns.

    `reference_id` is the provider's non-sensitive handle for the operation --
    safe to persist and log, and what a later review re-fetches instead of
    re-screening. `list_version` is the provider's identifier for the list state
    used: a screen that cannot name it is not reproducible evidence, so this
    module refuses to build a result without one.
    """

    outcome: str
    list_version: str
    reference_id: str
    match_count: int

    def __post_init__(self):
        if self.outcome not in OUTCOMES:
            raise ScreeningUnavailable(
                f"unknown screening outcome; expected one of {sorted(OUTCOMES)}")
        if not self.list_version:
            raise ScreeningUnavailable(
                "a screen with no list version is not evidence: "
                "'clear against the SDN List' without saying which day's list "
                "is a claim that cannot be reproduced")
        if not self.reference_id:
            raise ScreeningUnavailable(
                "a screen with no provider reference cannot be re-fetched")
        if self.match_count < 0:
            raise ScreeningUnavailable("match_count cannot be negative")

    @property
    def is_clear(self) -> bool:
        """True only for an unambiguous clear.

        A potential match is NOT clear and MUST NOT auto-resolve to one
        (spec 0004 §3.2). This property exists so no caller has to write that
        comparison itself and get it wrong in one place.
        """
        return self.outcome == CLEAR


class SanctionsScreeningProvider(Protocol):
    """The seam a real screening vendor must satisfy.

    `request_key` is the caller's idempotency key. An implementation MUST
    forward it to the provider (as `Idempotency-Key` or the provider's
    equivalent) so that repeating a call with the same key returns the ORIGINAL
    screen instead of performing a new one -- a duplicate screen writes a second
    piece of evidence about one subject, and two evidence rows that disagree are
    worse than one.

    Identity data MUST travel in the request body. Never a query string.
    """

    def screen(self, *, name: str, dob: str | None, address: str | None,
                     request_key: str) -> ScreeningResult:
        ...


class StubScreeningProvider:
    """Deterministic dev/test provider. NOT a sanctions list.

    It carries no names. A screen returns `potential_match` only when the
    subject name contains `STUB_MATCH_MARKER`, an obviously synthetic string, so
    the match path is testable without committing anything that resembles list
    data.

    Honours the same idempotency contract required of a real provider: a
    repeated `request_key` returns the ORIGINAL result, and `screen_count`
    exposes how many real screens were performed so a retry test can assert that
    an ambiguous timeout did not produce a second one. Process-local by design --
    it stands in for provider-side deduplication, which is where the real
    guarantee has to live, and is not a substitute for it.

    Its `list_version` says what it is. A stub that reported a plausible list
    date would make a stub screen indistinguishable from a real one in the audit
    record, which is the mistake the `-stub` model-version suffix already exists
    to prevent.
    """

    LIST_VERSION = "stub-list-0000-00-00"

    def __init__(self):
        self._by_key: dict[str, ScreeningResult] = {}
        self.screen_count = 0

    def screen(self, *, name: str, dob: str | None, address: str | None,
                     request_key: str) -> ScreeningResult:
        if not request_key:
            raise ScreeningUnavailable(
                "a screen with no request key cannot be replayed safely")

        existing = self._by_key.get(request_key)
        if existing is not None:
            log.info("screening stub replay request_key=%s reference_id=%s",
                     request_key, existing.reference_id)
            return existing

        self.screen_count += 1
        hit = STUB_MATCH_MARKER in (name or "")
        result = ScreeningResult(
            outcome=POTENTIAL_MATCH if hit else CLEAR,
            list_version=self.LIST_VERSION,
            reference_id=f"stub-{request_key}",
            match_count=1 if hit else 0,
        )
        self._by_key[request_key] = result
        # Identifiers and the verdict only. The subject's name, date of birth and
        # address never reach a log line here -- the same rule the CIP route
        # already follows (kyc-service/app/routers/kyc.py).
        log.info("screening stub screen request_key=%s outcome=%s reference_id=%s",
                 request_key, result.outcome, result.reference_id)
        return result

    def reset(self) -> None:
        self._by_key.clear()
        self.screen_count = 0


class HttpScreeningProvider:
    """Real-provider shape. Pins the requirements, proves nothing.

    Not exercised by any test in this repository, because no real endpoint
    exists. What it fixes in advance:

      * identity data in the POST body, never a query string;
      * the caller's idempotency key forwarded as a header, so the provider can
        deduplicate a retried request server-side;
      * a missing or unparseable outcome, or a response with no list version,
        raises rather than being coerced into a verdict;
      * the raw response body is neither returned nor logged. Outcome, list
        version, reference id and candidate count are the whole record.
    """

    def __init__(self, base_url: str = SCREENING_BASE_URL,
                 api_key: str = SCREENING_KEY):
        self._base_url = base_url
        self._api_key = api_key

    def screen(self, *, name: str, dob: str | None, address: str | None,
                     request_key: str) -> ScreeningResult:
        if not request_key:
            raise ScreeningUnavailable(
                "a screen with no request key cannot be replayed safely")
        try:
            with httpx.Client(timeout=30) as client:
                resp = client.post(
                    f"{self._base_url}/screenings",
                    json={                                # body, never ?name=
                        "name": name,
                        "dob": dob,
                        "address": address,
                    },
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Idempotency-Key": request_key,
                    },
                )
                resp.raise_for_status()
                payload = resp.json()
        except Exception as e:  # noqa: BLE001 -- every failure fails closed
            # The exception text is not interpolated: it can carry provider
            # response fragments, which are third-party list data about named
            # individuals. The type is enough for an operator.
            log.error("screening provider failed request_key=%s error=%s",
                      request_key, type(e).__name__)
            raise ScreeningUnavailable(
                "the screening provider did not return a usable result") from None

        try:
            return ScreeningResult(
                outcome=payload["outcome"],
                list_version=payload["list_version"],
                reference_id=payload["reference_id"],
                match_count=int(payload.get("match_count", 0)),
            )
        except ScreeningUnavailable:
            raise
        except Exception:
            log.error("screening provider returned an unusable payload "
                      "request_key=%s", request_key)
            raise ScreeningUnavailable(
                "the screening provider's response could not be read") from None


def provider() -> SanctionsScreeningProvider:
    """The configured provider.

    A stub outside a development environment is a configuration error, not a
    fallback (ADR 0012 §6). `ALLOW_SCREENING_STUB` is the same environment gate
    `ALLOW_MODEL_STUB` uses in decision-service, and an unset ENVIRONMENT counts
    as production: a container that boots without one gets the real shape, and a
    deploy that forgot to configure a provider fails closed at the first screen
    rather than quietly clearing everyone.
    """
    if ALLOW_SCREENING_STUB:
        return StubScreeningProvider()
    return HttpScreeningProvider()
