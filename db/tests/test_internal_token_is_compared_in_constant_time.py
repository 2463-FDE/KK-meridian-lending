"""Every service compares the internal service token the same safe way.

`INTERNAL_SERVICE_TOKEN` is the shared secret that proves a request came from
inside the estate. Five services compared it with `!=` or `==` while three
already used `secrets.compare_digest`, so the same secret was checked two
different ways depending on which service you landed on.

Two properties are pinned here, and the second was found by review of the first.

**1. Constant time.** Python string comparison short-circuits on the first
differing byte, so how long it takes leaks how much of the prefix was right.

**2. Compared as bytes.** `secrets.compare_digest` raises `TypeError` on a
non-ASCII `str`. HTTP headers are not ASCII by construction -- Starlette decodes
them as latin-1 -- so a non-conforming client sending raw high bytes turned a
wrong token into a **500** instead of a 401. That is an oracle: the caller can
tell a malformed guess from a merely incorrect one by the status code. Encoding
both sides removes it. Reproduced before fixing: with the `str` form,
`X-Internal-Token: b"attacker-guessed-tok\\xe9n"` raised
`TypeError: comparing strings with non-ASCII characters is not supported`
inside the handler.

**Scope, stated plainly.** None of the eight backend services publishes a host
port -- `docker-compose.yml` publishes only postgres, redis, gateway, frontend,
prometheus and grafana -- so these comparisons are not reachable from outside the
compose network. This is hardening and consistency, not a vulnerability report.
**It does not close SEC-17**: the token is still symmetric, so any service
holding it can still claim any role. It does not make anything
production-secure.

This guard is static. It reads the source rather than timing anything, because
timing a constant-time comparison in CI measures the runner, not the code. The
observable half -- that a non-ASCII token is a 401 and not a 500 -- is pinned
behaviourally in `payment-service/tests/test_charge_flow.py`.
"""
import ast
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
APP_ROOTS = sorted(REPO_ROOT.glob("services/*/app"))

TOKEN_NAME = "INTERNAL_SERVICE_TOKEN"

#: Services whose routes are gated on the shared token. Each must contain at
#: least one function that compares it, or the gate is gone rather than merely
#: written differently.
GATED_SERVICES = (
    "kyc-service",
    "decision-service",
    "disclosure-service",
    "payment-service",
    "origination-service",
    "servicing-service",
    "loan-assistant",
)


def _mentions_token(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Attribute) and child.attr == TOKEN_NAME:
            return True
        if isinstance(child, ast.Name) and child.id == TOKEN_NAME:
            return True
    return False


def _token_comparers(tree: ast.AST):
    """Functions that decide whether a presented token matches the configured one.

    Identified by mentioning the config name AND either comparing something or
    calling `compare_digest` -- so a function that merely FORWARDS the token in
    an outbound header (payment-service does this) is not mistaken for one that
    checks it.
    """
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _mentions_token(node):
            continue
        compares = any(isinstance(c, ast.Compare) for c in ast.walk(node))
        digests = any(
            isinstance(c, ast.Call)
            and isinstance(c.func, ast.Attribute)
            and c.func.attr == "compare_digest"
            for c in ast.walk(node)
        )
        if compares or digests:
            yield node


def _short_circuit_on_token(fn: ast.AST) -> list[tuple[int, str]]:
    """`==` / `!=` where one side is the configured token or the inbound header."""
    def is_token_ref(node):
        if isinstance(node, ast.Attribute) and node.attr == TOKEN_NAME:
            return True
        return isinstance(node, ast.Name) and node.id in {TOKEN_NAME, "x_internal_token"}

    found = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Compare):
            continue
        for op, right in zip(node.ops, node.comparators):
            if isinstance(op, (ast.Eq, ast.NotEq)) and (
                is_token_ref(node.left) or is_token_ref(right)
            ):
                found.append((node.lineno, "==" if isinstance(op, ast.Eq) else "!="))
    return found


def _digest_calls(fn: ast.AST) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "compare_digest"
    ]


def _is_encoded(arg: ast.AST) -> bool:
    """`x.encode("utf-8")` -- the form that keeps compare_digest off the str path."""
    return (
        isinstance(arg, ast.Call)
        and isinstance(arg.func, ast.Attribute)
        and arg.func.attr == "encode"
    )


def _service_of(path: pathlib.Path) -> str:
    return path.relative_to(REPO_ROOT).parts[1]


def _python_sources():
    for root in APP_ROOTS:
        for path in root.rglob("*.py"):
            if "__pycache__" not in path.parts:
                yield path


def test_the_scan_covers_the_services():
    assert APP_ROOTS, "no services/*/app directories found -- this guard scans nothing"
    names = {p.parent.name for p in APP_ROOTS}
    for expected in GATED_SERVICES:
        assert expected in names, (expected, sorted(names))


def test_no_token_check_uses_a_short_circuiting_comparison():
    offenders = []
    for path in _python_sources():
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
        for fn in _token_comparers(tree):
            for lineno, symbol in _short_circuit_on_token(fn):
                offenders.append(
                    f"{path.relative_to(REPO_ROOT)}:{lineno}: {fn.name}() uses `{symbol}`"
                )
    assert offenders == [], (
        "the internal service token is compared with a short-circuiting operator:\n  "
        + "\n  ".join(offenders)
        + "\n\nUse secrets.compare_digest on encoded bytes, guarding first that both "
        "the configured token and the inbound header are non-empty."
    )


def test_every_token_comparison_is_made_on_bytes():
    """Both arguments must be `.encode(...)`d.

    The str overload raises TypeError on a non-ASCII value, which turns a wrong
    token into a 500 and hands the caller a status-code oracle. Found in review
    of the constant-time change.
    """
    offenders = []
    for path in _python_sources():
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
        for fn in _token_comparers(tree):
            for call in _digest_calls(fn):
                if len(call.args) != 2 or not all(_is_encoded(a) for a in call.args):
                    offenders.append(
                        f"{path.relative_to(REPO_ROOT)}:{call.lineno}: {fn.name}() "
                        "compares without encoding both sides"
                    )
    assert offenders == [], (
        "a token comparison is made on str rather than bytes:\n  "
        + "\n  ".join(offenders)
        + "\n\nsecrets.compare_digest raises TypeError on non-ASCII str, and an HTTP "
        "header is not ASCII by construction -- Starlette decodes header bytes as "
        "latin-1. Encode both sides."
    )


@pytest.mark.parametrize("service", GATED_SERVICES)
def test_each_gated_service_actually_checks_the_token(service):
    """The absence of `==` is not the presence of a check.

    This looks for a function that COMPARES the token, not merely for the string
    `compare_digest` somewhere in the service -- origination has unrelated
    `compare_digest` calls in `decision_state.py` for access and accept token
    hashes, so a substring test would pass on those alone while `_is_staff`
    regressed. Found in review as M1.
    """
    app = REPO_ROOT / "services" / service / "app"
    comparers = []
    for path in app.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
        for fn in _token_comparers(tree):
            if _digest_calls(fn):
                comparers.append(f"{path.name}:{fn.lineno} {fn.name}()")

    assert comparers, (
        f"{service} has no function that compares {TOKEN_NAME} with compare_digest. "
        "If it stopped checking the token at all, that is a bigger problem than how "
        "it compares it."
    )


def test_the_matcher_would_notice_a_short_circuit(tmp_path):
    """Mutation-in-place: a broken matcher and clean code look identical green."""
    offender = tmp_path / "guard.py"
    offender.write_text(
        "def check(x_internal_token):\n"
        "    if x_internal_token != config.INTERNAL_SERVICE_TOKEN:\n"
        "        raise Exception\n",
        encoding="utf-8",
    )
    tree = ast.parse(offender.read_text(encoding="utf-8"))
    fns = list(_token_comparers(tree))
    assert len(fns) == 1
    assert _short_circuit_on_token(fns[0]) == [(2, "!=")]


def test_the_matcher_would_notice_an_unencoded_digest(tmp_path):
    module = tmp_path / "unencoded.py"
    module.write_text(
        "import secrets\n"
        "def check(x_internal_token):\n"
        "    return secrets.compare_digest(x_internal_token, config.INTERNAL_SERVICE_TOKEN)\n",
        encoding="utf-8",
    )
    tree = ast.parse(module.read_text(encoding="utf-8"))
    fn = next(iter(_token_comparers(tree)))
    call = _digest_calls(fn)[0]
    assert not all(_is_encoded(a) for a in call.args)


def test_a_forwarding_function_is_not_treated_as_a_check(tmp_path):
    """payment-service puts the token in an OUTBOUND header. Sending it is not
    checking it, and a guard that confused the two would demand a comparison in
    a function that has no business making one."""
    module = tmp_path / "forwarder.py"
    module.write_text(
        "def call_servicing(client):\n"
        '    return client.post("/x", headers={"X-Internal-Token": config.INTERNAL_SERVICE_TOKEN})\n',
        encoding="utf-8",
    )
    tree = ast.parse(module.read_text(encoding="utf-8"))
    assert list(_token_comparers(tree)) == []


def test_an_unrelated_equality_is_not_flagged(tmp_path):
    """The guard is about one secret, not about every `==` in the codebase."""
    module = tmp_path / "unrelated.py"
    module.write_text(
        "def check(status, x_internal_token):\n"
        "    if not secrets.compare_digest(x_internal_token.encode('utf-8'),\n"
        "                                  config.INTERNAL_SERVICE_TOKEN.encode('utf-8')):\n"
        "        raise Exception\n"
        "    return status == 'captured'\n",
        encoding="utf-8",
    )
    tree = ast.parse(module.read_text(encoding="utf-8"))
    fn = next(iter(_token_comparers(tree)))
    # The `status == 'captured'` comparison must not be read as a token compare.
    assert _short_circuit_on_token(fn) == []
