"""What happened to a proposal after somebody resolved it.

`GET /movements` listed unresolved proposals and nothing else. The moment a
second person approved or rejected one it left the queue, and no API anywhere
returned it again -- so from the browser, a rejected proposal and a proposal
that had never been saved looked identical. The control was working and its
outcome was invisible.

Every field this exposes was already written by `resolve_pending_movement`, and
the `resolution_complete` constraint means a resolution cannot commit without
them. Nothing new is recorded here; a read is added for evidence the database
already held.

Two properties carry the weight:

  * **The default is unchanged.** `state` defaults to `pending`, so every
    existing caller keeps the unresolved queue it asked for. A history panel is
    not worth a silent change to what an approver sees as waiting on them.
  * **`ledger_entry_id` is the account of whether money moved**, not the status
    word beside it. An approval writes a ledger entry and a rejection does not,
    so the id is present or absent as a consequence of the same transaction that
    moved the money. A test that asserted only on `resolution` would pass on a
    row whose two halves disagreed.

Authority is deliberately NOT widened: resolved history is readable by exactly
the principals who could already read the pending queue. These are the same
proposals, and hiding their outcome from the people who watched them wait would
protect nothing.
"""
import datetime
from decimal import Decimal

import pytest

from app import maker_checker

from tests.test_maker_checker_api import (  # noqa: F401  -- used by fixture name
    TOKEN, _client, _headers, fake_db, keys, no_money,
)


RESOLVED_SQL = "FROM pending_movements WHERE resolution IS NOT NULL"


def _rows():
    """One approved and one rejected row, as the database would return them."""
    when = datetime.datetime(2026, 8, 27, 9, 30, tzinfo=datetime.timezone.utc)
    return [
        {"id": 41, "loan_id": 7300, "component": "fees", "amount": Decimal("-40.00"),
         "entry_type": "fee_waived", "reason": "goodwill", "requested_by": 1,
         "requested_role": "csr", "requested_at": when, "resolution": "approved",
         "resolved_by": 2, "resolved_role": "admin", "resolved_at": when,
         "ledger_entry_id": 991, "resolved_threshold": Decimal("500.00")},
        {"id": 40, "loan_id": 7298, "component": "principal",
         "amount": Decimal("-250.00"), "entry_type": "adjustment",
         "reason": "raised in error", "requested_by": 2, "requested_role": "csr",
         "requested_at": when, "resolution": "rejected", "resolved_by": 3,
         "resolved_role": "admin", "resolved_at": when,
         # A rejection moved no money, so there is no entry to point at. NULL is
         # the answer here rather than missing data.
         "ledger_entry_id": None, "resolved_threshold": Decimal("500.00")},
    ]


@pytest.fixture
def history(fake_db, monkeypatch):  # noqa: F811
    """Answers the resolved SELECT, and records that it was the one issued."""
    inner = maker_checker.db.query
    seen = {"sql": None, "params": None}

    def _query(sql, params=None):
        flat = " ".join(sql.split())
        if RESOLVED_SQL in flat:
            seen["sql"], seen["params"] = flat, params
            return _rows()
        return inner(sql, params)

    monkeypatch.setattr(maker_checker.db, "query", _query)
    return seen


def _get(keys, query="", role="underwriter"):  # noqa: F811
    return _client().get("/movements" + query, headers=_headers(keys, role=role))


def test_the_default_is_still_the_pending_queue(keys, fake_db, history):  # noqa: F811
    """No existing caller changes behaviour by this route gaining a parameter."""
    response = _get(keys)

    assert response.status_code == 200, response.text
    assert response.json()["movements"] == []
    assert history["sql"] is None, "the default asked for resolved rows"


def test_state_pending_is_the_same_answer_as_no_state(keys, fake_db, history):  # noqa: F811
    assert _get(keys, "?state=pending").json() == _get(keys).json()


def test_resolved_returns_the_decided_proposals(keys, fake_db, history):  # noqa: F811
    response = _get(keys, "?state=resolved")

    assert response.status_code == 200, response.text
    ids = [m["id"] for m in response.json()["movements"]]
    assert ids == [41, 40]


def test_an_approval_carries_the_ledger_entry_it_wrote(keys, fake_db, history):  # noqa: F811
    """The evidence, and the reason this panel is worth having at all."""
    approved = _get(keys, "?state=resolved").json()["movements"][0]

    assert approved["resolution"] == "approved"
    assert approved["ledger_entry_id"] == 991
    assert approved["resolved_by"] == 2 and approved["resolved_role"] == "admin"


def test_a_rejection_carries_no_ledger_entry(keys, fake_db, history):  # noqa: F811
    """A rejection is a complete, correct outcome that moved no money. The null
    is the account of that, and it must survive the response rather than being
    filled in with a zero or dropped."""
    rejected = _get(keys, "?state=resolved").json()["movements"][1]

    assert rejected["resolution"] == "rejected"
    assert rejected["ledger_entry_id"] is None


def test_the_second_person_is_named_on_every_resolved_row(keys, fake_db, history):  # noqa: F811
    """That a DIFFERENT person resolved it is the entire control. A history that
    said only "resolved" would not show the control had held."""
    for movement in _get(keys, "?state=resolved").json()["movements"]:
        assert movement["resolved_by"] is not None and movement["resolved_role"]
        assert movement["resolved_by"] != movement["requested_by"]


def test_the_threshold_it_was_judged_against_is_returned(keys, fake_db, history):  # noqa: F811
    """spec 0002 AC-22: a history of approvals is unreadable if the bar moved and
    nothing says when. Recorded at resolution time, not read from configuration
    now -- so it is a value on the row, not a lookup."""
    approved = _get(keys, "?state=resolved").json()["movements"][0]

    assert approved["resolved_threshold"] == 500.00


def test_a_null_threshold_stays_null_rather_than_becoming_zero(fake_db, monkeypatch):  # noqa: F811
    """An absent threshold is not a threshold of zero, and rendering it as
    "judged against $0.00" would state something the row does not say."""
    row = dict(_rows()[0], resolved_threshold=None)
    monkeypatch.setattr(maker_checker.db, "query", lambda sql, params=None: [row])

    assert maker_checker.resolved()[0]["resolved_threshold"] is None


def test_the_history_is_ordered_most_recently_resolved_first(keys, fake_db, history):  # noqa: F811
    """A recent-decisions panel whose newest row is at the bottom is not one."""
    _get(keys, "?state=resolved")

    assert "ORDER BY resolved_at DESC" in history["sql"]


def test_the_history_is_bounded(keys, fake_db, history):  # noqa: F811
    """This is a panel, not an audit export. The immutable account of an approved
    movement is the ledger entry it points at."""
    _get(keys, "?state=resolved")

    assert "LIMIT" in history["sql"]
    assert history["params"] == (25,)


@pytest.mark.parametrize("bad", ["all", "PENDING", "", "1"])
def test_an_unrecognised_state_is_refused(keys, fake_db, history, bad):  # noqa: F811
    """A validated enum, so no caller-supplied value ever reaches a predicate."""
    assert _get(keys, "?state=" + bad).status_code == 422


def test_resolved_history_still_requires_the_internal_token(keys, fake_db, history):  # noqa: F811
    response = _client().get(
        "/movements?state=resolved",
        headers={"X-Principal-Assertion": _headers(keys)["X-Principal-Assertion"]})

    assert response.status_code in (401, 403), response.text


def test_resolved_history_still_requires_a_verified_staff_principal(keys, fake_db, history):  # noqa: F811
    """The unforgeable half. A caller holding the internal token but presenting
    no signed principal reads the pending queue no more than it reads this."""
    response = _client().get(
        "/movements?state=resolved", headers={"X-Internal-Token": TOKEN})

    assert response.status_code in (401, 403), response.text


def test_a_borrower_principal_cannot_read_the_history(keys, fake_db, history):  # noqa: F811
    response = _get(keys, "?state=resolved", role="borrower")

    assert response.status_code in (401, 403), response.text


def test_a_csr_may_read_the_history_just_as_it_may_read_the_queue(keys, fake_db, history):  # noqa: F811
    """Visibility is not authority. A CSR sees both and resolves neither -- and
    the outcome of a proposal it watched wait is not a secret from it."""
    assert _get(keys, "?state=resolved", role="csr").status_code == 200
