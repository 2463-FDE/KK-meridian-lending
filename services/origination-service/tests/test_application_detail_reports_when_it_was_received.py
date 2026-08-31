"""`GET /applications/{id}` reports when the application was received.

The staff console has a "Received" tile. It read `app.created_at` off the
detail response, `ApplicationDetail` never declared the field, so the value was
`undefined` and the tile rendered an em dash for **every** application in the
system -- while `applications.created_at` had held the answer since the table
existed and `models.Application` had always mapped it.

That is the same shape as the boarded-loan id PR #130 fixed: a fact the database
holds, a screen that shows a blank, and nothing between them reading it back.

The negative cases matter as much as the positive one. A missing timestamp must
report none rather than a fabricated one, and nothing here may infer a received
date from the id or from the first decision event -- an inferred date is a second
answer to a question the row already answers.
"""
import datetime

from app import schemas


class _App:
    """The columns this response reads. Shaped like the ORM row, not a mock of it."""

    def __init__(self, created_at, app_id=7301):
        self.id = app_id
        self.applicant_id = 1
        self.amount = 15000.0
        self.term_months = 36
        self.purpose = "Debt Consolidation"
        self.status = "submitted"
        self.employer = "Fictional Testing Co"
        self.job_title = "QA Analyst"
        self.income = 60000.0
        self.employment_years = 3.0
        self.created_at = created_at


def _detail(app):
    """Build the response the way the route does, without a database.

    The route's own serialization line is duplicated here deliberately rather
    than imported: it is one expression, and importing the router would pull in a
    session dependency this case does not need. `test_the_route_serializes_it`
    below pins that the route agrees with this.
    """
    return schemas.ApplicationDetail(
        id=app.id,
        amount=app.amount,
        term_months=app.term_months,
        purpose=app.purpose,
        status=app.status,
        created_at=(app.created_at.isoformat()
                    if hasattr(app.created_at, "isoformat") else app.created_at),
        employer=app.employer,
        job_title=app.job_title,
    )


def test_the_field_exists_on_the_response_model():
    """The whole defect was that it did not."""
    assert "created_at" in schemas.ApplicationDetail.model_fields


def test_a_real_timestamp_is_reported_as_iso_8601():
    received = datetime.datetime(2026, 8, 30, 14, 5, 9,
                                 tzinfo=datetime.timezone.utc)
    detail = _detail(_App(received))
    assert detail.created_at == "2026-08-30T14:05:09+00:00"


def test_the_timestamp_round_trips_through_the_json_body():
    """What the browser actually receives."""
    received = datetime.datetime(2026, 8, 30, 14, 5, 9,
                                 tzinfo=datetime.timezone.utc)
    body = _detail(_App(received)).model_dump()
    assert body["created_at"] == "2026-08-30T14:05:09+00:00"
    # A date the browser can parse, rather than a Python repr.
    assert datetime.datetime.fromisoformat(body["created_at"]) == received


def test_a_missing_timestamp_reports_none_rather_than_a_guess():
    """The column is nullable. A row with no timestamp says so.

    Rendering "unknown" is correct here; inventing one from the id, from
    `decisions.decided_at`, or from the current time would put a date on the
    screen that nothing in the database supports.
    """
    assert _detail(_App(None)).created_at is None


def test_a_string_timestamp_passes_through_unchanged():
    """Defends the `hasattr` branch.

    The value arrives as a datetime from the ORM and as a string from the fake
    sessions used elsewhere in this suite. An `isoformat()` call on a str raises,
    and an isinstance check against datetime would silently drop the string.
    """
    assert _detail(_App("2026-08-30T14:05:09+00:00")).created_at == \
        "2026-08-30T14:05:09+00:00"


def test_the_route_serializes_it_the_same_way():
    """The route and this file must not drift apart.

    Read out of the router source rather than asserted by calling it, because
    calling it needs a session, an applicant, a KYC row and a decision. What is
    being pinned is that the route passes `created_at` through the same
    `hasattr`/`isoformat` expression used above -- if somebody changes one, this
    fails rather than the two quietly disagreeing.
    """
    import inspect

    from app.routers import applications

    source = inspect.getsource(applications.get_application)
    assert "created_at=" in source, (
        "the route no longer sends created_at, so the Received tile is blank again")
    assert 'hasattr(a.created_at, "isoformat")' in source, (
        "the route's created_at serialization diverged from the one this file "
        "asserts")
