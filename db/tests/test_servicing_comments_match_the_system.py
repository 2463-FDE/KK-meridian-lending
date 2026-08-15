"""The servicing source comments must describe the servicing that exists.

Every claim pinned here was, until this file existed, stated in a comment and
false in the code sitting under it:

  * `balance.py` and `delinquency.py` said the money columns were still
    `DOUBLE PRECISION` and that migrating them was future work. D12 migrated them
    to `NUMERIC(14,2)`; the comments outlived the migration.
  * `balance.py` and `main.py` said D3 -- the unlocked read-modify-write that
    loses a concurrent payment -- was still open. `main.py` said it in the
    comment immediately above the call to `apply_payment_once`, which is the
    function that closed it.
  * `balance.py::adjust_balance` said "no ledger entry; the prior value is gone
    forever". Migration 0035's capture bridge mirrors that write into
    `ledger_entries`, so the prior value is recoverable.
  * `main.py` said the money routes were open to "ANY authenticated user", after
    the gateway had begun restricting them to csr/admin.

This repository has now been bitten by a stale comment three separate times --
the four `logging_config.py` docstrings (D5c), the reconciliation "control" that
never ran, and these. The lesson recorded under D5c generalises and is the reason
this file is a test rather than a careful edit: *a comment that overstates a
defect produces false findings as reliably as one that understates it.* Both of
this pass's remaining audit corrections came from a reader trusting one of the
sentences below.

Deliberately a regression pin on the exact retired sentences, not a ban on the
words in them. The replacements quote the old wording to explain what changed --
a keyword ban would fail on the correction itself, which is how a truth test
teaches people to delete the history instead of keeping it.

No PostgreSQL needed: this reads files.
"""
import ast
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
APP = REPO / "services" / "servicing-service" / "app"
SCHEMA = REPO / "db" / "init" / "001_schema.sql"
DEBT = REPO / "docs" / "DEBT.md"

#: The retired sentences, with what made each one false.
RETIRED = [
    ("travel to/from the DOUBLE PRECISION columns",
     "balances.balance and balances.past_due are NUMERIC(14,2) (D12)"),
    ("travels to/from the DOUBLE PRECISION column",
     "balances.past_due is NUMERIC(14,2) (D12)"),
    ("Read-modify-write with no lock (D3)",
     "the ledger projection composes signed deltas; D3 is closed"),
    ("races with apply_payment (D3)",
     "waive_fee and apply_payment touch different columns and neither loses an update"),
    ("still does the unlocked read-modify-write",
     "the route calls apply_payment_once, which writes a ledger entry"),
    ("No ledger entry; the prior value is gone forever",
     "0035's capture_legacy_balance_delta mirrors the write into ledger_entries"),
    ("ANY authenticated user. No role check",
     "the gateway restricts these routes to csr/admin (auth.can_move_money)"),
    ("ANY authenticated user can waive a fee",
     "the gateway restricts these routes to csr/admin (auth.can_move_money)"),
]


def _sources():
    return sorted(p for p in APP.glob("*.py") if p.name != "__init__.py")


@pytest.mark.parametrize("sentence,why_false", RETIRED, ids=[s[:40] for s, _ in RETIRED])
def test_a_retired_claim_has_not_come_back(sentence, why_false):
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        assert sentence not in text, (
            f"{path.relative_to(REPO)} states {sentence!r} again. It is false: "
            f"{why_false}."
        )


def test_the_money_columns_really_are_numeric():
    """The anchor for the replacement claim. If someone reverts the column types,
    the corrected comments become the stale ones and this fails first."""
    sql = SCHEMA.read_text(encoding="utf-8")
    block = sql[sql.index("CREATE TABLE IF NOT EXISTS balances"):]
    block = block[:block.index(");")]
    for column in ("balance", "past_due"):
        assert re.search(rf"^\s*{column}\s+NUMERIC\(14,2\)", block, re.M), (
            f"balances.{column} is no longer NUMERIC(14,2), so the servicing "
            f"comments describing exact money arithmetic are now overclaiming"
        )


def test_the_open_limitations_are_still_written_down():
    """The other failure direction, and the one a truth pass invites.

    Correcting a comment that overstates a defect must not quietly delete the
    part that was true. These three are open, and the source must keep saying so.
    """
    balance = (APP / "balance.py").read_text(encoding="utf-8")
    payments = (APP / "payments.py").read_text(encoding="utf-8")
    main = (APP / "main.py").read_text(encoding="utf-8")

    assert "D14" in balance, "balance.py no longer records that there is no waterfall"
    assert "D8" in main, (
        "main.py no longer records that the money routes have no approver and "
        "no human principal"
    )
    assert "D2" in payments, (
        "payments.py no longer records that this legacy route has no idempotency "
        "key -- the half of D2 that is still open"
    )


def test_the_legacy_payment_route_still_admits_it_is_not_idempotent():
    """D2's open half, pinned to the code rather than to the comment.

    `charge()` may not acquire an idempotency key without this test being
    updated: if it becomes idempotent, D2 closes and every document describing it
    as half-open needs rewriting. Either way the register and the code move
    together.
    """
    source = (APP / "payments.py").read_text(encoding="utf-8")
    assert "NO idempotency key" in source, (
        "payments.py no longer states that a retried POST double-applies"
    )
    # The module docstring names `idempotency_key` when describing the path that
    # IS fixed, so the check has to look at the code rather than the file.
    tree = ast.parse(source)
    body = tree.body[1:] if ast.get_docstring(tree) else tree.body
    code = "\n".join(ast.unparse(node) for node in body)
    assert "idempotency_key" not in code, (
        "servicing's legacy charge() now handles an idempotency key -- D2's open "
        "half may be closed; update docs/DEBT.md D2, the roadmap row and this "
        "test together"
    )


def test_the_debt_register_does_not_claim_d2_is_fixed_everywhere():
    row = next(l for l in DEBT.read_text(encoding="utf-8").splitlines()
               if l.startswith("| **D2**"))
    assert "servicing" in row.lower(), (
        "D2 does not mention the servicing-service duplicate, so it reads as "
        "though the whole repository is idempotent"
    )
    assert "payment-service" in row, "D2 does not name the path that IS fixed"
    for open_state in ("still open", "not idempotent", "open and bounded"):
        if open_state in row.lower():
            break
    else:
        pytest.fail("D2 does not say that one of its two paths is still open")


def test_the_debt_register_does_not_still_call_d3_open():
    row = next(l for l in DEBT.read_text(encoding="utf-8").splitlines()
               if l.startswith("| **D3**"))
    assert "**Open**" not in row, (
        "D3 is recorded as open while the ledger projection that closed it is on "
        "`main` -- the register and the schema disagree"
    )
    assert "test_balance_lost_update_real_postgres" in row, (
        "D3 claims a fix without naming the test that proves it"
    )
    assert "skip" in row.lower() or "not** executed" in row or "not executed" in row, (
        "D3 does not disclose that its proof needs a real PostgreSQL and skips "
        "without one -- a skipped test reads exactly like a passing one"
    )
