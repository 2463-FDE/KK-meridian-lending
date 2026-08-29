"""The cutover: adjust-balance and waive-fee raise proposals, and only a second
authorised person can make one move money.

This is D8's remaining half. The identity work made it possible -- there is no
point asking "is the approver a different person?" until the answer cannot be
supplied by the caller -- and 0036/0037 made it safe. What is tested here is the
API's part: who may ask, who may say yes, what a proposal does to the balance
(nothing), and that closing this gap did not break the machine paths.

The role matrix comes from spec 0002 §3, and the figures from configuration that
failed closed at boot. The approved cohort/demo values are injected explicitly by
the fixture -- there are no defaults to fall back on, which is the point.
"""
import time
from decimal import Decimal

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from app import main, maker_checker, principal


TOKEN = "test-internal-token"
#: Approved for this cohort/demo environment. Injected, never defaulted.
THRESHOLD = "500.00"
MAX_DELTA = "5000.00"
STATUSES = "current"


@pytest.fixture
def keys(monkeypatch):
    private = Ed25519PrivateKey.generate()
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()).decode()
    public_pem = private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    monkeypatch.setattr(main.config, "PRINCIPAL_VERIFY_KEY", public_pem)
    monkeypatch.setattr(principal.config, "PRINCIPAL_VERIFY_KEY", public_pem)
    monkeypatch.setattr(main.config, "INTERNAL_SERVICE_TOKEN", TOKEN)
    monkeypatch.setattr(main.config, "MAKER_CHECKER_ADMIN_THRESHOLD", THRESHOLD)
    monkeypatch.setattr(main.config, "MAKER_CHECKER_MAX_DELTA", MAX_DELTA)
    monkeypatch.setattr(main.config, "MAKER_CHECKER_PERMITTED_LOAN_STATUSES", STATUSES)
    monkeypatch.setattr(maker_checker.config, "MAKER_CHECKER_ADMIN_THRESHOLD", THRESHOLD)
    monkeypatch.setattr(maker_checker.config, "MAKER_CHECKER_MAX_DELTA", MAX_DELTA)
    monkeypatch.setattr(maker_checker.config, "MAKER_CHECKER_PERMITTED_LOAN_STATUSES",
                        STATUSES)
    return private_pem


@pytest.fixture
def fake_db(monkeypatch):
    """Records what reached the database, and what did not."""
    state = {"inserts": [], "resolves": [], "loan_status": "current", "serviced": True,
             "balance": "1000.00", "past_due": "80.00"}

    def _query(sql, params=None):
        flat = " ".join(sql.split())
        if "FROM loans l LEFT JOIN balances b" in flat:
            return [{"status": state["loan_status"],
                     "serviced": 1 if state["serviced"] else None,
                     "balance": Decimal(state["balance"]),
                     "past_due": Decimal(state["past_due"])}]
        if "INSERT INTO pending_movements" in flat:
            state["inserts"].append(params)
            return [{"id": 7, "requested_at": None}]
        if "SELECT amount, requested_by, resolution FROM pending_movements" in flat:
            return [{"amount": Decimal(state.get("pending_amount", "-100.00")),
                     "requested_by": 1, "resolution": None}]
        if "resolve_pending_movement" in flat:
            state["resolves"].append(params)
            return [{"entry_id": 99}]
        if "FROM pending_movements WHERE resolution IS NULL" in flat:
            return []
        return []

    monkeypatch.setattr(maker_checker.db, "query", _query)
    return state


@pytest.fixture
def no_money(monkeypatch):
    """Explodes if anything in this flow moves a balance directly."""
    def _boom(*a, **kw):                                     # pragma: no cover
        raise AssertionError("a maker-checker route moved money directly")

    for fn in ("adjust_balance", "waive_fee", "apply_payment"):
        monkeypatch.setattr(main.balance, fn, _boom, raising=False)


def _assertion(private_pem, *, sub="1", role="csr", **over):
    now = int(time.time())
    claims = {"iss": "meridian-gateway", "aud": "servicing-service", "sub": sub,
              "role": role, "iat": now, "nbf": now, "exp": now + 120, "jti": "t"}
    claims.update(over)
    return jwt.encode(claims, private_pem, algorithm="EdDSA")


def _headers(private_pem, *, sub="1", role="csr"):
    return {"X-Internal-Token": TOKEN,
            "X-Principal-Assertion": _assertion(private_pem, sub=sub, role=role)}


def _client():
    return TestClient(main.app)


# --- proposing moves no money --------------------------------------------------


def test_adjust_balance_raises_a_proposal_and_moves_nothing(keys, fake_db, no_money):
    response = _client().post(
        "/accounts/1/adjust-balance",
        json={"component": "principal", "amount": -250.00, "reason": "fee reversal"},
        headers=_headers(keys, role="csr"))

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "pending" and body["balance_moved"] is False
    assert len(fake_db["inserts"]) == 1, "no proposal was recorded"


def test_waive_fee_raises_a_proposal_and_moves_nothing(keys, fake_db, no_money):
    response = _client().post(
        "/accounts/1/waive-fee", json={"amount": -40.00, "reason": "goodwill"},
        headers=_headers(keys, role="csr"))
    assert response.status_code == 202, response.text
    assert fake_db["inserts"], "no proposal was recorded"


def test_a_target_balance_can_no_longer_be_submitted(keys, fake_db, no_money):
    """spec 0002 REQ-VAL-1: a proposal is a signed delta, never a target.

    The old field is gone rather than optional -- `extra="forbid"` refuses it, so
    a caller still sending `new_balance` is told, instead of having it silently
    ignored while some other field decides what happens.
    """
    response = _client().post(
        "/accounts/1/adjust-balance", json={"new_balance": 0.00},
        headers=_headers(keys, role="csr"))
    assert response.status_code == 422
    assert not fake_db["inserts"]


# --- who may ask, who may say yes ---------------------------------------------


@pytest.mark.parametrize("role", ["csr", "underwriter", "admin"])
def test_any_staff_role_may_propose(keys, fake_db, no_money, role):
    """REQ-VAL-14 option 2, approved for this environment: any staff principal
    may propose against any serviced, current loan. Recorded as a reviewed
    limitation -- there is no staff-to-loan assignment in this schema."""
    response = _client().post(
        "/accounts/1/adjust-balance",
        json={"component": "principal", "amount": -10.00, "reason": "small fix"},
        headers=_headers(keys, sub="1", role=role))
    assert response.status_code == 202, response.text


def test_a_csr_may_never_approve_any_amount(keys, fake_db, no_money):
    """Not a threshold question. A CSR may raise and may never resolve."""
    fake_db["pending_amount"] = "-1.00"
    response = _client().post(
        "/movements/7/resolve", json={"resolution": "approved"},
        headers=_headers(keys, sub="2", role="csr"))
    assert response.status_code == 403
    assert not fake_db["resolves"], "a csr reached the resolve function"


def test_a_csr_may_not_reject_either(keys, fake_db, no_money):
    """Rejecting is an authorisation decision too (spec 0002 §3): a CSR must not
    be able to dispose of a proposal by refusing it."""
    response = _client().post(
        "/movements/7/resolve", json={"resolution": "rejected"},
        headers=_headers(keys, sub="2", role="csr"))
    assert response.status_code == 403
    assert not fake_db["resolves"]


def test_an_underwriter_may_approve_at_or_below_the_threshold(keys, fake_db, no_money):
    fake_db["pending_amount"] = "-500.00"
    response = _client().post(
        "/movements/7/resolve", json={"resolution": "approved"},
        headers=_headers(keys, sub="2", role="underwriter"))
    assert response.status_code == 200, response.text
    assert fake_db["resolves"], "the resolution never reached the database"


def test_an_underwriter_may_not_approve_above_the_threshold(keys, fake_db, no_money):
    fake_db["pending_amount"] = "-500.01"
    response = _client().post(
        "/movements/7/resolve", json={"resolution": "approved"},
        headers=_headers(keys, sub="2", role="underwriter"))
    assert response.status_code == 403
    assert "threshold" in response.json()["detail"]
    assert not fake_db["resolves"]


def test_an_admin_may_approve_above_the_threshold(keys, fake_db, no_money):
    fake_db["pending_amount"] = "-4999.99"
    response = _client().post(
        "/movements/7/resolve", json={"resolution": "approved"},
        headers=_headers(keys, sub="3", role="admin"))
    assert response.status_code == 200, response.text


def test_the_threshold_actually_used_is_returned_and_recorded(keys, fake_db, no_money):
    """spec 0002 AC-22: the bar a decision was judged against travels with it."""
    fake_db["pending_amount"] = "-100.00"
    response = _client().post(
        "/movements/7/resolve", json={"resolution": "approved"},
        headers=_headers(keys, sub="2", role="underwriter"))
    assert response.json()["threshold_applied"] == 500.00
    assert Decimal(str(fake_db["resolves"][0][4])) == Decimal(THRESHOLD)


# --- refuse at creation --------------------------------------------------------


@pytest.mark.parametrize("amount, why", [
    (-5000.01, "above the approved maximum"),
    (5000.01, "above the approved maximum, positive"),
    (0, "a movement of nothing"),
])
def test_a_proposal_outside_the_approved_bounds_is_refused(keys, fake_db, no_money,
                                                           amount, why):
    response = _client().post(
        "/accounts/1/adjust-balance",
        json={"component": "principal", "amount": amount, "reason": why},
        headers=_headers(keys, role="admin"))
    assert response.status_code == 422, f"{why} was accepted"
    assert not fake_db["inserts"]


def test_the_maximum_applies_to_admins_too(keys, fake_db, no_money):
    """REQ-VAL-6 says what may be ASKED, which is a different question from who
    may say yes. An admin is not exempt."""
    response = _client().post(
        "/accounts/1/adjust-balance",
        json={"component": "principal", "amount": -5000.01, "reason": "too big"},
        headers=_headers(keys, sub="3", role="admin"))
    assert response.status_code == 422


@pytest.mark.parametrize("reason", ["", "   ", "\t"])
def test_a_proposal_without_a_reason_is_refused(keys, fake_db, no_money, reason):
    response = _client().post(
        "/accounts/1/adjust-balance",
        json={"component": "principal", "amount": -10.00, "reason": reason},
        headers=_headers(keys, role="csr"))
    assert response.status_code == 422
    assert not fake_db["inserts"]


def test_a_positive_fee_waiver_is_refused(keys, fake_db, no_money):
    response = _client().post(
        "/accounts/1/waive-fee", json={"amount": 40.00, "reason": "wrong direction"},
        headers=_headers(keys, role="csr"))
    assert response.status_code == 422


@pytest.mark.parametrize("status", ["closed", "charged_off", "delinquent", "CURRENT", ""])
def test_a_proposal_against_an_unpermitted_status_is_refused(keys, fake_db, no_money,
                                                             status):
    """Exactly `{"current"}` for this environment, compared case-sensitively:
    normalising would accept a status nobody approved."""
    fake_db["loan_status"] = status
    response = _client().post(
        "/accounts/1/adjust-balance",
        json={"component": "principal", "amount": -10.00, "reason": "wrong status"},
        headers=_headers(keys, role="csr"))
    assert response.status_code == 422
    assert not fake_db["inserts"]


def test_a_proposal_against_an_unserviced_loan_is_refused(keys, fake_db, no_money):
    fake_db["serviced"] = False
    response = _client().post(
        "/accounts/1/adjust-balance",
        json={"component": "principal", "amount": -10.00, "reason": "no balances row"},
        headers=_headers(keys, role="csr"))
    assert response.status_code == 422


# --- identity still governs everything -----------------------------------------


@pytest.mark.parametrize("path, body", [
    ("/accounts/1/adjust-balance",
     {"component": "principal", "amount": -10.0, "reason": "x"}),
    ("/accounts/1/waive-fee", {"amount": -10.0, "reason": "x"}),
    ("/movements/7/resolve", {"resolution": "approved"}),
    ("/movements", None),
])
def test_the_service_token_alone_cannot_reach_any_of_it(keys, fake_db, no_money,
                                                        path, body):
    """The maker-checker routes inherit the identity boundary rather than
    reimplementing it: a service token with no human behind it is refused."""
    client = _client()
    response = (client.post(path, json=body, headers={"X-Internal-Token": TOKEN})
                if body is not None
                else client.get(path, headers={"X-Internal-Token": TOKEN}))
    assert response.status_code == 401
    assert not fake_db["inserts"] and not fake_db["resolves"]


def test_a_forged_role_header_cannot_raise_authority(keys, fake_db, no_money):
    """A signed csr claiming admin in a header is refused outright, not served at
    csr authority -- serving it would leave the attempt invisible."""
    headers = _headers(keys, sub="2", role="csr")
    headers["X-User-Role"] = "admin"
    response = _client().post("/movements/7/resolve", json={"resolution": "approved"},
                              headers=headers)
    assert response.status_code == 401
    assert not fake_db["resolves"]


def test_the_queue_needs_a_verified_principal_but_no_authority(keys, fake_db):
    """Visibility is not authority: any staff role may read the queue, and
    reading it approves nothing."""
    response = _client().get("/movements", headers=_headers(keys, role="csr"))
    assert response.status_code == 200
    assert response.json() == {"movements": []}


# --- the machine paths must keep working ---------------------------------------


def test_apply_payment_is_untouched_by_the_cutover(keys, monkeypatch):
    """payment-service has no human behind it (spec 0002 §8). A control that
    quietly took the payment path down with it would be its own defect."""
    applied = []
    monkeypatch.setattr(main.balance, "apply_payment_once",
                        lambda p, l, a, **kw: (applied.append(p), (0.0, True))[1])
    response = _client().post("/accounts/1/apply-payment",
                              json={"amount": 1.0, "payment_id": 5},
                              headers={"X-Internal-Token": TOKEN})
    assert response.status_code == 200, response.text
    assert applied == [5]


def test_late_fee_still_requires_a_human_and_still_works(keys, monkeypatch):
    """Grouped with the staff routes because nothing automated calls it. It is
    NOT a proposal: assessing a late fee is machine-originated arithmetic on a
    schedule, not a discretionary movement (spec 0002 §8)."""
    monkeypatch.setattr(main.delinquency, "assess_late_fee", lambda loan_id: 35.0)
    response = _client().post("/accounts/1/late-fee",
                              headers=_headers(keys, sub="2", role="csr"))
    assert response.status_code == 200, response.text


def test_late_fee_on_a_current_loan_is_a_409_not_a_500(keys, monkeypatch):
    """LF-API-NOFEE. A refusal must reach the caller AS a refusal.

    `late_fee_for` raises `NoFeeIsDue` when the arrears rule it computes yields no fee
    -- a loan with no arrears, a credit balance, or arrears so small that five
    per cent is under a cent. The route caught only `LoanHasNoBalances`, so
    `@app.exception_handler(Exception)` turned this into
    `{"detail": "internal error"}` with status 500.

    **This is the second time this shape has appeared in this repository.**
    PR #38's review caught `LoanHasNoBalances` doing exactly the same thing, and
    the late-fee change then added a new named exception without a landing pad.
    A named refusal with no route mapping is indistinguishable from a crash to
    whoever called, so the test is written at the route rather than the helper.
    """
    def _refuse(loan_id):
        raise main.delinquency.NoFeeIsDue(
            f"past_due is 0.00; the schedule charges nothing")

    monkeypatch.setattr(main.delinquency, "assess_late_fee", _refuse)
    response = _client().post("/accounts/1/late-fee",
                              headers=_headers(keys, sub="2", role="csr"))

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail != "internal error", (
        "the refusal reached the caller through the catch-all handler"
    )
    assert "charges nothing" in detail, (
        f"the 409 does not say why no fee was charged: {detail!r}"
    )


def test_late_fee_on_a_loan_with_no_balance_is_a_404_not_a_500(keys, monkeypatch):
    """LF-API-500. The refusal has to reach the caller AS a refusal.

    `assess_late_fee` raises `LoanHasNoBalances` when the fee would land on a
    loan with no `balances` row. The route called it directly, and
    `@app.exception_handler(Exception)` turns anything uncaught into
    `{"detail": "internal error"}` with status 500 -- so the caller saw an
    opaque server error.

    That made the change it was part of false. The old code returned 35.0 for a
    loan whose balance never moved; replacing a wrong number with a server error
    is not the visible refusal it was replaced for, and an operator reading a
    500 has no way to tell a missing balance from a crash.

    The previous test in this file only covered the success path, and the
    conversion's own tests exercise the helper rather than the route -- so
    nothing had ever asserted what a caller receives. Reviewed on PR #38.
    """
    def _refuse(loan_id):
        raise main.delinquency.LoanHasNoBalances(
            f"no balances row for loan_id={loan_id}")

    monkeypatch.setattr(main.delinquency, "assess_late_fee", _refuse)
    response = _client().post("/accounts/1/late-fee",
                              headers=_headers(keys, sub="2", role="csr"))

    assert response.status_code == 404, response.text
    detail = response.json()["detail"]
    # Not the generic handler's body: that is the failure this test exists for,
    # and a 404 carrying "internal error" would be the same defect renumbered.
    assert detail != "internal error", (
        "the refusal reached the caller through the catch-all handler"
    )
    assert "balance" in detail.lower(), (
        f"the 404 does not say what was missing: {detail!r}"
    )


# --- review round 1 -------------------------------------------------------------


@pytest.mark.parametrize("component, past_due, balance, amount, entry_type", [
    ("fees", "40.00", "1000.00", -80.00, "fee_waived"),
    ("fees", "40.00", "1000.00", -80.00, "adjustment"),
    ("principal", "40.00", "100.00", -100.01, "adjustment"),
    # No fees owed at all, which is the case the servicing form used to offer:
    # an operator typed 350 against a zero fee balance, the client sent -350,
    # and the refusal arrived from the server. The form stops asking now, and
    # this is the proof that stopping asking is a usability guard rather than the
    # control -- a direct caller still gets a 422.
    ("fees", "0.00", "1000.00", -350.00, "fee_waived"),
])
def test_a_movement_below_zero_is_refused_at_creation(keys, fake_db, no_money,
                                                      component, past_due, balance,
                                                      amount, entry_type):
    """MC-NEG-CREATE. spec 0002 AC-20 refuses at creation AND at approval.

    Only the approval check existed. A waiver larger than the fees owed was
    accepted, returned 202 and sat in the queue until an approver hit the
    failure -- so the person who could fix the request was never told, and the
    approver was shown a movement the system had already decided was impossible.

    The approval check stays: the balance moves while a proposal waits, so a
    request that is valid now can be invalid then. This one exists so the maker
    finds out.
    """
    fake_db["balance"] = balance
    fake_db["past_due"] = past_due
    path = ("/accounts/1/waive-fee" if entry_type == "fee_waived"
            else "/accounts/1/adjust-balance")
    body = ({"amount": amount, "reason": "more than is owed"}
            if entry_type == "fee_waived"
            else {"component": component, "amount": amount,
                  "reason": "more than is owed"})

    response = _client().post(path, json=body, headers=_headers(keys, role="csr"))

    assert response.status_code == 422, response.text
    assert "below zero" in response.json()["detail"]
    assert not fake_db["inserts"], "the impossible proposal was queued anyway"


def test_a_movement_that_exactly_empties_a_component_is_allowed(keys, fake_db, no_money):
    """Guards the refusal above: `<= 0` instead of `< 0` would forbid waiving
    exactly what is owed, which is the most ordinary waiver there is."""
    fake_db["past_due"] = "40.00"
    response = _client().post(
        "/accounts/1/waive-fee", json={"amount": -40.00, "reason": "waive it all"},
        headers=_headers(keys, role="csr"))
    assert response.status_code == 202, response.text
    assert fake_db["inserts"]
