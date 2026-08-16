"""The gateway and servicing must mean the same thing by "may move money".

There is no shared library in this repository, so the rule is written twice:
`gateway/app/auth.py::MONEY_ROLES` decides which sessions may reach the money
routes, and `servicing-service/app/principal.py::MONEY_ROLES` decides which
verified principals may act on them. Two copies of one rule drift, and the drift
is silent in both directions:

  * widen the gateway alone and it forwards requests servicing will refuse --
    a staff member sees a 403 from a service they cannot see;
  * widen servicing alone and the defence-in-depth check stops being a check,
    because the outer gate is the only thing still refusing underwriters.

`servicing/app/principal.py`'s docstring names this test by name. That citation
is why it exists rather than being assumed: a comment that cites a test which
does not exist is the defect this repository keeps finding in its own documents.

Read statically, so it needs neither service importable nor a database.
"""
import ast
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
GATEWAY_AUTH = REPO / "services" / "gateway" / "app" / "auth.py"
SERVICING_PRINCIPAL = REPO / "services" / "servicing-service" / "app" / "principal.py"


def _literal_set(path: pathlib.Path, name: str) -> set[str]:
    """The value of a module-level assignment, evaluated as a literal."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if name not in targets:
            continue
        value = node.value
        # `frozenset({...})` and a bare tuple/set/list all read the same here.
        if isinstance(value, ast.Call) and getattr(value.func, "id", "") == "frozenset":
            value = value.args[0]
        return set(ast.literal_eval(value))
    raise AssertionError(f"{name} not found in {path.relative_to(REPO)}")


def test_the_two_services_permit_exactly_the_same_roles():
    gateway = _literal_set(GATEWAY_AUTH, "MONEY_ROLES")
    servicing = _literal_set(SERVICING_PRINCIPAL, "MONEY_ROLES")
    assert gateway == servicing, (
        f"the gateway admits {sorted(gateway)} to the money routes and servicing "
        f"admits {sorted(servicing)}. One of them is wrong, and which one is not "
        f"decidable from here -- widening servicing removes the independent "
        f"check, and widening the gateway forwards requests servicing refuses."
    )


def test_the_comparison_found_something_to_compare():
    """Guards the guard: two empty sets are equal."""
    assert _literal_set(GATEWAY_AUTH, "MONEY_ROLES"), "no roles parsed from the gateway"
    assert "csr" in _literal_set(SERVICING_PRINCIPAL, "MONEY_ROLES")


def test_underwriter_is_not_a_money_role_in_either_service():
    """The specific case the rule exists for.

    Underwriter is staff, and `is_staff()` alone once let one POST straight to
    these routes even though the servicing UI never shows them the button.
    """
    for path in (GATEWAY_AUTH, SERVICING_PRINCIPAL):
        assert "underwriter" not in _literal_set(path, "MONEY_ROLES"), (
            f"{path.relative_to(REPO)} admits underwriters to money movement"
        )


@pytest.mark.parametrize("path", [GATEWAY_AUTH, SERVICING_PRINCIPAL],
                         ids=["gateway", "servicing"])
def test_the_roles_are_a_literal_not_a_runtime_lookup(path):
    """If either becomes computed, this comparison silently stops meaning
    anything -- so it fails loudly instead, and asks for a real check."""
    _literal_set(path, "MONEY_ROLES")
