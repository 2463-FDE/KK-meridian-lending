"""Application code must not start calling the legacy direct balance writers again.

`balance.py` still contains three functions that write `balances` directly:

    apply_payment(loan_id, amount)          -- superseded by apply_payment_once
    adjust_balance(loan_id, new_value)      -- superseded by maker-checker
    waive_fee(loan_id, amount)              -- superseded by maker-checker

They are retained for the ledger cutover history (ADR 0010) and for the tests
that pin what they used to do. `models.py` states that none of them is reachable
from a route -- and until now that was a sentence in a docstring, which is
exactly the kind of claim this repository keeps finding to be stale.

**What this guards, and what it does not.** `balances` is a projection written by
`project_ledger_entry()`; the authoritative paths are `apply_payment_once` (which
allocates through the waterfall and writes ledger entries) and
`resolve_pending_movement` (which writes an entry only when a second person
approves). A direct writer bypasses the waterfall and the second approver, and
the 0035 capture trigger then papers over it with a `legacy_direct_write` entry
so parity still holds -- the money moves with no allocation and no approval, and
the ledger records that it happened rather than refusing it.

This is a **static** check on application code only. It is deliberately NOT
ADR 0010's step-5 database write guard, which is a different and much larger
change: that guard stops every writer including a psql session, needs the three
functions retired first, and is not freeze-week work. This only pins the fact
the docstring already asserts.

Two limits, stated rather than discovered later:

  * Tests may call them, and several do -- that is the point of keeping them.
    Only `services/*/app/**` is scanned.
  * A dynamic call (`getattr(balance, name)()`) would not be seen. That is a
    false negative, not a false positive, and nothing in this repository does it.
"""
import ast
import pathlib

import pytest

#: The functions in `balance.py` that write the projection directly.
LEGACY_DIRECT_WRITERS = frozenset({"apply_payment", "adjust_balance", "waive_fee"})

APP_ROOTS = sorted(pathlib.Path(__file__).resolve().parents[3].glob("services/*/app"))


def _call_sites(path: pathlib.Path) -> list[tuple[int, str]]:
    """Places this module reaches a legacy writer THROUGH the balance module.

    Matched on attribute access (`balance.waive_fee`) and on a direct import
    (`from .balance import waive_fee`). A bare `def waive_fee(...)` is not a
    call site, which matters here: `main.py`'s route handlers are named after
    the operations they expose, so the HTTP handler `def waive_fee` sits a few
    lines from a `maker_checker.propose` call and must not be mistaken for one.
    """
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in LEGACY_DIRECT_WRITERS:
            if isinstance(node.value, ast.Name) and node.value.id == "balance":
                found.append((node.lineno, f"balance.{node.attr}"))
        elif isinstance(node, ast.ImportFrom) and (node.module or "").endswith("balance"):
            # `from .balance import waive_fee` keeps its dots in `level`, not in
            # `module`, so the module name alone would render the import as an
            # absolute one it is not.
            module = "." * node.level + (node.module or "")
            for alias in node.names:
                if alias.name in LEGACY_DIRECT_WRITERS:
                    found.append((node.lineno, f"from {module} import {alias.name}"))
    return found


def test_the_app_roots_were_actually_found():
    # A scan that silently looked at nothing would pass forever.
    assert APP_ROOTS, "no services/*/app directories were found -- the guard is scanning nothing"
    names = {p.parent.name for p in APP_ROOTS}
    assert "servicing-service" in names, names


def test_no_application_module_calls_a_legacy_direct_balance_writer():
    offenders: list[str] = []
    scanned = 0
    for root in APP_ROOTS:
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            scanned += 1
            for lineno, what in _call_sites(path):
                offenders.append(f"{path}:{lineno}: {what}")

    assert scanned > 0, "no application modules were scanned"
    assert offenders == [], (
        "application code reached a legacy direct balance writer:\n  "
        + "\n  ".join(offenders)
        + "\n\nThese write `balances` directly, so they skip the payment waterfall "
        "and the second approver. Use `balance.apply_payment_once` for a captured "
        "payment, or raise a proposal through `maker_checker` for a staff-directed "
        "movement. If a direct write is genuinely required, that is an ADR 0010 "
        "decision, not a call site."
    )


@pytest.mark.parametrize("name", sorted(LEGACY_DIRECT_WRITERS))
def test_the_writers_this_guard_names_still_exist(name):
    """The guard must fail loudly if a name it protects is renamed or removed.

    Otherwise retiring `waive_fee` would leave a rule that silently protects
    nothing, and the next reader would take the passing test as evidence about a
    function that is no longer there.
    """
    balance_py = pathlib.Path(__file__).resolve().parents[1] / "app" / "balance.py"
    tree = ast.parse(balance_py.read_text(encoding="utf-8"), filename=str(balance_py))
    defined = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert name in defined, (
        f"`{name}` is no longer defined in balance.py. If it was retired, drop it from "
        "LEGACY_DIRECT_WRITERS in this file too -- a guard naming a function that does "
        "not exist protects nothing."
    )


def test_the_guard_would_notice_a_call(tmp_path):
    """Mutation-in-place: a module that DOES call one must be reported.

    Without this the suite could pass because the matcher is broken rather than
    because the code is clean -- the two are indistinguishable from a green tick.
    """
    offender = tmp_path / "regression.py"
    offender.write_text(
        "from . import balance\n"
        "def move(loan_id):\n"
        "    return balance.adjust_balance(loan_id, 0.0)\n",
        encoding="utf-8",
    )
    assert _call_sites(offender) == [(3, "balance.adjust_balance")]

    importer = tmp_path / "importer.py"
    importer.write_text("from .balance import waive_fee\n", encoding="utf-8")
    assert _call_sites(importer) == [(1, "from .balance import waive_fee")]


def test_a_route_handler_named_after_the_operation_is_not_a_call_site(tmp_path):
    """`main.py` defines `def waive_fee(...)` as an HTTP handler. Defining a
    function with the same name is not calling the legacy writer, and a guard
    that could not tell the difference would fail on the current codebase."""
    handler = tmp_path / "routes.py"
    handler.write_text(
        "def waive_fee(loan_id, body):\n"
        "    return maker_checker.propose(loan_id)\n"
        "def adjust_balance(loan_id, body):\n"
        "    return maker_checker.propose(loan_id)\n",
        encoding="utf-8",
    )
    assert _call_sites(handler) == []
