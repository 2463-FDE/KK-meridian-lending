"""Every money-moving servicing route must refuse a caller without the token.

`servicing-service` moves money -- adjust a balance, waive a fee, assess a late
fee, post a captured payment -- and until now **not one of those routes checked
anything at all**. Its own module docstring said so: "accept ANY authenticated
caller". The only thing between a caller and the money was `docker-compose.yml`
declining to publish port 8002.

That is the position `kyc-service` was in, twice. First it turned out to be
host-published after all. Then, once that was fixed, it turned out to be
reachable through an anonymous gateway relay that attached the trusted token on
the caller's behalf. Both times the topology was believed to be the guarantee and
both times it was not, and the second time a green 22/22 CI run said nothing
about it because no test exercised the route. `DEBT.md` D8.

**Coverage is derived, not listed.** The single most expensive lesson from the
kyc work was that a hand-written list of protected routes reads exactly like a
complete one -- the regression test there named four services and silently
omitted the fifth for two months. So `test_every_money_route_is_guarded` below
enumerates the app's own routes and fails if a mutating one is unguarded. A new
`POST /accounts/{id}/write-off` is covered the moment it is added, without anyone
remembering this file exists.

Read routes are deliberately not guarded here: `GET /accounts/{id}/balance` and
the loan reads are ownership-checked at the gateway, and widening this change to
them would be a different concern with a different blast radius.
"""
import pytest
from fastapi.testclient import TestClient

from app import main


client = TestClient(main.app)

TOKEN = "test-internal-token"

# Every route below moves money. The bodies are the minimum each schema accepts;
# what is asserted is the authorization outcome, never the arithmetic.
MONEY_ROUTES = [
    ("/accounts/1/adjust-balance", {"new_balance": 1.0}),
    ("/accounts/1/waive-fee", {"amount": 1.0}),
    ("/accounts/1/late-fee", None),
    ("/accounts/1/apply-payment", {"amount": 1.0, "payment_id": 1}),
    ("/payments", {"loan_id": 1, "processor_token": "tok_x", "last4": "1111",
                   "brand": "visa", "amount": 1.0, "method": "card"}),
]


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setattr(main.config, "INTERNAL_SERVICE_TOKEN", TOKEN)


@pytest.fixture
def no_writes(monkeypatch):
    """Fails loudly if a rejected request still reaches the money layer.

    Asserting only on the status code would pass on a handler that moved the
    money and *then* returned 401, which is the outcome that actually matters
    here -- the balance does not un-move because the response was an error.
    """
    def _boom(*a, **kw):                                    # pragma: no cover
        raise AssertionError("a rejected request reached the balance layer")

    for fn in ("adjust_balance", "waive_fee", "apply_payment", "apply_payment_once"):
        monkeypatch.setattr(main.balance, fn, _boom, raising=False)
    monkeypatch.setattr(main.delinquency, "assess_late_fee", _boom, raising=False)
    monkeypatch.setattr(main.payments, "charge", _boom, raising=False)


@pytest.mark.parametrize("path, body", MONEY_ROUTES)
@pytest.mark.parametrize(
    "headers, why",
    [
        ({}, "no token -- a bare same-network caller"),
        ({"X-Internal-Token": ""}, "empty token"),
        ({"X-Internal-Token": "wrong"}, "a guessed or stale token"),
        ({"X-User-Role": "admin"}, "a spoofed role header instead of the token"),
    ],
)
def test_money_route_refuses_a_caller_without_the_token(path, body, headers, why, no_writes):
    resp = client.post(path, json=body, headers=headers)

    assert resp.status_code == 401, f"{path} accepted a caller with {why}"


def test_an_unset_server_token_fails_closed(monkeypatch, no_writes):
    """A deploy that forgets INTERNAL_SERVICE_TOKEN must refuse, not accept.

    The comparison is `not TOKEN or header != TOKEN`, so an empty server-side
    value cannot be matched -- not even by a caller sending an empty header,
    which a bare `!=` would have let straight through.
    """
    monkeypatch.setattr(main.config, "INTERNAL_SERVICE_TOKEN", "")

    for path, body in MONEY_ROUTES:
        for headers in ({}, {"X-Internal-Token": ""}, {"X-Internal-Token": TOKEN}):
            assert client.post(path, json=body, headers=headers).status_code == 401, path


def test_the_token_still_admits_the_legitimate_callers(monkeypatch):
    """The guard is worthless if it also breaks the gateway and payment-service.

    Only the authorization boundary is exercised; the money layer is stubbed so
    this asserts "the request was allowed through", not any balance arithmetic.
    """
    monkeypatch.setattr(main.balance, "adjust_balance", lambda loan_id, v: 42.0)
    monkeypatch.setattr(main.balance, "waive_fee", lambda loan_id, v: 7.0)
    monkeypatch.setattr(main.delinquency, "assess_late_fee", lambda loan_id: 35.0)
    monkeypatch.setattr(main.balance, "apply_payment_once", lambda p, l, a: (99.0, True))

    auth = {"X-Internal-Token": TOKEN}
    assert client.post("/accounts/1/adjust-balance", json={"new_balance": 1.0}, headers=auth).status_code == 200
    assert client.post("/accounts/1/waive-fee", json={"amount": 1.0}, headers=auth).status_code == 200
    assert client.post("/accounts/1/late-fee", headers=auth).status_code == 200
    assert client.post("/accounts/1/apply-payment",
                       json={"amount": 1.0, "payment_id": 1}, headers=auth).status_code == 200


def test_health_and_metrics_stay_open(monkeypatch):
    """Container health checks and the Prometheus scrape must not need a token.

    `docker-compose.yml` probes /health and prometheus scrapes /metrics; gating
    either would make the service report itself unhealthy and disappear from
    monitoring, which is a self-inflicted outage rather than a security control.
    """
    assert client.get("/health").status_code == 200
    assert client.get("/metrics").status_code == 200


def test_every_money_route_is_guarded():
    """Derive the list instead of trusting one, and prove the guard RUNS.

    Two failure modes, and the first version of this test only caught one.

    A hand-maintained list of protected routes is indistinguishable from a
    complete one until someone re-derives it -- that is how kyc-service stayed
    unguarded for two months while a security test appeared to cover the estate.
    So the routes come from the app itself.

    But inspecting the signature for an `x_internal_token` parameter proves only
    that the header is ACCEPTED, not that anything is done with it. A route that
    declared the parameter and never called `_require_internal` would have passed
    the earlier version of this test while being completely open -- the precise
    shape of "the check exists and protects nothing" that this PR's review found
    in the configuration. So each derived route is also called with no token and
    must actually refuse.
    """
    exempt = {
        # Read-only: ownership-checked at the gateway, no money moves.
        ("GET", "/accounts/{loan_id}/balance"),
        ("GET", "/reconciliation/peek"),
        ("GET", "/health"),
        ("GET", "/metrics"),
    }

    declared, unguarded = [], []
    for route in main.app.routes:
        methods = getattr(route, "methods", set()) or set()
        path = getattr(route, "path", "")
        mutating = bool(methods & {"POST", "PUT", "PATCH", "DELETE"})
        if not mutating or any((m, path) in exempt for m in methods):
            continue
        params = getattr(route, "dependant", None)
        names = {p.name for p in params.header_params} if params else set()
        if "x_internal_token" not in names:
            unguarded.append(f"{sorted(methods)} {path}")
        else:
            declared.append(path)

    assert not unguarded, (
        "mutating servicing routes with no X-Internal-Token check: "
        + ", ".join(unguarded)
        + ". Add _require_internal(x_internal_token) to each, or -- if a route "
          "genuinely must be open -- add it to `exempt` above with the reason, "
          "so the decision is recorded rather than implied by its absence."
    )

    # Every declared route must be represented in MONEY_ROUTES, so the behavioural
    # cases above actually cover what the derivation found. Compared on the final
    # path segment because the app exposes templates ("/accounts/{loan_id}/...")
    # while the behavioural cases use concrete ids ("/accounts/1/...").
    def _action(path):
        return path.rstrip("/").rsplit("/", 1)[-1]

    covered = {_action(p) for p, _ in MONEY_ROUTES}
    missing = [p for p in declared if _action(p) not in covered]
    assert not missing, (
        f"routes declare the header but are never called without it: {missing}. "
        "Add them to MONEY_ROUTES so the guard is proven to execute, not merely "
        "to be present in the signature."
    )


def test_the_guard_actually_executes_on_every_derived_route(no_writes):
    """Call each money route with no credential and require a real refusal.

    This is the test that would have caught a declared-but-unused parameter.
    It is separate from the signature check above because the two prove
    different things: that one proves the estate is enumerated, this one proves
    the enumeration is not decorative.
    """
    for path, body in MONEY_ROUTES:
        resp = client.post(path, json=body)
        assert resp.status_code == 401, f"{path} did not refuse an unauthenticated caller"


def test_the_comparison_is_constant_time(monkeypatch):
    """`!=` on str short-circuits at the first differing byte, so response
    timing leaks how much of a guess was correct, one byte at a time.

    Asserted structurally rather than by timing: a timing assertion on a CI
    runner is a flake generator, and what actually matters is that the comparison
    goes through `secrets.compare_digest` at all.
    """
    calls = []
    real = main.secrets.compare_digest

    def _spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(main.secrets, "compare_digest", _spy)
    client.post("/accounts/1/late-fee", headers={"X-Internal-Token": "wrong-but-present"})

    assert calls, "the token comparison did not go through secrets.compare_digest"


def test_an_empty_header_cannot_match_an_empty_configured_token(monkeypatch):
    """compare_digest("", "") is True.

    So a deployment with no token configured would admit every caller that sent
    an empty header -- the emptiness check has to come first, and does. Startup
    validation should prevent this state entirely; this is the second line of
    the same defence, and it is asserted because "unreachable" states are
    exactly the ones that turn out to be reachable.
    """
    monkeypatch.setattr(main.config, "INTERNAL_SERVICE_TOKEN", "")

    resp = client.post("/accounts/1/late-fee", headers={"X-Internal-Token": ""})

    assert resp.status_code == 401
