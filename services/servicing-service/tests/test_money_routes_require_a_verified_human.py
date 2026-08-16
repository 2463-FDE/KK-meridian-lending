"""Servicing enforces WHO is acting, by itself, against a signature it cannot forge.

This is G-SERVICING-ROLE. Before it, the csr/admin rule lived only at the gateway
and this service read `x_user_role` and ignored it -- correctly, because
believing it would have been worse: every backend holds the same
`X-Internal-Token`, so any service on the compose network could have stamped
`X-User-Role: admin` and moved a balance. The role check was one hop away from
the money, and a caller that skipped that hop met no role check at all.

The cases below are written from the attacker's side, because that is the only
side that matters here. Each one asserts a refusal AND that the money layer was
never reached -- a handler that adjusts a balance and then returns 401 has still
adjusted the balance.

The keys are generated per test run. Nothing in this repository is a usable key,
and a fixture that committed one would be handing over the ability to mint an
admin (`db/tools/generate_principal_keypair.py` is how a deployment gets a pair).
"""
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from app import main, principal


TOKEN = "test-internal-token"
STAFF_ROUTES = [
    ("/accounts/1/adjust-balance", {"new_balance": 1.0}),
    ("/accounts/1/waive-fee", {"amount": 1.0}),
    ("/accounts/1/late-fee", None),
]


def _keypair():
    private = Ed25519PrivateKey.generate()
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_pem, public_pem


@pytest.fixture
def keys(monkeypatch):
    private_pem, public_pem = _keypair()
    monkeypatch.setattr(main.config, "PRINCIPAL_VERIFY_KEY", public_pem)
    monkeypatch.setattr(principal.config, "PRINCIPAL_VERIFY_KEY", public_pem)
    monkeypatch.setattr(main.config, "INTERNAL_SERVICE_TOKEN", TOKEN)
    return private_pem, public_pem


@pytest.fixture
def no_money(monkeypatch):
    """Explodes if a refused request still reaches the money layer."""
    def _boom(*a, **kw):                                     # pragma: no cover
        raise AssertionError("a refused request reached the balance layer")

    for fn in ("adjust_balance", "waive_fee", "apply_payment", "apply_payment_once"):
        monkeypatch.setattr(main.balance, fn, _boom, raising=False)
    monkeypatch.setattr(main.delinquency, "assess_late_fee", _boom, raising=False)


@pytest.fixture
def money_spy(monkeypatch):
    """Records the money layer being reached, for the paths that SHOULD reach it."""
    calls = []
    monkeypatch.setattr(main.balance, "adjust_balance",
                        lambda loan_id, value: calls.append(("adjust", loan_id)) or 0.0)
    monkeypatch.setattr(main.balance, "waive_fee",
                        lambda loan_id, amount: calls.append(("waive", loan_id)) or 0.0)
    monkeypatch.setattr(main.delinquency, "assess_late_fee",
                        lambda loan_id: calls.append(("late", loan_id)) or 0.0)
    return calls


def _assert(private_pem, **overrides):
    now = int(time.time())
    claims = {
        "iss": "meridian-gateway",
        "aud": "servicing-service",
        "sub": "7",
        "role": "csr",
        "iat": now,
        "nbf": now,
        "exp": now + 120,
        "jti": "test-assertion",
    }
    claims.update(overrides)
    for key in [k for k, v in claims.items() if v is None]:
        del claims[key]
    return jwt.encode(claims, private_pem, algorithm="EdDSA")


def _post(path, body, headers):
    client = TestClient(main.app)
    return client.post(path, json=body, headers=headers) if body is not None \
        else client.post(path, headers=headers)


def _headers(assertion=None, **extra):
    headers = {"X-Internal-Token": TOKEN}
    if assertion is not None:
        headers["X-Principal-Assertion"] = assertion
    headers.update(extra)
    return headers


# --- the happy path, so the refusals below are not vacuous -------------------


@pytest.mark.parametrize("path, body", STAFF_ROUTES)
def test_a_verified_csr_may_move_money(keys, money_spy, path, body):
    private_pem, _ = keys
    response = _post(path, body, _headers(_assert(private_pem, role="csr")))
    assert response.status_code == 200, response.text
    assert money_spy, "the request was accepted but never reached the money layer"


def test_a_verified_admin_may_move_money(keys, money_spy):
    private_pem, _ = keys
    response = _post("/accounts/1/adjust-balance", {"new_balance": 5.0},
                     _headers(_assert(private_pem, role="admin", sub="42")))
    assert response.status_code == 200, response.text


# --- the direct-to-servicing bypass, which is the whole point ----------------


@pytest.mark.parametrize("path, body", STAFF_ROUTES)
def test_the_shared_token_alone_cannot_move_money(keys, no_money, path, body):
    """The bypass G-SERVICING-ROLE existed for.

    A caller inside the compose network holding the shared service token, with no
    human behind it, used to be indistinguishable from a csr acting through the
    gateway. It is now refused by servicing itself.
    """
    response = _post(path, body, _headers())
    assert response.status_code == 401, (
        f"{path} accepted a service token with no human principal ({response.status_code})"
    )


@pytest.mark.parametrize("path, body", STAFF_ROUTES)
def test_forged_identity_headers_with_a_valid_token_are_refused(keys, no_money, path, body):
    """The escalation an implementer would reach for: just read the role header.

    Any backend holds the shared token, so a header it stamps is a header it can
    choose. Without a signature these are worth nothing, and the request is
    refused rather than served at some lower authority.
    """
    response = _post(path, body,
                     _headers(**{"X-User-Id": "1", "X-User-Role": "admin"}))
    assert response.status_code == 401


def test_a_role_header_cannot_disagree_with_the_signature(keys, no_money):
    """A signed csr presenting `X-User-Role: admin` is refused outright.

    Serving it at csr authority would be safe and still wrong: the attempt would
    leave no trace, and the next attempt would be the one that finds a route
    where the header IS read.
    """
    private_pem, _ = keys
    response = _post("/accounts/1/adjust-balance", {"new_balance": 1.0},
                     _headers(_assert(private_pem, role="csr"),
                              **{"X-User-Role": "admin"}))
    assert response.status_code == 401


def test_a_subject_header_cannot_disagree_with_the_signature(keys, no_money):
    private_pem, _ = keys
    response = _post("/accounts/1/waive-fee", {"amount": 1.0},
                     _headers(_assert(private_pem, sub="7"), **{"X-User-Id": "9"}))
    assert response.status_code == 401


# --- forgery, replay and confusion -------------------------------------------


def test_an_assertion_signed_by_another_key_is_refused(keys, no_money):
    """Signature checked, not merely parsed."""
    other_private, _ = _keypair()
    response = _post("/accounts/1/adjust-balance", {"new_balance": 1.0},
                     _headers(_assert(other_private)))
    assert response.status_code == 401


def test_an_hmac_token_forged_with_the_public_key_is_refused(keys, no_money):
    """Algorithm confusion, and it is a live risk here rather than a textbook one.

    The verification key is PUBLIC by design. A verifier that accepted an HMAC
    family would accept a token an attacker signs with that published key as the
    shared secret. The allowlist is one algorithm long for this reason.
    """
    import base64
    import hashlib
    import hmac
    import json

    _, public_pem = keys
    now = int(time.time())

    # Assembled by hand. PyJWT's ENCODER refuses to use a PEM as an HMAC secret,
    # which protects the person writing this test and says nothing about the
    # verifier under test -- an attacker is not using PyJWT to build the forgery.
    # This is the wire format the verifier actually receives.
    def _b64(raw: bytes) -> bytes:
        return base64.urlsafe_b64encode(raw).rstrip(b"=")

    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64(json.dumps({
        "iss": "meridian-gateway", "aud": "servicing-service", "sub": "7",
        "role": "admin", "iat": now, "nbf": now, "exp": now + 120,
    }).encode())
    signing_input = header + b"." + payload
    signature = _b64(hmac.new(public_pem.encode(), signing_input, hashlib.sha256).digest())
    forged = (signing_input + b"." + signature).decode()

    response = _post("/accounts/1/adjust-balance", {"new_balance": 1.0},
                     _headers(forged))
    assert response.status_code == 401, (
        "an HMAC token forged with the PUBLISHED verification key was accepted -- "
        "the algorithm allowlist is not being enforced"
    )


def test_an_unsigned_none_algorithm_token_is_refused(keys, no_money):
    _, _ = keys
    now = int(time.time())
    unsigned = jwt.encode(
        {"iss": "meridian-gateway", "aud": "servicing-service", "sub": "7",
         "role": "admin", "iat": now, "nbf": now, "exp": now + 120},
        key="", algorithm="none",
    )
    response = _post("/accounts/1/adjust-balance", {"new_balance": 1.0},
                     _headers(unsigned))
    assert response.status_code == 401


def test_an_expired_assertion_is_refused(keys, no_money):
    private_pem, _ = keys
    now = int(time.time())
    response = _post("/accounts/1/adjust-balance", {"new_balance": 1.0},
                     _headers(_assert(private_pem, iat=now - 600, nbf=now - 600,
                                      exp=now - 300)))
    assert response.status_code == 401


def test_a_not_yet_valid_assertion_is_refused(keys, no_money):
    private_pem, _ = keys
    now = int(time.time())
    response = _post("/accounts/1/adjust-balance", {"new_balance": 1.0},
                     _headers(_assert(private_pem, iat=now + 3600, nbf=now + 3600,
                                      exp=now + 3720)))
    assert response.status_code == 401


def test_an_assertion_for_another_audience_is_refused(keys, no_money):
    """A perfectly valid signature, addressed elsewhere.

    This is what stops an assertion captured off a different hop -- a future
    origination or disclosure verifier -- being replayed at the money routes.
    """
    private_pem, _ = keys
    response = _post("/accounts/1/adjust-balance", {"new_balance": 1.0},
                     _headers(_assert(private_pem, aud="origination-service")))
    assert response.status_code == 401


def test_an_assertion_from_another_issuer_is_refused(keys, no_money):
    private_pem, _ = keys
    response = _post("/accounts/1/adjust-balance", {"new_balance": 1.0},
                     _headers(_assert(private_pem, iss="not-the-gateway")))
    assert response.status_code == 401


def test_an_assertion_with_no_expiry_is_refused(keys, no_money):
    """Absent must not read as satisfied.

    PyJWT only checks a claim it is asked to require; a token with no `exp` sails
    through an expiry check that has nothing to compare.
    """
    private_pem, _ = keys
    response = _post("/accounts/1/adjust-balance", {"new_balance": 1.0},
                     _headers(_assert(private_pem, exp=None)))
    assert response.status_code == 401


def test_an_over_long_lifetime_is_refused_even_though_it_has_not_expired(keys, no_money):
    """Expiry asks "is it still valid?"; this asks "was it ever allowed to live
    this long?" -- which is what catches a leaked key minting week-long tokens."""
    private_pem, _ = keys
    now = int(time.time())
    response = _post("/accounts/1/adjust-balance", {"new_balance": 1.0},
                     _headers(_assert(private_pem, iat=now, nbf=now,
                                      exp=now + 7 * 24 * 3600)))
    assert response.status_code == 401


def test_a_malformed_assertion_is_refused(keys, no_money):
    response = _post("/accounts/1/adjust-balance", {"new_balance": 1.0},
                     _headers("not-a-jwt-at-all"))
    assert response.status_code == 401


# --- authorization, once identity is settled ---------------------------------


def test_a_verified_underwriter_is_refused_by_servicing_itself(keys, no_money):
    """Staff, authenticated, and still not permitted.

    403 rather than 401: this caller is who they say they are. Underwriter is a
    real staff role that has no business moving money, and until now that rule
    existed only at the gateway.
    """
    private_pem, _ = keys
    response = _post("/accounts/1/adjust-balance", {"new_balance": 1.0},
                     _headers(_assert(private_pem, role="underwriter")))
    assert response.status_code == 403


def test_a_borrower_role_is_refused(keys, no_money):
    private_pem, _ = keys
    response = _post("/accounts/1/waive-fee", {"amount": 1.0},
                     _headers(_assert(private_pem, role="borrower")))
    assert response.status_code == 403


def test_an_assertion_with_no_role_is_refused(keys, no_money):
    private_pem, _ = keys
    response = _post("/accounts/1/adjust-balance", {"new_balance": 1.0},
                     _headers(_assert(private_pem, role=None)))
    assert response.status_code == 401


# --- configuration failures fail closed --------------------------------------


def test_money_routes_refuse_when_no_verification_key_is_configured(monkeypatch, no_money):
    """A dev box with no key does not get a permissive mode.

    `validate_principal_verify_key` lets a development environment boot without
    one -- otherwise the answer would be to paste a key into the repository --
    but every money route then refuses, so the failure is loud and local rather
    than a silent downgrade.
    """
    private_pem, _ = _keypair()
    monkeypatch.setattr(main.config, "INTERNAL_SERVICE_TOKEN", TOKEN)
    monkeypatch.setattr(main.config, "PRINCIPAL_VERIFY_KEY", "")
    monkeypatch.setattr(principal.config, "PRINCIPAL_VERIFY_KEY", "")

    response = _post("/accounts/1/adjust-balance", {"new_balance": 1.0},
                     _headers(_assert(private_pem)))
    assert response.status_code == 401


def test_the_machine_apply_path_still_needs_no_human(keys, monkeypatch):
    """payment-service has no human behind it, and must keep working.

    Spec 0002 §8 keeps machine-originated movements outside the staff workflow
    deliberately. If this failed, closing the role gap would have broken the
    payment path -- the failure mode where a control is added and quietly takes
    an unrelated flow down with it.
    """
    applied = []
    monkeypatch.setattr(main.balance, "apply_payment_once",
                        lambda payment_id, loan_id, amount: (applied.append(payment_id), (0.0, True))[1])
    response = _post("/accounts/1/apply-payment", {"amount": 1.0, "payment_id": 5},
                     _headers())
    assert response.status_code == 200, response.text
    assert applied == [5]
