"""Every service compares the internal service token in constant time.

`INTERNAL_SERVICE_TOKEN` is the shared secret that proves a request came from
inside the estate. Five services compared it with `!=` or `==` while three
already used `secrets.compare_digest`, so the same secret was checked two
different ways depending on which service you landed on.

Python's string comparison short-circuits on the first differing byte, so how
long it takes leaks how much of the prefix was right. This is a **hardening**
consistency fix, not a vulnerability report:

  * none of the eight backend services publishes a host port
    (`docker-compose.yml` publishes only postgres, redis, gateway, frontend,
    prometheus and grafana), so the comparison is not reachable from outside the
    compose network;
  * remote timing measurement across a network hop, against a Python string
    compare, is not a practical attack here.

It is fixed because a security control that is written two ways is one a reader
cannot check at a glance, and because the cheap version was already the one in
use elsewhere. **It does not close SEC-17** -- the token is still symmetric, so
any service holding it can still claim any role -- and it does not make the
platform production-secure. Both remain open and are recorded as such.

This guard is static. It reads the source rather than timing anything, because
timing a constant-time comparison in CI measures the runner, not the code.
"""
import ast
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
APP_ROOTS = sorted(REPO_ROOT.glob("services/*/app"))

#: The config name every service reads the shared secret from.
TOKEN_CONFIG_NAMES = frozenset({"INTERNAL_SERVICE_TOKEN"})


def _is_token_ref(node: ast.AST) -> bool:
    """`config.INTERNAL_SERVICE_TOKEN`, a bare `INTERNAL_SERVICE_TOKEN`, or the
    inbound header variable it is checked against."""
    if isinstance(node, ast.Attribute) and node.attr in TOKEN_CONFIG_NAMES:
        return True
    if isinstance(node, ast.Name) and (
        node.id in TOKEN_CONFIG_NAMES or node.id == "x_internal_token"
    ):
        return True
    return False


def _short_circuit_comparisons(path: pathlib.Path) -> list[tuple[int, str]]:
    """`==` / `!=` comparisons where either side is the internal token."""
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        for op, right in zip(node.ops, node.comparators):
            if not isinstance(op, (ast.Eq, ast.NotEq)):
                continue
            if _is_token_ref(node.left) or _is_token_ref(right):
                symbol = "==" if isinstance(op, ast.Eq) else "!="
                found.append((node.lineno, symbol))
    return found


def test_the_scan_covers_the_services():
    assert APP_ROOTS, "no services/*/app directories found -- this guard is scanning nothing"
    names = {p.parent.name for p in APP_ROOTS}
    for expected in ("kyc-service", "decision-service", "payment-service"):
        assert expected in names, (expected, names)


def test_no_service_compares_the_internal_token_with_a_short_circuiting_operator():
    offenders: list[str] = []
    for root in APP_ROOTS:
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            for lineno, symbol in _short_circuit_comparisons(path):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: uses `{symbol}`")

    assert offenders == [], (
        "the internal service token is compared with a short-circuiting operator:\n  "
        + "\n  ".join(offenders)
        + "\n\nUse `secrets.compare_digest(...)`, guarding first that both the "
        "configured token and the inbound header are non-empty -- compare_digest "
        "raises on None. An unset configured token must still fail closed."
    )


@pytest.mark.parametrize(
    "service",
    [
        "kyc-service",
        "decision-service",
        "disclosure-service",
        "payment-service",
        "origination-service",
        "servicing-service",
        "loan-assistant",
    ],
)
def test_each_gated_service_reaches_compare_digest(service):
    """The absence of `==` is not the same as the presence of the right check.

    A service that stopped comparing the token at all would pass the scan above
    while failing open, so this asserts the positive form is actually there.
    """
    app = REPO_ROOT / "services" / service / "app"
    sources = [
        p.read_text(encoding="utf-8", errors="replace")
        for p in app.rglob("*.py")
        if "__pycache__" not in p.parts
    ]
    assert any("compare_digest" in s for s in sources), (
        f"{service} never calls secrets.compare_digest -- if it no longer checks the "
        "internal token at all, that is a bigger problem than how it compares it."
    )


def test_the_matcher_would_notice_a_short_circuit(tmp_path):
    """Mutation-in-place: the scan must fail on code that does compare with `!=`.

    Otherwise a broken matcher and clean code look identical from a green tick.
    """
    offender = tmp_path / "guard.py"
    offender.write_text(
        "def check(x_internal_token):\n"
        "    if x_internal_token != config.INTERNAL_SERVICE_TOKEN:\n"
        "        raise Exception\n",
        encoding="utf-8",
    )
    assert _short_circuit_comparisons(offender) == [(2, "!=")]

    equality = tmp_path / "equality.py"
    equality.write_text(
        "def check(x_internal_token):\n"
        "    return x_internal_token == config.INTERNAL_SERVICE_TOKEN\n",
        encoding="utf-8",
    )
    assert _short_circuit_comparisons(equality) == [(2, "==")]


def test_an_unrelated_equality_is_not_flagged(tmp_path):
    """The guard is about one secret, not about every `==` in the codebase."""
    unrelated = tmp_path / "unrelated.py"
    unrelated.write_text(
        "def check(status):\n"
        "    return status == 'captured'\n",
        encoding="utf-8",
    )
    assert _short_circuit_comparisons(unrelated) == []
