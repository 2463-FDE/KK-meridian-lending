"""The gateway must not sign an anonymous caller's request to kyc-service.

The defect this pins, in full, because it survived one fix already:

`kyc-service` was host-published on 8003 with no auth, so anyone on the host
could `POST /kyc/check` and write a `kyc_checks` row for any `applicant_id` --
forged CIP evidence in the record a BSA/AML programme relies on. The first fix
removed the host port and added an `X-Internal-Token` check to the service.

That fix was ineffective. This gateway's `/kyc/{path}` route required no session
and attached the trusted token itself, so the anonymous caller simply moved to
port 8000 -- the one port deliberately published -- and had the gateway sign the
request on its behalf. Verified live against the running stack before this test
existed: `POST localhost:8000/kyc/kyc/check` returned 200 and wrote a real row.

Two things made it invisible for as long as it was:

  * the token check *looked* like the control, and reviewing it in isolation is
    convincing -- the question that matters is not "is the token required" but
    "who can obtain one", and the gateway was handing it to anyone;
  * no test exercised the `/kyc/*` route at all. CI was 22/22 green on the branch
    that claimed to close the bypass. A green run is evidence about what is
    tested, not about what is true.

So these tests assert on the *effect* -- what reaches kyc-service -- rather than
on the route's configuration, which is what the previous round got wrong.

The legitimate anonymous path to CIP is `POST /los/applications`: origination
derives the applicant and application identity from the row it has just written
instead of trusting the caller's body, then calls kyc-service server-to-server.
That path is unaffected by this change and is covered by
`origination-service/tests/test_intake_forwards_the_internal_token_to_kyc.py`
and by `frontend/e2e/approved-workflow.spec.ts`.
"""
import httpx
import pytest
from fastapi.testclient import TestClient

from app import auth, main


_CIP_BODY = {
    "application_id": 1,
    "applicant_id": 1,
    "name": "Forged Owner",
    "dob": "1990-01-01",
    "ssn": "123456782",
    "address": "1 Attacker Way",
}


@pytest.fixture
def upstream(monkeypatch):
    """Records anything that reaches a downstream service.

    An empty list is the assertion that matters: a request the gateway refused
    must not arrive at kyc-service at all. Checking only the status code would
    pass on a gateway that forwarded the write and then relayed a 4xx back.
    """
    seen = []

    class _Response:
        status_code = 200
        content = b'{"check_id": 1, "cip_passed": true}'

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, method, url, content=None, headers=None, params=None):
            seen.append({"method": method, "url": url, "headers": dict(headers or {})})
            return _Response()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return seen


@pytest.fixture
def client():
    return TestClient(main.app)


def _staff_session(monkeypatch, role="admin"):
    monkeypatch.setattr(auth, "get_session", lambda token: {"id": 1, "role": role} if token else None)


# --- the bypass itself -------------------------------------------------------

def test_anonymous_post_never_reaches_kyc_service(client, upstream, monkeypatch):
    monkeypatch.setattr(auth, "get_session", lambda token: None)

    resp = client.post("/kyc/kyc/check", json=_CIP_BODY)

    assert resp.status_code == 401
    assert upstream == [], (
        "an unauthenticated POST reached kyc-service through the gateway. This is "
        "the original defect: the gateway attaches X-Internal-Token itself, so "
        "forwarding an anonymous request means signing it on the caller's behalf "
        "and letting them write a kyc_checks row for any applicant_id."
    )


def test_a_borrower_session_is_not_enough(client, upstream, monkeypatch):
    """Authenticated is not authorized.

    A borrower has a real session, so a check that only required *a* session
    would pass here while still letting any registered user forge CIP evidence
    for a stranger's applicant_id.
    """
    monkeypatch.setattr(auth, "get_session", lambda token: {"id": 9, "role": "borrower", "applicant_id": 9})

    resp = client.post("/kyc/kyc/check", json=_CIP_BODY, headers={"Authorization": "Bearer t"})

    assert resp.status_code == 403
    assert upstream == []


def test_anonymous_get_is_refused_too(client, upstream, monkeypatch):
    """The route accepts GET as well; both verbs go through the same gate."""
    monkeypatch.setattr(auth, "get_session", lambda token: None)

    resp = client.get("/kyc/anything")

    assert resp.status_code == 401
    assert upstream == []


def test_staff_may_still_reach_kyc_service(client, upstream, monkeypatch):
    """The gate must not break the ops/staff path it exists to serve.

    A GET, since review round 2 made this route read-only: the write path is
    origination's server-to-server call, not a staff request body.
    """
    _staff_session(monkeypatch)

    resp = client.get("/kyc/kyc/status", headers={"Authorization": "Bearer t"})

    assert resp.status_code == 200
    assert len(upstream) == 1
    assert upstream[0]["url"].endswith("/kyc/status")
    assert upstream[0]["headers"].get("X-Internal-Token") == main.INTERNAL_SERVICE_TOKEN


# --- the header the gateway signs with ---------------------------------------

def test_a_client_supplied_internal_token_is_stripped(client, upstream, monkeypatch):
    """A caller must not be able to substitute the header the gateway asserts.

    Header names arrive lowercased, so a client's `X-Internal-Token: junk` used
    to survive the inbound filter as `x-internal-token` while `headers.update()`
    added `X-Internal-Token` as a separate key. Both reached the wire and the
    downstream `Header(alias=...)` read the first -- the client's. Any caller
    could therefore force a 401 on every internal-token route.

    Asserted by inspecting what the gateway actually sends, not by status code:
    a status check cannot tell "the client's token was stripped" from "the
    client's token won and the upstream rejected it", and those are opposite
    outcomes. That ambiguity is why the live probe for this was inconclusive.
    """
    _staff_session(monkeypatch)

    client.get(
        "/kyc/kyc/status",
        headers={"Authorization": "Bearer t", "X-Internal-Token": "junk-from-client"},
    )

    assert len(upstream) == 1
    sent = upstream[0]["headers"]
    values = {k.lower(): v for k, v in sent.items()}
    assert values.get("x-internal-token") == main.INTERNAL_SERVICE_TOKEN
    assert "junk-from-client" not in sent.values(), (
        f"the client's token survived into the outbound request: {sent}"
    )
    # Exactly one spelling of the header may reach the wire.
    assert sum(1 for k in sent if k.lower() == "x-internal-token") == 1


def test_the_strip_covers_every_proxied_route(client, upstream, monkeypatch):
    """The filter lives in _proxy, so /decision/* gets it too.

    Kept because the defect was in the shared helper, not in one route -- fixing
    only the route that surfaced it would leave the same hole on four others.
    """
    _staff_session(monkeypatch)

    client.post(
        "/decision/decisions",
        json={"application_id": 1},
        headers={"Authorization": "Bearer t", "X-Internal-Token": "junk-from-client"},
    )

    assert len(upstream) == 1
    assert "junk-from-client" not in upstream[0]["headers"].values()


# --- review round 2: staff-only was not enough --------------------------------

def test_staff_cannot_write_kyc_evidence_through_the_gateway(client, upstream, monkeypatch):
    """A CSR must not be able to mint CIP evidence for an invented applicant.

    Review finding: making the route staff-only closed the anonymous path and
    left the write. This proxy attaches the trusted token, and kyc-service
    persisted whatever `applicant_id` the body named -- so any CSR, underwriter
    or admin could POST an invented applicant and create durable identity
    evidence against a stranger. The same forgery as before, now requiring the
    weakest staff role instead of no session at all.

    The route exists so staff can INSPECT kyc-service, and inspection is a read.
    The mutating endpoint has exactly one legitimate caller -- origination, which
    reaches it server-to-server and derives the applicant from the row it has
    just written rather than from a request body.
    """
    _staff_session(monkeypatch, role="csr")

    resp = client.post("/kyc/kyc/check", json=_CIP_BODY, headers={"Authorization": "Bearer t"})

    assert resp.status_code == 405
    assert upstream == [], "a staff POST reached kyc-service and could have written evidence"


@pytest.mark.parametrize("role", ["csr", "underwriter", "admin"])
def test_no_staff_role_may_write(client, upstream, monkeypatch, role):
    """Including admin: this is not about seniority, it is about the route.

    An admin has every legitimate reason to read KYC and none to author a CIP
    result by hand -- that is what the intake flow is for.
    """
    _staff_session(monkeypatch, role=role)

    assert client.post("/kyc/kyc/check", json=_CIP_BODY,
                       headers={"Authorization": "Bearer t"}).status_code == 405
    assert upstream == []


def test_staff_may_still_read(client, upstream, monkeypatch):
    """The inspection path this route exists for must keep working."""
    _staff_session(monkeypatch)

    resp = client.get("/kyc/health", headers={"Authorization": "Bearer t"})

    assert resp.status_code == 200
    assert len(upstream) == 1
    assert upstream[0]["method"] == "GET"
