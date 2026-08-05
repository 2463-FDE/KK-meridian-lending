"""PR #6 review, Gap A -- the bureau boundary's idempotency contract.

Origination cannot distinguish "the bureau never ran" from "the bureau ran and
we lost the response" when its HTTP client times out, so its only safe move is
to retry. Without an idempotency key that retry is a SECOND billable hard
credit inquiry against a real applicant.

These tests cover decision-service's half of the fix: the bureau client
deduplicates on the caller's stable `request_key`. Origination's half -- that
the key actually stays the same across an ambiguous-timeout retry, and that
only ONE permanent decision event is committed -- is covered in
origination-service/tests/test_decision_attempt_real_postgres.py.

Scope honesty: StubBureauClient stands in for provider-side deduplication.
There is no real Experian endpoint in this repository, so what is proven here
is that our own stub honours the contract we would require of a provider, NOT
that any real provider does. See app/bureau.py's module docstring.
"""
import inspect

import pytest

from app import bureau, decision


@pytest.fixture(autouse=True)
def _reset_stub():
    bureau.stub_client.reset()
    yield
    bureau.stub_client.reset()


async def test_repeated_request_key_performs_exactly_one_pull():
    """The core contract: same key replayed -> one real pull, identical result."""
    client = bureau.StubBureauClient()

    first = await client.pull_score("123456782", "stable-key-1")
    second = await client.pull_score("123456782", "stable-key-1")
    third = await client.pull_score("123456782", "stable-key-1")

    assert client.pull_count == 1, "a replayed request_key must not re-pull the bureau"
    assert first == second == third
    assert first.reference_id == second.reference_id


async def test_a_different_request_key_is_a_genuinely_new_pull():
    """The other half of the boundary: this is an idempotency key, NOT a credit
    cache. A genuinely new decision request must reach the bureau again rather
    than be served a stale score."""
    client = bureau.StubBureauClient()

    await client.pull_score("123456782", "request-one")
    await client.pull_score("123456782", "request-two")

    assert client.pull_count == 2
    assert client._by_key["request-one"].reference_id != client._by_key["request-two"].reference_id


async def test_reference_id_is_non_sensitive_and_never_echoes_the_ssn():
    """bureau_reference_id is persisted by origination, so it must not carry
    any part of the applicant's SSN."""
    client = bureau.StubBureauClient()
    ssn = "123456782"

    result = await client.pull_score(ssn, "key-for-reference-check")

    assert ssn not in result.reference_id
    assert ssn[-4:] not in result.reference_id


async def test_pull_credit_threads_the_key_through_and_returns_a_reference(monkeypatch):
    """decision._pull_credit must pass the caller's key to the client and
    surface the provider reference, not just a bare score."""
    monkeypatch.setattr(decision, "EXPERIAN_KEY", "")
    monkeypatch.setattr(decision, "ALLOW_CREDIT_STUB", True)

    first = await decision._pull_credit("123456782", "shared-key")
    second = await decision._pull_credit("123456782", "shared-key")

    assert first.score == second.score == 680
    assert first.reference_id == second.reference_id
    assert bureau.stub_client.pull_count == 1


def test_http_client_sends_the_ssn_in_a_body_and_the_key_as_a_header():
    """Static guard on the real-provider shape. The SSN must never travel as a
    URL query parameter (it lands in the provider's access logs, any proxy in
    between, and our own outbound client logs), and the idempotency key must be
    forwarded so the provider can deduplicate a retry server-side.

    Asserted against the source of HttpBureauClient.pull_score because there is
    no real endpoint to exercise -- see the module docstring's scope note."""
    src = inspect.getsource(bureau.HttpBureauClient.pull_score)

    assert "json={" in src, "the SSN must be sent in a POST body"
    assert 'params=' not in src, "the SSN must never travel as a URL query parameter"
    assert "Idempotency-Key" in src, "the caller's key must be forwarded to the provider"
    assert "client.post(" in src
