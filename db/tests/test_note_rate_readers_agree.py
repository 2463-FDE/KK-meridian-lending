"""Both readers of the note rate must apply the same evidence rule.

Two services answer "what rate is this loan billed at": servicing, for staff and
for the loan detail page, and the gateway, for a borrower's own list. They read
the same rows and must not disagree — a borrower seeing 7.99% on one screen and
"not recorded" on another is the D19 confusion wearing a new costume.

The rule during the expand phase has three branches, and each is a different
answer to a different row shape:

  1. `note_rate_pct` present  -> that value, proven
  2. NULL but `schedule_version` present -> `apr`, proven (a row an older image
     boarded after 0038 ran)
  3. neither -> unknown, and the UI says "not recorded"

Branch 2 is the one that will be deleted at the contract step, in both places.
Read statically: importing two services into one process is what broke a
fixture in this suite before, and the property here is about the code both
services contain rather than about a running request.
"""
import ast
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
SERVICING = REPO / "services" / "servicing-service" / "app" / "routers" / "loans.py"
GATEWAY = REPO / "services" / "gateway" / "app" / "main.py"


def _servicing_rule() -> str:
    tree = ast.parse(SERVICING.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_proven_note_rate")
    return ast.unparse(fn)


def _gateway_rule() -> str:
    text = GATEWAY.read_text(encoding="utf-8")
    start = text.index('"note_rate_pct": (')
    return text[start:start + 700]


def test_both_readers_prefer_the_column_that_says_what_it_holds():
    servicing, gateway = _servicing_rule(), _gateway_rule()
    for name, rule in (("servicing", servicing), ("gateway", gateway)):
        assert "note_rate_pct" in rule, (
            f"{name} does not consult loans.note_rate_pct, so it is still "
            f"inferring a regulated figure that is now recorded explicitly"
        )


def test_both_readers_keep_the_rolling_deploy_fallback():
    """Removing it early is the subtle failure: nothing errors, and loans boarded
    by an older image during the deploy silently report 'not recorded'."""
    servicing, gateway = _servicing_rule(), _gateway_rule()
    for name, rule in (("servicing", servicing), ("gateway", gateway)):
        assert "schedule_version" in rule, (
            f"{name} dropped the schedule_version fallback. A loan boarded by an "
            f"instance still on the previous image has a proven rate and an empty "
            f"note_rate_pct, and would now display as unrecorded"
        )
        assert "apr" in rule, f"{name} no longer falls back to the legacy column"


def test_neither_reader_reports_an_unproven_rate():
    """The D19 defect itself: a legacy row may hold the DISCLOSED APR, and
    printing it under a contractual label states a term the borrower never
    agreed to."""
    servicing = _servicing_rule()
    assert "return (None, False)" in servicing or "return None, False" in servicing, (
        "servicing no longer has an unknown branch -- every loan now reports "
        "some number, including the ones whose figure cannot be proven"
    )
    gateway = _gateway_rule()
    assert "else None" in gateway, (
        "the gateway no longer has an unknown branch"
    )


def test_the_contract_step_has_exactly_two_places_to_change():
    """A map for the next PR, held to the code so it cannot go stale.

    The contract step deletes the fallback. If a third reader appears without
    being listed here, this fails -- which is the point: the last time a
    hand-written list of readers was trusted in this repository it was missing
    one, twice.
    """
    readers = []
    for path in (REPO / "services").rglob("*.py"):
        if "__pycache__" in str(path) or "/tests/" in path.as_posix():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "schedule_version" in text and "note_rate_pct" in text:
            readers.append(path.relative_to(REPO).as_posix())

    expected = {
        "services/gateway/app/main.py",
        "services/servicing-service/app/routers/loans.py",
    }
    unexpected = set(readers) - expected - {
        # Schema/model files name both columns without applying the rule.
        "services/servicing-service/app/models.py",
        "services/servicing-service/app/schemas.py",
        "services/origination-service/app/intake.py",
        "services/origination-service/app/routers/applications.py",
        "services/disclosure-service/app/routers/offers.py",
        "services/disclosure-service/app/models.py",
        # origination's Offer model -- offers.note_rate_pct has existed since
        # 0030 and is a different column from the one 0038 adds to loans.
        "services/origination-service/app/models.py",
    }
    assert not unexpected, (
        f"a new note-rate reader appeared in {sorted(unexpected)}. The contract "
        f"step must remove the fallback there too, or that loan's rate will read "
        f"as unrecorded once loans.apr is dropped."
    )
