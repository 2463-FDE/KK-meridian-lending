"""Credit-bureau boundary, behind an interface.

Why this module exists (PR #6 review, Gap A): the bureau pull used to be an
inline `httpx.get` in decision.py with two problems.

1. **The SSN travelled as a URL query parameter** (`?ssn=...`). A query string
   lands in the provider's access logs, in any proxy in between, and in our own
   outbound client logs -- the same leak class the acceptance token was moved out
   of a query parameter to avoid. It is a POST body field here instead.

2. **A retry after an ambiguous timeout started a brand new pull.** Origination
   cannot tell "the bureau never ran" from "the bureau ran and we lost the
   response", so its only safe move is to retry -- and with no idempotency key
   that meant a second, independently-billed hard credit pull against a real
   applicant. Every call now carries a stable `request_key` supplied by
   origination, which is REUSED across a retry of the same logical decision
   request and regenerated for a genuinely new one (see
   origination-service/app/decision_state.py::start_decision_attempt).

`BureauResult.reference_id` is the provider's own handle for the operation. It
is deliberately non-sensitive: it is persisted (decision_attempts.
bureau_reference_id) so a future real-provider implementation can look the
operation up by reference instead of re-pulling. The SSN and the raw provider
response are never persisted or logged.

HONEST LIMITATION -- read before trusting this in production. There is no real
Experian endpoint in this repository: `EXPERIAN_BASE_URL` points at a
placeholder host, and every local/test run uses StubBureauClient. The
idempotency contract below is therefore the contract we would REQUIRE of a real
provider (RFC-style `Idempotency-Key`, deduplicated server-side), and it is
verified only against our own stub. Real-provider behaviour is UNVERIFIED -- no
production guarantee is claimed. HttpBureauClient exists to pin the shape that
implementation must take, not to prove it works.
"""
from dataclasses import dataclass
from typing import Protocol

import httpx

from .config import EXPERIAN_BASE_URL, EXPERIAN_KEY
from .logging_config import get_logger

log = get_logger("decision.bureau")


@dataclass(frozen=True)
class BureauResult:
    """What the bureau boundary returns. `reference_id` is the provider's
    non-sensitive handle for the operation -- safe to persist and log."""
    score: int
    reference_id: str


class BureauClient(Protocol):
    """The seam a real provider implementation must satisfy.

    `request_key` is the caller's idempotency key. An implementation MUST
    forward it to the provider (as `Idempotency-Key` or the provider's
    equivalent) so that repeating a call with the same key returns the
    original operation instead of starting a new one.
    """

    async def pull_score(self, ssn: str, request_key: str) -> BureauResult:
        ...


class StubBureauClient:
    """Deterministic dev/test bureau.

    Honours the same idempotency contract we require of a real provider:
    a repeated `request_key` returns the ORIGINAL result and reference id
    without performing another pull. `pull_count` exposes the number of real
    pulls performed so tests can assert that an ambiguous-timeout retry does
    not double-pull.

    Process-local by design -- this stands in for provider-side deduplication,
    which is where the real guarantee has to live. It is not a substitute for
    it, and it is not a cache of credit data: the key is scoped to one logical
    decision request, so a genuinely new decision request always pulls again.
    """

    def __init__(self):
        self._by_key: dict[str, BureauResult] = {}
        self.pull_count = 0

    @staticmethod
    def _score_for(ssn: str) -> int:
        return 680 if ssn and ssn[-1] in "02468" else 612

    async def pull_score(self, ssn: str, request_key: str) -> BureauResult:
        existing = self._by_key.get(request_key)
        if existing is not None:
            # Same logical request replayed -- return the original operation.
            log.info("bureau stub replay request_key=%s reference_id=%s",
                     request_key, existing.reference_id)
            return existing
        self.pull_count += 1
        result = BureauResult(
            score=self._score_for(ssn),
            reference_id=f"stub-{request_key}",
        )
        self._by_key[request_key] = result
        log.info("bureau stub pull request_key=%s reference_id=%s", request_key, result.reference_id)
        return result

    def reset(self) -> None:
        self._by_key.clear()
        self.pull_count = 0


class HttpBureauClient:
    """Real-provider shape. Pins two requirements for whoever wires up a live
    bureau: the SSN goes in the POST body (never a query string), and the
    caller's idempotency key is forwarded as a header so the provider can
    deduplicate a retried request server-side.

    Not exercised by any test in this repository -- no real endpoint exists.
    """

    def __init__(self, base_url: str = EXPERIAN_BASE_URL, api_key: str = EXPERIAN_KEY):
        self._base_url = base_url
        self._api_key = api_key

    async def pull_score(self, ssn: str, request_key: str) -> BureauResult:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self._base_url}/scores",
                json={"ssn": ssn},                     # body, never ?ssn=
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Idempotency-Key": request_key,    # provider must dedupe on this
                },
            )
        resp.raise_for_status()
        body = resp.json()
        return BureauResult(
            score=body.get("score", 680),
            # Never fall back to echoing the SSN or the raw body here.
            reference_id=str(body.get("reference_id") or f"unknown-{request_key}"),
        )


# Module-level stub instance: dedupe state must survive across requests within
# a process for the idempotency contract to mean anything. decision.py picks
# between this and HttpBureauClient based on whether a real key is configured.
stub_client = StubBureauClient()
