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
    ("accept ANY authenticated caller",
     "every money route requires X-Internal-Token and the gateway adds a role rule"),
    ("applies the amount twice (double-charge)",
     "a retry inserts another payment record and applies the loan balance again; "
     "it double-records and double-applies, and performs no processor charge"),
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
    main = (APP / "main.py").read_text(encoding="utf-8")

    assert "D14" in balance, "balance.py no longer records that there is no waterfall"
    assert "D8" in main, (
        "main.py no longer records that the money routes have no approver and "
        "no human principal"
    )
    # D2's servicing half is no longer a limitation to write down: the
    # processorless route and `app/payments.py` were deleted, so there is no
    # non-idempotent servicing payment path left to admit to. What replaced this
    # assertion is the absence itself --
    # `servicing-service/tests/test_legacy_payments_route_is_retired.py` fails if
    # the route, the module, or a `servicing_legacy` INSERT returns.
    assert not (APP / "payments.py").exists(), (
        "app/payments.py is back. It held the charge that recorded a payment "
        "twice on a retry; if it is needed again, D2 reopens and docs/DEBT.md, "
        "the Week 5 roadmap row and the retirement test must change together"
    )


def test_no_servicing_module_reintroduces_an_unkeyed_charge():
    """The tripwire that replaces the one reading `payments.py`.

    That check asserted the deleted module still said "NO idempotency key" --
    correct while the module existed, meaningless once it did not. The property
    worth keeping is broader and outlives the file: no servicing module may
    insert a `payments` row at all. Payment creation belongs to payment-service,
    which requires an `idempotency_key`; servicing only APPLIES an already
    captured payment, keyed by `payment_id`.

    Static, and only static -- it fails the moment someone writes the INSERT,
    which is earlier than any behavioural test could notice.
    """
    #: The one legitimate INSERT INTO payments in this service. It writes a
    #: negative-id sentinel inside a transaction that is ALWAYS rolled back, to
    #: prove the apply-payment write path is usable before payment-service
    #: authorizes a card -- a preflight that returns 200 without exercising a
    #: write is how a charge got captured against an apply that could not land.
    #: It creates no payment: nothing it writes survives the rollback.
    PREFLIGHT = "internal_auth_check"

    offenders = []
    for path in sorted(APP.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if func.name == PREFLIGHT:
                continue
            for node in ast.walk(func):
                if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                    continue
                if "INSERT INTO PAYMENTS" in " ".join(node.value.upper().split()):
                    offenders.append(f"{path.name}:{node.lineno} in {func.name}()")
    assert not offenders, (
        f"servicing-service records a payment again at {offenders}. Only "
        f"payment-service may create a payment row -- it is the path that "
        f"carries an idempotency key (docs/DEBT.md D2). If a new rolled-back "
        f"probe needs one, name it here explicitly rather than widening the rule."
    )


def test_the_debt_register_names_both_payment_paths_in_d2():
    """D2 covered two endpoints with one name, and that is what made it wrong.

    The register first said the repository was idempotent while servicing's
    processorless duplicate double-recorded on retry. It then said one path was
    fixed and one still open. Both statements were true in their moment, and a
    test pinned to either would now be wrong -- the duplicate has been retired.

    So the durable requirement is not a status word but the DISTINCTION: D2 must
    keep naming both endpoints, because the entry is unreadable without it. A
    reader who does not know there were two `POST /payments` cannot tell which
    one a claim is about, which is exactly how this row went stale twice.
    """
    row = next(l for l in DEBT.read_text(encoding="utf-8").splitlines()
               if l.startswith("| **D2**"))
    assert "payment-service" in row, (
        "D2 no longer names the canonical processor-backed path"
    )
    assert "servicing" in row.lower(), (
        "D2 no longer names the servicing duplicate, so it reads as though there "
        "was only ever one POST /payments"
    )
    assert "retired" in row.lower() or "deleted" in row.lower(), (
        "D2 claims a fix without saying what happened to the second endpoint -- "
        "if it were merely disabled or still present, the defect would still be "
        "reachable by anything holding the internal token"
    )
    # And the claim has to be checkable, not asserted.
    assert "test_legacy_payments_route_is_retired" in row, (
        "D2 does not cite the test that proves the route is gone"
    )


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
    # The entry must say what kind of evidence it has, because "Fixed" on its own
    # cannot distinguish a proof that ran from one that skipped. It said "not
    # executed during this pass" while no database was reachable; it now says the
    # cases were executed and against what. Either is acceptable; silence is not.
    lowered = row.lower()
    assert ("executed" in lowered or "skip" in lowered), (
        "D3 does not disclose whether its real-PostgreSQL proof actually ran -- a "
        "skipped test reads exactly like a passing one, so the entry has to say"
    )
    if "executed" in lowered and "postgresql" not in lowered:
        raise AssertionError(
            "D3 claims its proof was executed without naming the database it ran "
            "against; 'executed' with no version is the claim this register keeps "
            "having to retract"
        )
