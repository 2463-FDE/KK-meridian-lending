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
]
# `/payments` was in this list until the processorless duplicate was retired
# (docs/DEBT.md D2). It is not listed as an unguarded exception -- the route does
# not exist, and `test_every_money_route_is_guarded` below derives its coverage
# from the running app, so it would fail if the route ever came back unguarded.
# Its absence is asserted directly in test_legacy_payments_route_is_retired.py.


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


def test_mismatched_payment_replay_returns_conflict(monkeypatch):
    def conflict(*_args):
        raise main.balance.PaymentReplayConflict("payment replay does not match")

    monkeypatch.setattr(main.balance, "apply_payment_once", conflict)
    response = client.post(
        "/accounts/1/apply-payment",
        json={"amount": 31.0, "payment_id": 7},
        headers={"X-Internal-Token": TOKEN},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "payment replay does not match"


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


# --- review round 5: a 200 must mean "I can persist", not "tokens match" ------

def test_auth_check_reports_unavailable_when_the_database_is_down(monkeypatch):
    """The preflight must prove the path apply-payment uses, not the credential.

    Review round 5: this endpoint authenticated and returned, touching no
    database -- and that was documented as a feature. It was the defect.
    payment-service reads a 200 as permission to capture a card, so a servicing
    process that is up with its database down answered 200, the card was
    charged, and the follow-up apply-payment failed. A real charge with no credit
    on the loan, which is exactly what the preflight exists to prevent.
    """
    class _DeadDb:
        def query(self, sql, params=None):
            raise RuntimeError("could not connect to server")

    monkeypatch.setattr(main, "db", _DeadDb())

    resp = client.get("/internal/auth-check", headers={"X-Internal-Token": TOKEN})

    assert resp.status_code == 503
    assert "persist" in resp.json()["detail"].lower()


def test_auth_check_uses_the_same_connection_helper_as_the_real_apply(monkeypatch):
    """The probe must run where the real write runs.

    This test previously asserted the check SELECTed from `balances` and
    `payment_applications` by name. That was the right instinct -- prove the
    dependency, not liveness -- applied to the wrong operation: reads on those
    tables succeed on a read-only replica while the apply's INSERT and UPDATE
    fail. Superseded by the write probe; what survives is the part that still
    matters, which is that the probe goes through `db.transaction()`, the same
    helper `apply_payment_once` uses, and therefore the same role and transaction
    semantics. A preflight on a different connection proves nothing about the one
    that carries the money.
    """
    used = {"transaction": False}
    from contextlib import contextmanager

    class _Db:
        def query(self, sql, params=None):
            return []

        @contextmanager
        def transaction(self):
            used["transaction"] = True
            class _Cur:
                def execute(self, sql, params=None):
                    pass
                def fetchall(self):
                    return [{"id": 1}]
            yield _Cur()

    monkeypatch.setattr(main, "db", _Db())

    assert client.get("/internal/auth-check", headers={"X-Internal-Token": TOKEN}).status_code == 200
    assert used["transaction"], (
        "the preflight did not use db.transaction(), so it did not exercise the "
        "connection the real apply-payment writes through"
    )


def test_auth_check_still_refuses_an_unauthenticated_caller(monkeypatch):
    """Authentication runs first: a caller with no token learns nothing about
    the database, and a database failure is not an authentication bypass."""
    class _DeadDb:
        def query(self, sql, params=None):                          # pragma: no cover
            raise AssertionError("the database was touched before authentication")

    monkeypatch.setattr(main, "db", _DeadDb())

    assert client.get("/internal/auth-check").status_code == 401


def test_auth_check_refuses_when_reads_pass_but_writes_fail(monkeypatch):
    """The case two SELECTs could not see.

    Review round 6: a read-only replica, a revoked INSERT grant, a read-only
    transaction or a full disk all let reads succeed while
    `apply_payment_once`'s INSERT and UPDATE fail. The preflight returned 200,
    payment-service captured the card, and the apply failed -- a real charge with
    no credit, reached through the check built to prevent it.

    Reads prove reachability. Only a write proves what this endpoint claims.
    """
    from contextlib import contextmanager

    class _ReadOnlyDb:
        def query(self, sql, params=None):
            return []                       # reads are fine

        @contextmanager
        def transaction(self):
            class _Cur:
                def execute(self, sql, params=None):
                    if sql.strip().upper().startswith("INSERT"):
                        raise RuntimeError(
                            "cannot execute INSERT in a read-only transaction")
                def fetchall(self):
                    return []
            yield _Cur()

    monkeypatch.setattr(main, "db", _ReadOnlyDb())

    resp = client.get("/internal/auth-check", headers={"X-Internal-Token": TOKEN})

    assert resp.status_code == 503, (
        "reads succeeded and the write failed, and the preflight still greenlit a "
        "card capture that could not have been credited"
    )


def test_auth_check_write_is_rolled_back(monkeypatch):
    """The probe must leave nothing behind.

    It runs before every card authorization, so a committed row per charge would
    be both a data-growth problem and a lie about what the table means.
    """
    from contextlib import contextmanager

    statements = []

    class _RecordingDb:
        def query(self, sql, params=None):
            return []

        @contextmanager
        def transaction(self):
            class _Cur:
                def execute(self, sql, params=None):
                    statements.append(" ".join(sql.split()).upper())
                def fetchall(self):
                    return [{"id": 1}]
            yield _Cur()

    monkeypatch.setattr(main, "db", _RecordingDb())

    assert client.get("/internal/auth-check", headers={"X-Internal-Token": TOKEN}).status_code == 200
    joined = " ".join(statements)
    assert "INSERT" in joined, "the preflight performed no write"
    assert "ROLLBACK" in joined, "the preflight's write was not rolled back"


def test_auth_check_refuses_when_a_deferred_ledger_invariant_fails(monkeypatch):
    """Deferred constraints must be forced before the throwaway rollback."""
    from contextlib import contextmanager

    class _DeferredFailureDb:
        @contextmanager
        def transaction(self):
            class _Cur:
                last_sql = ""

                def execute(self, sql, params=None):
                    self.last_sql = " ".join(sql.split())
                    if self.last_sql == "SET CONSTRAINTS ALL IMMEDIATE":
                        raise RuntimeError("deferred ledger parity violation")

                def fetchall(self):
                    if self.last_sql.startswith("SELECT 1 FROM balances"):
                        return [{"exists": 1}]
                    if self.last_sql.startswith("SELECT loan_id FROM balances"):
                        return [{"loan_id": 1}]
                    return [{"payment_id": -1}]

            yield _Cur()

    monkeypatch.setattr(main, "db", _DeferredFailureDb())
    response = client.get(
        "/internal/auth-check?loan_id=1",
        headers={"X-Internal-Token": TOKEN},
    )
    assert response.status_code == 503
    assert "deferred ledger parity violation" not in response.text


# --- review round 7: the probe must write to the tables the money path writes -


def _preflight_calls(monkeypatch):
    """Run the preflight against a recording cursor; return (sql, params) pairs."""
    from contextlib import contextmanager

    calls = []

    class _RecordingDb:
        def query(self, sql, params=None):
            return []

        @contextmanager
        def transaction(self):
            class _Cur:
                last_sql = ""
                def execute(self, sql, params=None):
                    self.last_sql = sql
                    calls.append((" ".join(sql.split()), params))
                def fetchall(self):
                    if self.last_sql.strip().startswith("SELECT loan_id"):
                        return [{"loan_id": 1}]
                    return [{"payment_id": -1}]
            yield _Cur()

    monkeypatch.setattr(main, "db", _RecordingDb())
    resp = client.get("/internal/auth-check", headers={"X-Internal-Token": TOKEN})
    assert resp.status_code == 200
    return calls


def _preflight_statements(monkeypatch):
    return [sql for sql, _ in _preflight_calls(monkeypatch)]


def test_the_preflight_writes_to_every_table_the_real_apply_writes(monkeypatch):
    """Derived from `apply_payment_once`'s source, not from a list I maintain.

    Review round 7. The probe wrote to `preflight_writes`, a table that existed
    only to be written to, and called that proof the apply-payment path was
    writable. It was not: per-table grant drift, a constraint or trigger failure,
    or bloat on `payment_applications` or `balances` specifically all leave a
    dedicated probe table perfectly healthy. The preflight answered 200, the card
    was captured, and the apply still failed -- the exact charge-without-credit
    this endpoint exists to prevent, reached through the endpoint built to
    prevent it.

    This is the same shape as three earlier defects on this branch: a
    hand-maintained list of protected things reads as complete while missing one.
    So the expectation is derived. If a future write path touches a third table,
    this fails instead of the probe silently going stale.
    """
    import inspect
    import re

    from app import balance

    src = inspect.getsource(balance.apply_payment_once)
    written = set(re.findall(r"INSERT INTO (\w+)", src)) | set(
        re.findall(r"UPDATE (\w+) SET", src))

    assert written, "could not read the write path out of apply_payment_once"
    assert written <= set(main._PREFLIGHT_WRITE_TABLES), (
        f"apply_payment_once writes {sorted(written)} but the preflight declares "
        f"{sorted(main._PREFLIGHT_WRITE_TABLES)} -- a table the money path writes "
        f"is not being proved before a card is captured"
    )

    joined = " ".join(_preflight_statements(monkeypatch))
    for table in main._PREFLIGHT_WRITE_TABLES:
        assert re.search(rf"(INSERT INTO|UPDATE) {table}\b", joined), (
            f"the preflight never writes to {table}, which apply_payment_once does"
        )


def test_the_preflight_cannot_collide_with_a_real_payment(monkeypatch):
    """The sentinel must be unreachable by a real row.

    `payments.id` is a positive SERIAL, so a negative payment_id can never name a
    real payment. Two consecutive probes must also differ, or concurrent
    preflights would serialise behind one primary key while a card waits.

    Read from the BOUND PARAMETERS, not the SQL text -- the sentinel is passed as
    a parameter rather than formatted into the statement, which is where it
    belongs.
    """
    seen = []
    for _ in range(6):
        inserts = [params for sql, params in _preflight_calls(monkeypatch)
                   if sql.startswith("INSERT INTO payment_applications")]
        assert len(inserts) == 1, f"expected one probe insert, got {inserts}"
        payment_id, loan_id, amount = inserts[0]
        assert payment_id < 0, "the sentinel payment_id could name a real payment"
        assert loan_id > 0, "the probe must use a real loan for payment provenance"
        assert amount == 0.01, "the probe must exercise allocation equality"
        seen.append(payment_id)

    assert len(set(seen)) > 1, (
        "every probe used the same sentinel, so concurrent preflights would block "
        "on the primary key while a cardholder waits"
    )


@pytest.mark.parametrize("broken", ["payments", "payment_applications", "ledger_entries", "balances"])
def test_the_preflight_refuses_when_either_money_table_fails(monkeypatch, broken):
    """Charles's case: the probe table is writable and a real one is not.

    Parametrised over both tables rather than written once, because the failure
    being guarded against is table-SPECIFIC -- that is the whole reason a
    dedicated probe table was the wrong answer.
    """
    from contextlib import contextmanager

    class _PartiallyBrokenDb:
        def query(self, sql, params=None):
            return []

        @contextmanager
        def transaction(self):
            class _Cur:
                last_sql = ""
                def execute(self, sql, params=None):
                    self.last_sql = sql
                    if broken in sql:
                        raise RuntimeError(
                            f'permission denied for table {broken}')
                def fetchall(self):
                    if self.last_sql.strip().startswith("SELECT loan_id"):
                        return [{"loan_id": 1}]
                    return [{"payment_id": -1}]
            yield _Cur()

    monkeypatch.setattr(main, "db", _PartiallyBrokenDb())

    resp = client.get("/internal/auth-check", headers={"X-Internal-Token": TOKEN})

    assert resp.status_code == 503, (
        f"{broken} was unwritable and the preflight still greenlit a capture the "
        f"apply could not have credited"
    )


def test_the_preflight_never_waits_on_a_live_apply(monkeypatch):
    """It runs before every authorization, so it must not be able to block.

    The balances probe locks a real row to exercise row-level triggers. Without
    SKIP LOCKED that lock queues behind an apply-payment in flight on the same
    loan, and the preflight -- and the cardholder -- wait on it.
    """
    stmts = " ".join(_preflight_statements(monkeypatch)).upper()
    assert "FOR UPDATE" in stmts, "the balances probe does not touch a real row"
    assert "SKIP LOCKED" in stmts, (
        "the preflight takes a row lock without SKIP LOCKED, so a live "
        "apply-payment on that loan makes every card authorization wait"
    )


# --- review round 8: the same COLUMNS, not merely the same table -------------


def test_the_ledger_probe_exercises_the_projection_path(monkeypatch):
    """Derived from apply_payment_once's UPDATE, not from a list I maintain.

    Round 7 moved the probe onto the real tables and stopped there: the balances
    write was `SET updated_at = updated_at`, while the money path writes
    `balance`. A column-level grant, a trigger attached to `balance`, or a
    constraint on it fails for the apply and not for the probe -- so the card is
    captured against a credit that cannot land. Probing the same TABLE is not
    probing the same WRITE; the column list is part of the statement.

    Same shape of defect as rounds 5, 6 and 7, one level finer each time, which
    is why the expectation is derived rather than written down.
    """
    import inspect
    import re

    from app import balance

    probe = next(s for s in _preflight_statements(monkeypatch)
                 if s.startswith("INSERT INTO ledger_entries"))
    assert "component, amount, entry_type, payment_id" in probe
    assert "'principal', -0.01, 'payment'" in probe


def test_the_probe_targets_the_loan_being_charged(monkeypatch):
    """A probe of some other loan's row does not prove this loan's row is writable."""
    from contextlib import contextmanager

    calls = []

    class _Db:
        def query(self, sql, params=None):
            return []

        @contextmanager
        def transaction(self):
            class _Cur:
                def execute(self, sql, params=None):
                    calls.append((" ".join(sql.split()), params))
                # The loan exists here -- the missing-row case has its own test
                # below, and this one is about which loan the probe targets.
                def fetchall(self):
                    return [{"x": 1}]
            yield _Cur()

    monkeypatch.setattr(main, "db", _Db())

    resp = client.get("/internal/auth-check?loan_id=4242",
                      headers={"X-Internal-Token": TOKEN})
    assert resp.status_code == 200

    upd = next(p for s, p in calls if s.startswith("INSERT INTO ledger_entries"))
    assert 4242 in upd, (
        f"the balances probe ignored the loan it was given ({upd}), so it can "
        f"pass on another loan's row while this one is missing or unwritable"
    )


def test_the_probe_still_works_with_no_loan_named(monkeypatch):
    """Backwards compatible: an older payment-service sends no loan_id.

    It must degrade to probing some row rather than probing nothing -- a
    preflight that silently stopped writing would be the failure this endpoint
    exists to prevent, reintroduced by a deploy ordering.
    """
    stmts = _preflight_statements(monkeypatch)
    assert any(s.startswith("INSERT INTO ledger_entries") for s in stmts)
    assert any(s.startswith("INSERT INTO payment_applications") for s in stmts)


def test_the_preflight_refuses_a_loan_with_no_balance_row(monkeypatch):
    """A named loan with no balances row must not greenlight a capture.

    Found by running the fix from the round-8 review against the live stack: the
    probe answered 200 for a loan_id that does not exist, because the targeted
    UPDATE simply matched zero rows. The comment above it claimed the probe
    proved "the row this payment will credit is writable" -- it did not, and a
    comment that overstates the code is the same defect as a test that proves a
    proxy.

    It matters because apply_payment_once UPDATEs `WHERE loan_id = %s`. With no
    row the update matches nothing, raises nothing, and the payment is recorded
    as applied while the borrower is credited nothing -- charged money, no credit,
    and no error anywhere to notice it by.
    """
    from contextlib import contextmanager

    class _NoSuchLoanDb:
        def query(self, sql, params=None):
            return []

        @contextmanager
        def transaction(self):
            class _Cur:
                def execute(self, sql, params=None):
                    self.last = sql
                def fetchall(self):
                    # The existence read finds nothing; anything else succeeds.
                    return [] if "SELECT 1 FROM balances" in self.last else [{"x": 1}]
            yield _Cur()

    monkeypatch.setattr(main, "db", _NoSuchLoanDb())

    resp = client.get("/internal/auth-check?loan_id=99999999",
                      headers={"X-Internal-Token": TOKEN})

    assert resp.status_code == 503, (
        "the preflight approved a capture for a loan with no balance row, which "
        "apply-payment would silently fail to credit"
    )
