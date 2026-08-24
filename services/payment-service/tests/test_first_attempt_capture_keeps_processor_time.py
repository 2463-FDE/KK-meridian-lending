"""The FIRST attempt is the path almost every capture takes, and it was the one
still stamping our own clock.

`test_recovered_capture_keeps_processor_time.py` fixed the crash-recovery branch:
a row completed today for money the processor took yesterday keeps the
processor's `captured_at`. The happy path did not get the same treatment, and it
could not, because `authorize_charge()` returned the authorization id and threw
the rest of the processor's answer away.

So a normal charge whose processor confirmation and local UPDATE straddle
midnight -- a slow authorization, a retried HTTP call, a clock skew between us
and the processor -- was scoped to the wrong reconciliation day. Reconciliation
windows on `captured_at`, so that is a settlement-only break on day N and a
ledger-only break on day N+1: two false findings, on the busiest path, from
nothing being wrong.

The same call now also carries the processor's settlement reference
(`processor_ref`, db/migrations/0041). Without it no captured row has a join key
to the settlement file, and reconciliation can only compare net totals per loan
-- which lets two offsetting defects on one loan cancel and report a clean run.

Both facts are asserted on both branches, and the fallbacks are asserted too: a
processor that reports no timestamp must not block the capture, and a missing
timestamp must not cost the reference.
"""
import pytest

from app import payments, processor

# The processor confirms at 23:58 on the 8th; our UPDATE runs at 00:04 on the
# 9th. Under the old code the row would have claimed the 9th.
PROCESSOR_TIME = "2026-08-08T23:58:00+00:00"
PROCESSOR_REF = "PR-100231"


class _FakeDb:
    """Enough of db.query to observe what the capture UPDATE writes."""

    def __init__(self):
        self.rows = {}
        self.captured_with = None
        self.reference_with = None
        self.capture_sql = None

    def query(self, sql, params=None):
        stmt = " ".join(sql.split())
        if stmt.startswith("INSERT INTO payments"):
            # 0043 added correlation_id to the INSERT; unpacked strictly so a
            # dropped column fails here rather than storing a NULL.
            (loan_id, last4, brand, amount, method, key, correlation_id,
             source_ref) = params
            row = {"id": 501, "loan_id": loan_id, "amount": amount,
                   "auth_status": "pending", "applied_at": None,
                   "correlation_id": correlation_id}
            self.rows[501] = row
            return [row]
        if "auth_status = 'captured'" in stmt:
            _auth, captured_at, processor_ref, pid = params
            self.captured_with = captured_at
            self.reference_with = processor_ref
            self.rows[pid]["auth_status"] = "captured"
            self.capture_sql = stmt
        return []


@pytest.fixture
def fake(monkeypatch):
    db = _FakeDb()
    monkeypatch.setattr(payments, "db", db)
    monkeypatch.setattr(payments, "_require_servicing_auth", lambda *a, **k: None)
    monkeypatch.setattr(payments, "_apply_via_servicing", lambda *a, **k: True)
    return db


def _charge(key="first-attempt-1"):
    return payments.charge(
        loan_id=4471, processor_token="tok_x", amount=250.00,
        last4="4242", brand="visa", idempotency_key=key,
    )


# --- the reported defect ------------------------------------------------------

def test_a_first_attempt_capture_uses_the_processor_timestamp(fake, monkeypatch):
    monkeypatch.setattr(
        processor, "authorize_charge",
        lambda *a, **k: processor.Authorization("auth-1", PROCESSOR_TIME, PROCESSOR_REF),
    )

    _charge()

    assert fake.captured_with == PROCESSOR_TIME, (
        "the first-attempt capture was stamped with our clock rather than the "
        "processor's. A charge confirmed at 23:58 and written at 00:04 is then "
        "reconciled against the wrong day, which is a settlement-only break on "
        "one day and a ledger-only break on the next -- the exact false-break "
        "class db/migrations/0040 exists to close, left open on the path almost "
        "every capture takes."
    )


def test_a_first_attempt_capture_stores_the_settlement_reference(fake, monkeypatch):
    monkeypatch.setattr(
        processor, "authorize_charge",
        lambda *a, **k: processor.Authorization("auth-1", PROCESSOR_TIME, PROCESSOR_REF),
    )

    _charge()

    assert fake.reference_with == PROCESSOR_REF, (
        "the capture stored no processor_ref, so it has no join key to the "
        "settlement file and reconciliation falls back to per-loan totals -- "
        "where two offsetting defects on one loan cancel out and the run "
        "reports ok (db/migrations/0041)"
    )


# --- the fallbacks, which must stay fallbacks --------------------------------

def test_a_processor_that_reports_no_time_falls_back_to_our_clock(fake, monkeypatch):
    """Not every processor reports one. Passing NULL lets the SQL COALESCE to
    now(), which is the previous behaviour and the best estimate available --
    but it must be reached only when the processor genuinely said nothing."""
    monkeypatch.setattr(
        processor, "authorize_charge",
        lambda *a, **k: processor.Authorization("auth-1", None, PROCESSOR_REF),
    )

    _charge()

    assert fake.captured_with is None
    assert fake.reference_with == PROCESSOR_REF, (
        "a missing timestamp cost the reference as well -- they are independent "
        "facts and the row can still be matched to its settlement line"
    )


def test_a_processor_that_reports_no_reference_still_captures(fake, monkeypatch):
    """The money has already moved. Refusing to record the capture because the
    reference is missing would strand a real charge, which is strictly worse
    than a break an operator has to investigate."""
    monkeypatch.setattr(
        processor, "authorize_charge",
        lambda *a, **k: processor.Authorization("auth-1", PROCESSOR_TIME, None),
    )

    result = _charge()

    assert result["status"] == "captured"
    assert fake.reference_with is None
    assert fake.captured_with == PROCESSOR_TIME


# --- authorize_charge() must actually read those fields ----------------------

def test_authorize_charge_reads_the_time_and_reference_from_the_response(monkeypatch):
    """The root cause was here: the function had the processor's answer in hand
    and returned one field of it."""
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"approved": True, "authorization_id": "auth-real",
                    "captured_at": PROCESSOR_TIME, "processor_ref": PROCESSOR_REF}

    monkeypatch.setattr(processor, "PROCESSOR_API_KEY", "live-key")
    monkeypatch.setattr(processor.httpx, "post", lambda *a, **k: _Resp())

    auth = processor.authorize_charge("tok_mock_x", 250.00, "k-1")

    assert auth.authorization_id == "auth-real"
    assert auth.captured_at == PROCESSOR_TIME
    assert auth.processor_ref == PROCESSOR_REF


@pytest.mark.parametrize("field", ["processor_ref", "settlement_reference", "reference"])
def test_the_reference_is_read_under_any_of_its_common_names(monkeypatch, field):
    """Processors name the settlement handle differently. The one that matters is
    whichever name appears in the settlement file, so all three are accepted
    rather than one being guessed at."""
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"approved": True, "authorization_id": "auth-real",
                    field: PROCESSOR_REF}

    monkeypatch.setattr(processor, "PROCESSOR_API_KEY", "live-key")
    monkeypatch.setattr(processor.httpx, "post", lambda *a, **k: _Resp())

    assert processor.authorize_charge("tok_mock_x", 250.00, "k-1").processor_ref == PROCESSOR_REF


def test_the_stub_reference_is_unique_per_idempotency_key():
    """`idx_payments_processor_ref` is UNIQUE. A stub reference derived from the
    card token would collide across the many payments a demo makes with the same
    mock card, and the second capture would fail to record at all."""
    a = processor._stub_settlement_reference("tok_mock_same", "key-1")
    b = processor._stub_settlement_reference("tok_mock_same", "key-2")

    assert a != b
    assert a.startswith("PR-STUB-"), (
        "a stub reference that did not announce itself as a stub could be read "
        "as evidence from a real processor"
    )
    assert processor._stub_settlement_reference("tok_mock_same", "key-1") == a, (
        "the same key produced two references, so a retry would not match the "
        "capture it is retrying"
    )


# --- the guard: neither column may drift away from the status write ----------

def _capture_statements():
    """The text of every `db.query(...)` call in payments.py that captures.

    Extracted by matching the call's own parentheses rather than by splitting the
    file on ';'. The prose around this code contains semicolons, so a split
    version cut a statement in half and then found the column name it was looking
    for in a COMMENT -- a guard that passes on documentation is not a guard.
    """
    import pathlib

    source = (pathlib.Path(__file__).resolve().parents[1]
              / "app" / "payments.py").read_text(encoding="utf-8")

    calls = []
    marker = "db.query("
    at = source.find(marker)
    while at != -1:
        depth, i = 0, at + len(marker) - 1
        while i < len(source):
            if source[i] == "(":
                depth += 1
            elif source[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        calls.append(source[at:i + 1])
        at = source.find(marker, i)
    return [c for c in calls if "auth_status = 'captured'" in c]


def test_every_capture_update_writes_both_the_time_and_the_reference():
    """Mirrors the `captured_at` guard in servicing-service's
    test_reconciliation_window_uses_capture_time.py, extended to the reference.

    A capture UPDATE that sets `auth_status` without one of these produces a row
    that records that money moved but not when, or not against which settlement
    line -- and either restores a defect this PR closed. Asserted against the
    source so a NEW capture path cannot be added without them.
    """
    statements = _capture_statements()

    assert len(statements) == 2, (
        "expected exactly the two capture paths -- first attempt and recovered "
        "pending row -- and found %d. A third path would need the same two "
        "columns and this guard would not have been read." % len(statements)
    )
    for stmt in statements:
        assert "captured_at = COALESCE(" in stmt, (
            "a capture UPDATE sets auth_status without taking captured_at from "
            "the processor, so a charge that straddles midnight is reconciled "
            "against the wrong day"
        )
        assert "processor_ref = %s" in stmt, (
            "a capture UPDATE sets auth_status without processor_ref, so the "
            "capture has no join key to the settlement file and reconciliation "
            "reports it as unreferenced"
        )
        assert "capture_source = 'processor'" in stmt, (
            "a capture UPDATE sets auth_status without capture_source, so the "
            "row keeps db/migrations/0042's default of 'unknown' and "
            "reconciliation drops it from the comparison entirely -- the run "
            "reports ok while comparing a settlement file against a ledger side "
            "the filter emptied"
        )


def test_a_capture_is_marked_in_scope_for_reconciliation(fake, monkeypatch):
    """The reported defect, at the write itself.

    Servicing's ledger side is `WHERE capture_source = 'processor'`. A capture
    that does not set it keeps 0042's default of 'unknown' and is excluded, so
    the control compares the settlement file against nothing and reports ok --
    the vacuous success this whole PR exists to prevent, arriving through the one
    column that decides what gets compared.
    """
    monkeypatch.setattr(
        processor, "authorize_charge",
        lambda *a, **k: processor.Authorization("auth-1", PROCESSOR_TIME, PROCESSOR_REF),
    )

    _charge()

    assert "capture_source = 'processor'" in fake.capture_sql, (
        "the capture UPDATE did not mark the row as processor-backed"
    )
