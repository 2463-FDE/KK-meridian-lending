"""The gateway mints the human principal, and is the only thing that can.

Spec 0002 REQ-ID-1/2/3/6. The claims come from the resolved Redis session and
from nothing the caller sent; the private key lives here alone; and an inbound
`X-Principal-Assertion` is stripped exactly like `x-user-*` and
`x-internal-token`, because it is a statement this gateway makes rather than one
a client may assert.

The compose split is asserted against `docker-compose.yml` itself. That is the
invariant the whole design rests on: if the private half ever reaches a second
service, every service can mint an admin again and the signature proves nothing
that the old shared token did not.
"""
import pathlib
import re
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app import config, principal


REPO = pathlib.Path(__file__).resolve().parents[3]
COMPOSE = REPO / "docker-compose.yml"


def _keypair():
    private = Ed25519PrivateKey.generate()
    return (
        private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode(),
        private.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode(),
    )


@pytest.fixture
def signing_key(monkeypatch):
    private_pem, public_pem = _keypair()
    monkeypatch.setattr(principal, "PRINCIPAL_SIGNING_KEY", private_pem)
    monkeypatch.setattr(config, "PRINCIPAL_SIGNING_KEY", private_pem)
    return private_pem, public_pem


def _decode(assertion, public_pem):
    return jwt.decode(
        assertion, public_pem, algorithms=["EdDSA"],
        audience="servicing-service", issuer="meridian-gateway",
    )


# --- what a minted assertion says --------------------------------------------


def test_the_claims_come_from_the_session(signing_key):
    _, public_pem = signing_key
    claims = _decode(
        principal.mint({"id": 7, "role": "csr", "username": "alice"}), public_pem
    )
    assert claims["sub"] == "7"
    assert claims["role"] == "csr"
    assert claims["iss"] == "meridian-gateway"
    assert claims["aud"] == "servicing-service"


def test_the_assertion_is_short_lived(signing_key):
    _, public_pem = signing_key
    claims = _decode(principal.mint({"id": 7, "role": "csr"}), public_pem)
    lifetime = claims["exp"] - claims["iat"]
    assert lifetime == config.ASSERTION_TTL_SECONDS
    assert lifetime <= 300, (
        f"assertions live {lifetime}s. This is minted per request, not a session; "
        f"a long TTL turns a captured header into a reusable credential"
    )


def test_every_assertion_is_distinguishable(signing_key):
    """Two assertions for one user are not the same string.

    `jti` is not a replay check -- there is no store of seen ids, and the module
    says so rather than implying one -- but an audit trail that cannot tell two
    actions apart is not a trail.
    """
    _, public_pem = signing_key
    user = {"id": 7, "role": "csr"}
    first = _decode(principal.mint(user), public_pem)
    second = _decode(principal.mint(user), public_pem)
    assert first["jti"] != second["jti"]


def test_it_carries_a_not_before_as_well_as_an_expiry(signing_key):
    _, public_pem = signing_key
    claims = _decode(principal.mint({"id": 7, "role": "csr"}), public_pem)
    assert "nbf" in claims, (
        "no nbf claim -- a verifier that only checks expiry accepts a token "
        "minted for the future, which is how a skewed clock becomes an unbounded "
        "lifetime"
    )


def test_it_is_signed_with_eddsa_not_an_hmac(signing_key):
    """The algorithm is the control. An HMAC would make the verify key a minting
    key, which is the property being removed."""
    assertion, _ = principal.mint({"id": 7, "role": "csr"}), None
    header = jwt.get_unverified_header(assertion)
    assert header["alg"] == "EdDSA"


def test_minting_fails_loudly_without_a_key(monkeypatch):
    """No key means no assertion -- never an unsigned one."""
    monkeypatch.setattr(principal, "PRINCIPAL_SIGNING_KEY", "")
    with pytest.raises(config.PrincipalKeyError):
        principal.mint({"id": 7, "role": "csr"})


def test_minting_refuses_a_key_of_the_wrong_type(monkeypatch):
    from cryptography.hazmat.primitives.asymmetric import rsa

    rsa_pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    monkeypatch.setattr(principal, "PRINCIPAL_SIGNING_KEY", rsa_pem)
    with pytest.raises(config.PrincipalKeyError):
        principal.mint({"id": 7, "role": "csr"})


# --- startup validation -------------------------------------------------------


def test_an_unset_key_is_a_boot_failure_outside_development():
    with pytest.raises(config.PrincipalKeyError):
        config.validate_principal_signing_key(environment="production", key_pem="")


def test_an_unset_environment_is_treated_as_production():
    """A container boots without ENVIRONMENT, so "unset" is a reachable
    production state -- the same reasoning `validate_internal_token` uses."""
    with pytest.raises(config.PrincipalKeyError):
        config.validate_principal_signing_key(environment="", key_pem="")


def test_development_may_boot_without_a_key():
    config.validate_principal_signing_key(environment="development", key_pem="")


def test_a_malformed_key_is_refused_even_in_development():
    """Present-but-broken fails everywhere. A key that does not parse fails at
    mint time instead -- on a staff money request."""
    with pytest.raises(config.PrincipalKeyError):
        config.validate_principal_signing_key(
            environment="development", key_pem="-----BEGIN PRIVATE KEY-----\nnope\n"
        )


def test_a_valid_key_passes_validation():
    private_pem, _ = _keypair()
    config.validate_principal_signing_key(environment="production", key_pem=private_pem)


# --- the key split, asserted against compose ----------------------------------


def _compose_env_for(service: str) -> str:
    text = COMPOSE.read_text(encoding="utf-8")
    block = re.split(rf"^  {re.escape(service)}:\s*$", text, flags=re.M)
    assert len(block) == 2, f"{service} is not a compose service"
    rest = block[1]
    nxt = re.search(r"^  [a-z0-9-]+:\s*$", rest, flags=re.M)
    return rest[:nxt.start()] if nxt else rest


def test_only_the_gateway_receives_the_private_key():
    """The invariant everything else rests on.

    If a second service is ever handed PRINCIPAL_SIGNING_KEY, it can mint an
    admin, and the signature becomes exactly the shared secret it replaced.
    """
    text = COMPOSE.read_text(encoding="utf-8")
    holders = [
        service for service in re.findall(r"^  ([a-z0-9-]+):\s*$", text, flags=re.M)
        if "PRINCIPAL_SIGNING_KEY" in _compose_env_for(service)
    ]
    assert holders == ["gateway"], (
        f"PRINCIPAL_SIGNING_KEY is given to {holders}. Only the gateway may hold "
        f"the private half -- anything else can mint a human."
    )


def test_servicing_receives_only_the_public_key():
    env = _compose_env_for("servicing-service")
    assert "PRINCIPAL_VERIFY_KEY" in env, (
        "servicing-service is not given a verification key, so it cannot check a "
        "principal and its money routes refuse everything"
    )
    assert "PRINCIPAL_SIGNING_KEY" not in env, (
        "servicing-service is given the SIGNING key -- it could mint the "
        "identities it is supposed to only verify"
    )


def test_both_keys_are_required_not_defaulted():
    """A `:-default` would put the fail-closed behaviour back to sleep, the same
    way it would for INTERNAL_SERVICE_TOKEN."""
    text = COMPOSE.read_text(encoding="utf-8")
    for var in ("PRINCIPAL_SIGNING_KEY", "PRINCIPAL_VERIFY_KEY"):
        assert f"${{{var}:?" in text, f"{var} is not declared as required in compose"
        assert not re.findall(rf"\$\{{{var}:-[^}}]*\}}", text), (
            f"{var} has a default in compose, so an operator who configures "
            f"nothing still gets a system that looks fine"
        )


def test_no_key_material_is_committed():
    """The rule the generator exists to keep.

    A private key in the repository is not a key -- and unlike the shared token,
    whoever reads it can mint an admin rather than merely reach an endpoint.
    """
    tracked = [
        path for path in REPO.rglob("*")
        if path.is_file()
        and ".git" not in path.parts
        and "node_modules" not in path.parts
        and path.suffix in {".py", ".yml", ".yaml", ".md", ".env", ".example", ".txt"}
    ]
    offenders = []
    for path in tracked:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:                                      # pragma: no cover
            continue
        if "BEGIN PRIVATE KEY" in text and "generate" not in path.name:
            # A test that GENERATES one at runtime is fine; a file that CONTAINS
            # one is not.
            if "Ed25519PrivateKey.generate()" not in text:
                offenders.append(str(path.relative_to(REPO)))
    assert not offenders, f"private key material is committed in {offenders}"


# --- the proxy hop ------------------------------------------------------------


class _FakeResponse:
    def __init__(self):
        self.status_code = 200
        self.content = b'{"ok": true}'
        self.text = '{"ok": true}'

    def json(self):
        return {"ok": True}


class _CapturingClient:
    """Captures what the gateway actually put on the wire."""

    last_headers = None

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def request(self, method, url, content=None, headers=None, params=None):
        _CapturingClient.last_headers = dict(headers or {})
        return _FakeResponse()


@pytest.fixture
def staff_session(monkeypatch, signing_key):
    from app import auth, main

    monkeypatch.setattr(main.httpx, "AsyncClient", _CapturingClient)
    monkeypatch.setattr(auth, "get_session",
                        lambda token: {"id": 7, "role": "csr", "name": "Alice",
                                       "applicant_id": None})
    monkeypatch.setattr(main.principal, "PRINCIPAL_SIGNING_KEY", signing_key[0])
    _CapturingClient.last_headers = None
    return signing_key


def _client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def test_a_money_hop_carries_a_freshly_minted_assertion(staff_session):
    _, public_pem = staff_session
    resp = _client().post(
        "/lss/accounts/1/adjust-balance", json={"new_balance": 10.0},
        headers={"Authorization": "Bearer faketoken123"},
    )
    assert resp.status_code == 200, resp.text

    forwarded = _CapturingClient.last_headers or {}
    assertion = {k.lower(): v for k, v in forwarded.items()}.get("x-principal-assertion")
    assert assertion, "the gateway forwarded no principal assertion on a money route"
    claims = _decode(assertion, public_pem)
    assert claims["sub"] == "7" and claims["role"] == "csr"


def test_an_inbound_assertion_is_replaced_not_forwarded(staff_session):
    """A client-supplied assertion must never survive the hop.

    This is the same rule as `x-user-*` and `x-internal-token`: a claim the
    gateway makes is not one a caller may assert. Header names arrive lowercased,
    so a surviving copy would sit beside the gateway's own under a different
    casing -- and the downstream `Header(alias=...)` returns the FIRST match,
    which is how a stripped-looking header still won an argument once before.
    """
    private_pem, public_pem = staff_session
    attacker = jwt.encode(
        {"iss": "meridian-gateway", "aud": "servicing-service", "sub": "1",
         "role": "admin", "iat": int(time.time()), "nbf": int(time.time()),
         "exp": int(time.time()) + 120},
        private_pem, algorithm="EdDSA",
    )
    resp = _client().post(
        "/lss/accounts/1/adjust-balance", json={"new_balance": 10.0},
        headers={"Authorization": "Bearer faketoken123",
                 "X-Principal-Assertion": attacker},
    )
    assert resp.status_code == 200

    forwarded = {k.lower(): v for k, v in (_CapturingClient.last_headers or {}).items()}
    sent = forwarded.get("x-principal-assertion")
    assert sent != attacker, "the caller's own assertion was forwarded unchanged"
    assert _decode(sent, public_pem)["sub"] == "7", (
        "the forwarded assertion does not describe the resolved session"
    )
    copies = [k for k in (_CapturingClient.last_headers or {})
              if k.lower() == "x-principal-assertion"]
    assert len(copies) == 1, f"two assertion headers went on the wire: {copies}"


def test_a_read_route_carries_no_assertion(staff_session):
    """Least privilege: only the hops that move money get one.

    A read does not need a principal, and minting one anyway would put a valid
    admin-capable credential on hops that have no use for it.
    """
    resp = _client().get("/lss/loans/1",
                         headers={"Authorization": "Bearer faketoken123"})
    assert resp.status_code == 200
    forwarded = {k.lower(): v for k, v in (_CapturingClient.last_headers or {}).items()}
    assert "x-principal-assertion" not in forwarded


# --- the shape a key arrives in ----------------------------------------------


def test_a_pem_survives_a_single_line_env_var():
    """`.env` is line-based and a PEM is not.

    `scripts/bootstrap_env.py` writes the key with its newlines escaped, and
    nothing decoded them: `os.getenv` returns the literal two-character sequence,
    `load_pem_private_key` refuses it with `InvalidByte(0, 92)` -- 92 being the
    backslash -- and every money route would have failed with a 401 that looks
    like an authorization problem rather than a configuration one.

    Three comments in this change claimed the decode existed before it did. It
    was found by running the documented bootstrap and trying to load what it
    wrote, which is the only reason this test exists rather than the claim.
    """
    private_pem, _ = _keypair()
    escaped = private_pem.strip().replace("\n", "\\n")
    assert "\\n" in escaped and "\n" not in escaped, "the fixture is not a one-liner"

    decoded = config._pem_from_env(escaped)
    serialization.load_pem_private_key(decoded.encode(), password=None)


def test_a_pem_with_real_newlines_is_left_alone():
    """Both shapes are real: a secrets manager or a mounted file supplies genuine
    newlines, and mangling those would break the deployment that did it right."""
    private_pem, _ = _keypair()
    assert config._pem_from_env(private_pem) == private_pem


def test_the_bootstrap_generates_a_pair_that_actually_matches():
    """A mismatched pair is the worst failure mode: everything boots, and every
    money route refuses with a 401 that reads as an authorization problem.

    The bootstrap's own functions are exercised rather than the script as a
    whole -- it writes to the repository's `.env` by design (its paths are
    resolved from the script, not the cwd), and a test that ran it for real would
    either rewrite a developer's keys or prove nothing about the code path.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "bootstrap_env_under_test", REPO / "scripts" / "bootstrap_env.py")
    bootstrap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bootstrap)

    private_pem, public_pem = bootstrap._generate_principal_keypair()

    # Through the one-line form bootstrap actually writes, and back out through
    # the decoder the services actually use -- the round trip that was broken.
    private = serialization.load_pem_private_key(
        config._pem_from_env(bootstrap._one_line(private_pem)).encode(), password=None)
    public = serialization.load_pem_public_key(
        config._pem_from_env(bootstrap._one_line(public_pem)).encode())
    assert private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ) == public.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ), "bootstrap wrote a private and public key that are not a pair"
