"""The approved consumer-reason table is duplicated. Prove it cannot drift.

`decision-service` produces the reason and `origination-service` renders it in
operational messages, and neither imports the other — this repository has no
shared library, which is the same reason the internal-token validator is
duplicated in every service that enforces it.

Duplication is fine. Duplication nobody checks is how two services end up
disagreeing about what a declined applicant may be told, and the disagreement
would surface as one service refusing a denial while the other quietly renders
something for it. So the two tables are compared here rather than trusted.

Read as source rather than imported, because importing either app package pulls
in service dependencies this suite does not have.
"""
import ast
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
DECISION = REPO / "services" / "decision-service" / "app" / "decision.py"
ORIGINATION = REPO / "services" / "origination-service" / "app" / "decision_state.py"


def _literal_dict(path: pathlib.Path, name: str) -> dict:
    """Every key/value assigned to `name`, across an assignment and any
    `.update({...})` calls on it."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: dict = {}
    seen_name = False

    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and node.target.id == name and node.value is not None:
            seen_name = True
            found.update(ast.literal_eval(node.value))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    seen_name = True
                    found.update(ast.literal_eval(node.value))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "update" \
                and isinstance(node.func.value, ast.Name) \
                and node.func.value.id == name:
            seen_name = True
            found.update(_resolve(node.args[0], tree))

    assert seen_name, f"{name} not found in {path.name}"
    return found


def _resolve(node: ast.AST, tree: ast.Module) -> dict:
    """`literal_eval` a dict whose keys/values may be module-level constants."""
    constants = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 \
                and isinstance(stmt.targets[0], ast.Name) \
                and isinstance(stmt.value, ast.Constant):
            constants[stmt.targets[0].id] = stmt.value.value

    def value_of(expr):
        if isinstance(expr, ast.Constant):
            return expr.value
        if isinstance(expr, ast.Name):
            assert expr.id in constants, f"unresolved constant {expr.id}"
            return constants[expr.id]
        raise AssertionError(f"unsupported expression {ast.dump(expr)}")

    assert isinstance(node, ast.Dict)
    return {value_of(k): value_of(v) for k, v in zip(node.keys, node.values)}


@pytest.fixture(scope="module")
def tables():
    return (_literal_dict(DECISION, "APPROVED_CONSUMER_REASONS"),
            _literal_dict(ORIGINATION, "APPROVED_CONSUMER_REASONS"))


def test_both_services_are_populated(tables):
    producer, renderer = tables

    assert producer, "decision-service has no approved reasons at all"
    assert renderer, "origination-service has no approved reasons at all"


def test_the_two_services_agree_exactly(tables):
    producer, renderer = tables

    assert producer == renderer, (
        "the approved consumer-reason tables disagree.\n"
        f"  only in decision-service:   {sorted(set(producer) - set(renderer))}\n"
        f"  only in origination-service:{sorted(set(renderer) - set(producer))}")


def test_no_approved_reason_is_a_machine_token(tables):
    """The whole point is that a consumer reads these. A snake_case token is
    not a specific reason under 12 CFR 1002.9."""
    for table_name, table in zip(("decision-service", "origination-service"), tables):
        for code, wording in table.items():
            assert "_" not in wording, (
                f"{table_name}: {code!r} maps to a machine token {wording!r}")
            assert wording.strip() and " " in wording.strip(), (
                f"{table_name}: {code!r} maps to {wording!r}, not a sentence")


def test_the_placeholder_was_never_promoted(tables):
    """`high_debt_to_income` exists in this repository only as a test author's
    example. Promoting it would invent a vendor taxonomy entry."""
    for table in tables:
        assert "high_debt_to_income" not in table


def test_the_tables_hold_only_codes_this_repository_emits(tables):
    """VENDOR-BLOCKED: no vendor taxonomy is committed, so any entry for a code
    this repository does not itself produce came from somewhere unaccountable."""
    ours = _literal_dict(DECISION, "APPROVED_CONSUMER_REASONS")
    source = DECISION.read_text(encoding="utf-8")

    for code in ours:
        assert code in source, f"{code!r} is mapped but never emitted here"
