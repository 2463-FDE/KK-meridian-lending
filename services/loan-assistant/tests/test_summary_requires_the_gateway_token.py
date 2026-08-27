"""The staff summary route must not authorise on a role header alone.

SEC-16, reproduced at runtime against the full Compose stack on 2026-08-27 and
then written down here so it cannot come back:

    POST http://loan-assistant:8007/applications/4471/summary
    X-User-Role: underwriter

    -> 200, with the applicant's name, loan amount, and the financials that
       origination-service refuses to release without a token.

Nothing was spoofed to get that. The route read `X-User-Role`, believed it, and
then spent this service's own `INTERNAL_SERVICE_TOKEN` fetching staff-only data
on the caller's behalf -- a confused deputy, and the more exact description of
it is that origination-service already refuses a role-only caller (`_is_staff`)
and this route was the thing defeating that refusal.

The same run confirmed the boundary is real in the other direction, which is
why this is a gap and not an incident: through the gateway, a borrower session
got 403, a borrower sending `X-User-Role: admin` got 403, and an anonymous
caller sending the same header got 401. The gateway strips inbound `x-user-*`
and re-mints the pair from the session, so nothing outside the Compose network
could reach this. What could reach it is anything already inside -- and inside
is exactly where `X-Internal-Token` is the control this repository uses.

Two asymmetries this closes, both visible in the reproduction:

  * a role-only caller got 200 (should be 403);
  * a caller with NO identity at all got 500, not a refusal -- origination
    returned 403 to the token-without-role fetch and `raise_for_status()` turned
    that into an unhandled error. A missing identity now gets an answer about
    identity.

What is deliberately NOT asserted here: that a principal assertion is required.
Servicing verifies a gateway-signed Ed25519 principal because it moves money and
needs to know WHICH human acted. This route reads. The control that fits it is
the one origination, payment, kyc and decision already use, and inventing a
second scheme for a read path would be a worse answer than reusing the first.
"""
from fastapi.testclient import TestClient

from app import main
from app.schemas import LoanSummary
from tests.conftest import STAFF_HEADERS

client = TestClient(main.app, raise_server_exceptions=False)

APP_ID = 42
TOKEN = STAFF_HEADERS["X-Internal-Token"]


class _Upstream:
    """origination-service, as far as this route is concerned.

    Deliberately unconditional: it answers 200 to anything. If a request ever
    reaches it in a test below that expects a refusal, the refusal did not
    happen and the assertion fails on the status rather than on a stub that
    happened to say no.
    """

    def __init__(self, url):
        self._url = url
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        if "financials" in self._url:
            return {"income": 85000, "employment_years": 4}
        return {"id": APP_ID, "amount": 15000, "term_months": 36,
                "purpose": "debt_consolidation", "decision": "approve"}


def _reaches_upstream(monkeypatch):
    """Records whether the route got far enough to call origination-service."""
    calls = []

    def _get(url, **kwargs):
        calls.append(url)
        return _Upstream(url)

    monkeypatch.setattr(main.httpx, "get", _get)
    monkeypatch.setattr(
        main, "summarize_application",
        lambda app_data: LoanSummary(applicant_name="Test Applicant",
                                     loan_amount=15000.0, term_months=36,
                                     purpose="debt_consolidation",
                                     summary="A summary."))
    return calls


def test_a_role_header_alone_does_not_get_a_summary(monkeypatch):
    """The reproduction, as a test. This returned 200 before the fix."""
    calls = _reaches_upstream(monkeypatch)

    resp = client.post(f"/applications/{APP_ID}/summary",
                       headers={"X-User-Role": "underwriter"})

    assert resp.status_code == 403, (
        "a caller supplying nothing but a role header was answered %d. That is "
        "SEC-16: the role is a claim, and this service spends its own internal "
        "token on it." % resp.status_code)
    assert calls == [], (
        "the route called origination-service before deciding whether the "
        "caller was allowed: %s" % calls)


def test_no_identity_at_all_is_refused_rather_than_erroring(monkeypatch):
    """The second half of the reproduction: no headers used to give 500.

    A 500 tells a caller the service broke. The truth was that the caller had
    no identity, which is a different answer and the one they should get.
    """
    calls = _reaches_upstream(monkeypatch)

    resp = client.post(f"/applications/{APP_ID}/summary")

    assert resp.status_code == 403, (
        "an unidentified caller was answered %d rather than refused"
        % resp.status_code)
    assert calls == []


def test_the_gateway_contract_still_works(monkeypatch):
    """The fix must not close the route to the one caller that should reach it.

    This is the failure mode of the fix, and it is worse than the gap: a
    summary route that 403s the gateway is a broken demo, not a hardened one.
    """
    _reaches_upstream(monkeypatch)

    resp = client.post(f"/applications/{APP_ID}/summary", headers=STAFF_HEADERS)

    assert resp.status_code == 200, (
        "the gateway's own header pair was refused %d -- the staff summary "
        "path is broken end to end" % resp.status_code)


def test_a_wrong_token_is_refused(monkeypatch):
    calls = _reaches_upstream(monkeypatch)

    resp = client.post(f"/applications/{APP_ID}/summary",
                       headers={"X-User-Role": "underwriter",
                                "X-Internal-Token": TOKEN + "x"})

    assert resp.status_code == 403
    assert calls == []


def test_a_borrower_cannot_elevate_with_the_role_header(monkeypatch):
    """Holding the token is not enough; the role still has to be a staff one.

    Both halves are load-bearing. Dropping this one would mean any service on
    the network could ask for any applicant's financials by presenting the
    shared token and the word `borrower`.
    """
    calls = _reaches_upstream(monkeypatch)

    for role in ("borrower", "", "administrator", "UNDERWRITER"):
        resp = client.post(f"/applications/{APP_ID}/summary",
                           headers={"X-User-Role": role,
                                    "X-Internal-Token": TOKEN})
        assert resp.status_code == 403, (
            "role %r was accepted as staff (%d)" % (role, resp.status_code))

    assert calls == []


def test_an_unset_configured_token_refuses_everything(monkeypatch):
    """Fail closed. An empty secret must never be the thing a caller matches.

    Without this, a service started with no `INTERNAL_SERVICE_TOKEN` would
    accept a caller who also sent nothing -- the check would compare "" to ""
    and pass, which is the worst possible reading of "secure by default".
    """
    calls = _reaches_upstream(monkeypatch)
    monkeypatch.setattr(main, "INTERNAL_SERVICE_TOKEN", "")

    for headers in ({"X-User-Role": "underwriter"},
                    {"X-User-Role": "underwriter", "X-Internal-Token": ""},
                    STAFF_HEADERS):
        resp = client.post(f"/applications/{APP_ID}/summary", headers=headers)
        assert resp.status_code == 403, (
            "an unset configured token admitted %s (%d)"
            % (headers, resp.status_code))

    assert calls == []


def test_the_refusal_says_nothing_about_the_application(monkeypatch):
    """A refusal is an authorisation answer, not a data leak.

    The route refuses before it has fetched anything, so there is nothing to
    leak -- but asserting it is what keeps a later "helpful" error message from
    confirming an application id to a caller who was not allowed to ask.
    """
    _reaches_upstream(monkeypatch)

    resp = client.post(f"/applications/{APP_ID}/summary",
                       headers={"X-User-Role": "underwriter"})

    body = resp.text
    assert str(APP_ID) not in body, (
        "the refusal echoed the application id back: %r" % body)
    for leaked in ("income", "employment", "Maria", "amount", "risk"):
        assert leaked.lower() not in body.lower(), (
            "the refusal mentioned %r: %r" % (leaked, body))
