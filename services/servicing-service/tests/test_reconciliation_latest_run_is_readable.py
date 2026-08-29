"""The last run's own evidence, readable, and gated like the money it describes.

`GET /reconciliation/peek` answers "do the two totals agree, and has anything
checked". It cannot answer the question an operator actually asks next -- what
did the run FIND -- because everything that would answer it was written to
`reconciliation_runs` and never exposed: the window covered, the file read, how
fine the comparison was, the threshold it was judged against, and the
transaction-level breaks.

So `/reconciliation/latest` reads that row back. Three properties are worth
testing and are tested here.

**It is gated on a verified staff principal, not the internal token alone.**
`peek` returns two aggregates and is token-gated. This returns loan ids,
processor references and the two amounts that disagree, which is the same class
of data as the review queue and gets the same treatment. A caller holding the
shared service token with no human behind it is refused.

**It recomputes nothing.** Every figure is the one the run recorded. A read path
that recomputed could disagree with the run it claims to display, and would make
opening a page do the control's work -- a control that runs when somebody opens
a tab is not a scheduled control.

**Never-run is a distinct answer from no-breaks.** A system that has never
reconciled must not render as a clean result, which is the same defect D7
corrected in `peek`.

Arithmetic-free and database-free: this drives the route against a fake, because
what is under test is the read path and its guard, not the comparison. The
comparison has its own real-Postgres proofs elsewhere.
"""
from tests.test_maker_checker_api import (  # noqa: F401  -- used by fixture name
    TOKEN, _client, _headers, fake_db, keys, no_money,
)

from app import reconciliation

#: One run, as `compare` would have written it -- a breach with two
#: transaction-level breaks. Values are strings where the column is NUMERIC,
#: because that is what the database hands back and what the route must not
#: quietly turn into floats.
RUN = {
    "id": 7,
    "started_at": "2026-08-28 03:00:00+00",
    "finished_at": "2026-08-28 03:00:04+00",
    "outcome": "breach",
    "loans_compared": 184,
    "references_compared": 306,
    "unreferenced_captures": 1,
    "out_of_scope_captures": 2,
    "breaks_found": 2,
    "break_value": "349.99",
    "threshold_value": "0.00",
    "window_start": "2026-08-01",
    "window_end": "2026-08-28",
    "source": {"file": "settlement-2026-08-28.csv"},
    "error_code": None,
    "breaks": [
        {"kind": "settlement_only", "loan_id": 4471, "processor_ref": "PR-100231",
         "ledger": "0.00", "settlement": "250.00", "difference": "-250.00"},
        {"kind": "amount_mismatch", "loan_id": 4472, "processor_ref": "PR-100244",
         "ledger": "0.01", "settlement": "100.00", "difference": "-99.99"},
    ],
}


def _get(keys, role="underwriter"):  # noqa: F811
    return _client().get("/reconciliation/latest", headers=_headers(keys, role=role))


def test_the_run_is_returned_as_recorded(keys, fake_db, monkeypatch):  # noqa: F811
    monkeypatch.setattr(reconciliation, "latest_run", lambda: dict(RUN))

    body = _get(keys).json()["run"]

    # Every field the operator needs to interpret the result, and each one the
    # value the run stored rather than a recomputation.
    assert body["id"] == 7
    assert body["outcome"] == "breach"
    assert body["window_start"] == "2026-08-01"
    assert body["window_end"] == "2026-08-28"
    assert body["source"] == {"file": "settlement-2026-08-28.csv"}
    assert body["loans_compared"] == 184
    assert body["references_compared"] == 306
    assert body["unreferenced_captures"] == 1
    assert body["out_of_scope_captures"] == 2
    assert body["breaks_found"] == 2
    assert body["break_value"] == "349.99"
    assert body["threshold_value"] == "0.00"
    assert body["error_code"] is None


def test_the_transaction_breaks_carry_both_sides_and_the_difference(keys, fake_db, monkeypatch):  # noqa: F811
    """A break is only investigable if it says which loan, which reference, and
    what the two sides were. A count alone sends somebody to the database."""
    monkeypatch.setattr(reconciliation, "latest_run", lambda: dict(RUN))

    breaks = _get(keys).json()["run"]["breaks"]

    assert [b["loan_id"] for b in breaks] == [4471, 4472]
    assert [b["processor_ref"] for b in breaks] == ["PR-100231", "PR-100244"]
    assert [b["kind"] for b in breaks] == ["settlement_only", "amount_mismatch"]
    assert breaks[1]["ledger"] == "0.01"
    assert breaks[1]["settlement"] == "100.00"
    assert breaks[1]["difference"] == "-99.99"


def test_amounts_stay_strings(keys, fake_db, monkeypatch):  # noqa: F811
    """Money crosses this boundary as text, as it does everywhere else in this
    service. A float here would be a second representation of a figure the
    ledger already stores exactly."""
    monkeypatch.setattr(reconciliation, "latest_run", lambda: dict(RUN))

    run = _get(keys).json()["run"]

    assert isinstance(run["break_value"], str)
    assert isinstance(run["threshold_value"], str)
    assert all(isinstance(b["difference"], str) for b in run["breaks"])


def test_never_having_run_is_not_a_clean_result(keys, fake_db, monkeypatch):  # noqa: F811
    """D7's lesson, applied to this route.

    An empty break list under a "Reconciliation" heading reads as "nothing is
    wrong". A database where the job has never run has to say so instead, or the
    screen reports agreement nobody established.
    """
    monkeypatch.setattr(reconciliation, "latest_run", lambda: None)

    body = _get(keys).json()

    assert body["run"] is None
    assert "never run" in body["note"].lower()
    assert "agree" in body["note"].lower(), (
        "the note must deny the agreement reading explicitly, not merely omit it"
    )


def test_the_payload_says_a_break_is_not_a_duplicate(keys, fake_db, monkeypatch):  # noqa: F811
    """The candidate/break distinction travels in the response, not only in the
    UI. A client that renders this under a heading of its own choosing still
    carries the sentence saying what a break is and is not."""
    monkeypatch.setattr(reconciliation, "latest_run", lambda: dict(RUN))

    note = _get(keys).json()["note"].lower()

    assert "not a duplicate" in note
    assert "not proof that money was lost" in note
    assert "requiring investigation" in note


def test_the_internal_token_alone_is_not_enough(keys, fake_db, monkeypatch):  # noqa: F811
    """The guard this route exists to carry.

    `peek` is token-gated because it returns two aggregates. This returns loan
    ids, processor references and amounts, so it needs the human as well --
    the same bar the review queue sets.
    """
    monkeypatch.setattr(reconciliation, "latest_run", lambda: dict(RUN))

    response = _client().get(
        "/reconciliation/latest", headers={"X-Internal-Token": TOKEN},
    )

    assert response.status_code in (401, 403), response.text


def test_a_missing_internal_token_is_refused(keys, fake_db, monkeypatch):  # noqa: F811
    monkeypatch.setattr(reconciliation, "latest_run", lambda: dict(RUN))

    headers = dict(_headers(keys))
    headers.pop("X-Internal-Token", None)
    response = _client().get("/reconciliation/latest", headers=headers)

    assert response.status_code in (401, 403), response.text


def test_reading_it_starts_no_run(keys, fake_db, monkeypatch):  # noqa: F811
    """The route reads; it must not reconcile.

    A control that runs because somebody opened a tab is not a scheduled
    control, and the evidence on screen would depend on who was looking. The
    scheduler owns when reconciliation happens.
    """
    monkeypatch.setattr(reconciliation, "latest_run", lambda: dict(RUN))
    called = {"compare": 0, "run_once": 0}
    for name in ("compare", "run_once"):
        if hasattr(reconciliation, name):
            monkeypatch.setattr(
                reconciliation, name,
                lambda *a, _n=name, **k: called.__setitem__(_n, called[_n] + 1),
            )

    _get(keys)

    assert called == {"compare": 0, "run_once": 0}


def test_every_staff_role_may_read_it(keys, fake_db, monkeypatch):  # noqa: F811
    """Visibility is not authority. Reading the control's findings concludes
    nothing and moves nothing, so it is not narrowed to approvers -- the same
    reasoning `GET /movements` and the review queue already carry."""
    monkeypatch.setattr(reconciliation, "latest_run", lambda: dict(RUN))

    for role in ("csr", "underwriter", "admin"):
        assert _get(keys, role=role).status_code == 200, role


def test_the_route_does_not_invent_a_figure_the_run_did_not_record(keys, fake_db, monkeypatch):  # noqa: F811
    """A run with nulls where a column was never written must render as null,
    not as zero. `finished_at` is nullable for a run that errored, and an
    error_code is the reason -- reporting 0 or a blank would describe a run that
    completed."""
    errored = dict(RUN, outcome="error", finished_at=None, error_code="SETTLEMENT_MISSING",
                   breaks=[], breaks_found=0, window_start=None, window_end=None)
    monkeypatch.setattr(reconciliation, "latest_run", lambda: errored)

    run = _get(keys).json()["run"]

    assert run["finished_at"] is None
    assert run["window_start"] is None
    assert run["error_code"] == "SETTLEMENT_MISSING"
    assert run["outcome"] == "error"
