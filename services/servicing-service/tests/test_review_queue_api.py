"""Who may read the review queue, who may answer it, and what an answer does.

The client's decision of 2026-08-24 authorised the in-app queue as the ONLY
destination for a payment flagged for review, and was explicit about what a flag
is not: not a duplicate finding, not a validity conclusion, not permission to
move money. Two of those are properties of this API rather than of the table --
a route that recorded a disposition and then reversed a payment would satisfy
every constraint in `db/migrations/0045` and still break the instruction.

So what is tested here is the API's part: that the reviewer is a verified human
rather than a claimed header, that an answer is write-once, that no money moves,
and that the payload carries no instrument or token material.
"""
import time
from decimal import Decimal

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from app import main, principal, review_queue


TOKEN = "test-internal-token"


@pytest.fixture
def keys(monkeypatch):
    private = Ed25519PrivateKey.generate()
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()).decode()
    public_pem = private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    monkeypatch.setattr(main.config, "PRINCIPAL_VERIFY_KEY", public_pem)
    monkeypatch.setattr(principal.config, "PRINCIPAL_VERIFY_KEY", public_pem)
    monkeypatch.setattr(main.config, "INTERNAL_SERVICE_TOKEN", TOKEN)
    return private_pem


def _row(item_id=1, *, signal="heuristic_30_minute_candidate", status="open",
         disposition=None, related=True):
    """One joined row in the shape `review_queue._SELECT` produces."""
    row = {
        "id": item_id, "created_at": "2026-08-24 10:00:00+00",
        "signal_type": signal, "loan_id": 42, "correlation_ref": "corr-abc",
        "queue": "reconciliation_review", "status": status,
        "disposition": disposition, "disposition_note": None,
        "reviewed_at": "2026-08-24 11:00:00+00" if disposition else None,
        "reviewed_by": "7" if disposition else None,
        "reviewed_by_role": "csr" if disposition else None,
        "payment_id": 500, "payment_amount": Decimal("250.00"),
        "payment_method": "card", "payment_captured_at": "2026-08-24 09:59:00+00",
        "payment_auth_status": "captured",
    }
    related_fields = {
        "related_id": 499, "related_amount": Decimal("250.00"),
        "related_method": "card", "related_captured_at": "2026-08-24 09:41:00+00",
        "related_auth_status": "captured",
    } if related else {
        "related_id": None, "related_amount": None, "related_method": None,
        "related_captured_at": None, "related_auth_status": None,
    }
    row.update(related_fields)
    return row


@pytest.fixture
def fake_db(monkeypatch):
    """Records every statement, so a test can assert what did NOT run."""
    state = {"rows": [_row()], "updates": [], "sql": []}

    def _query(sql, params=None):
        flat = " ".join(sql.split())
        state["sql"].append((flat, params))
        if flat.startswith("UPDATE reconciliation_review_items"):
            state["updates"].append(params)
            # Emulates the statement AS WRITTEN, not as intended. The first
            # version of this fake filtered on `status == "open"` because the
            # real SQL was supposed to -- so deleting `AND status = 'open'` from
            # the query left every test green: the fake was enforcing
            # write-once, not the code. It now reads the clause out of the SQL,
            # so removing it from the query removes it from the fake too.
            # `test_review_queue_real_postgres.py` is the version with no fake
            # at all.
            target = params[4]
            only_open = "status = 'open'" in flat
            hits = [r for r in state["rows"]
                    if r["id"] == target
                    and (r["status"] == "open" or not only_open)]
            for r in hits:
                r["status"] = "reviewed"
                r["disposition"] = params[0]
                r["disposition_note"] = params[1]
                r["reviewed_by"] = params[2]
                r["reviewed_by_role"] = params[3]
                r["reviewed_at"] = "2026-08-24 12:00:00+00"
            return [{"id": target}] if hits else []
        if "GROUP BY signal_type, status" in flat:
            counted = {}
            for r in state["rows"]:
                counted[(r["signal_type"], r["status"])] = counted.get(
                    (r["signal_type"], r["status"]), 0) + 1
            return [{"signal_type": s, "status": st, "n": n}
                    for (s, st), n in counted.items()]
        if "WHERE r.id = %s" in flat:
            return [r for r in state["rows"] if r["id"] == params[0]]
        if "WHERE r.status = %s" in flat:
            return [r for r in state["rows"] if r["status"] == params[0]]
        return []

    monkeypatch.setattr(review_queue.db, "query", _query)
    return state


@pytest.fixture
def no_money(monkeypatch):
    """Explodes if reading or dispositioning touches a balance or the ledger."""
    def _boom(*a, **kw):                                     # pragma: no cover
        raise AssertionError("a review-queue route moved money")

    for fn in ("adjust_balance", "waive_fee", "apply_payment"):
        monkeypatch.setattr(main.balance, fn, _boom, raising=False)


def _headers(private_pem, *, sub="7", role="csr", token=TOKEN):
    now = int(time.time())
    claims = {"iss": "meridian-gateway", "aud": "servicing-service", "sub": sub,
              "role": role, "iat": now, "nbf": now, "exp": now + 120, "jti": "t"}
    return {"X-Internal-Token": token,
            "X-Principal-Assertion": jwt.encode(claims, private_pem,
                                                algorithm="EdDSA"),
            "X-User-Id": sub, "X-User-Role": role}


def _client():
    return TestClient(main.app)


# --- reading it ---------------------------------------------------------------


def test_the_queue_needs_a_verified_human_not_just_the_internal_token(keys, fake_db):
    """The internal token identifies a service. This returns payment amounts for
    real loans, so it asks who the person is."""
    response = _client().get("/reconciliation/review-queue",
                             headers={"X-Internal-Token": TOKEN})

    assert response.status_code == 401, response.text


def test_the_queue_refuses_a_borrower(keys, fake_db):
    response = _client().get("/reconciliation/review-queue",
                             headers=_headers(keys, role="borrower"))

    assert response.status_code == 403


def test_the_queue_refuses_a_missing_internal_token(keys, fake_db):
    response = _client().get("/reconciliation/review-queue",
                             headers=_headers(keys, token=""))

    assert response.status_code == 401


@pytest.mark.parametrize("role", ["csr", "underwriter", "admin"])
def test_any_staff_role_may_read_it(keys, fake_db, role):
    """Visibility is not authority, the same rule `GET /movements` follows.
    Reading the queue concludes nothing about any payment."""
    response = _client().get("/reconciliation/review-queue",
                             headers=_headers(keys, role=role))

    assert response.status_code == 200, response.text


def test_an_item_shows_both_payments_so_the_question_can_be_answered(keys, fake_db):
    item = _client().get("/reconciliation/review-queue",
                         headers=_headers(keys)).json()["items"][0]

    # "Same payment twice, or a second real one?" is unanswerable without the
    # amounts and the times, and this is the authenticated surface the client
    # authorised as the destination.
    assert item["payment"]["amount"] == "250.00"
    assert item["related_payment"]["amount"] == "250.00"
    assert item["payment"]["captured_at"] and item["related_payment"]["captured_at"]


def test_an_item_carries_no_instrument_or_token_material(keys, fake_db):
    """The client's constraint on review data, asserted on the whole payload
    rather than field by field: a column added to the projection later is caught
    by this without anyone remembering to extend a list."""
    body = _client().get("/reconciliation/review-queue",
                         headers=_headers(keys)).json()
    flat = repr(body)

    for forbidden in ("last4", "brand", "processor_token", "processor_ref",
                      "authorization_id", "idempotency_key", "source_ref",
                      "applicant", "cardholder"):
        assert forbidden not in flat, (
            "the review queue payload carries %r; a reviewer decides this "
            "question from the amount, channel and time, and a queue is exactly "
            "the surface that gets screenshotted into a ticket" % forbidden)


def test_the_amount_is_a_string_not_a_float(keys, fake_db):
    """NUMERIC(14,2) arrives as Decimal. Serialising it through float is the D12
    defect the column type was changed to fix."""
    item = _client().get("/reconciliation/review-queue",
                         headers=_headers(keys)).json()["items"][0]

    assert isinstance(item["payment"]["amount"], str)


def test_an_item_with_no_related_payment_reads_as_absent_not_as_zero(keys, fake_db):
    """A provider-reference collision may not know which earlier capture holds
    the reference. `related_payment: null` says that; a zeroed amount would tell
    a reviewer a payment of 0.00 exists."""
    fake_db["rows"] = [_row(related=False)]

    item = _client().get("/reconciliation/review-queue",
                         headers=_headers(keys)).json()["items"][0]

    assert item["related_payment"] is None


def test_the_response_says_a_flag_is_not_a_conclusion(keys, fake_db):
    """In the payload, not only in the UI. A client that renders this list under
    a heading of its own choosing still carries the sentence."""
    body = _client().get("/reconciliation/review-queue",
                         headers=_headers(keys)).json()

    assert "not permission to move money" in body["note"]
    assert "not a duplicate finding" in body["note"]


def test_the_exact_and_heuristic_signals_are_labelled_apart(keys, fake_db):
    """The client drew this line itself, and a reviewer must see it: a repeated
    provider reference is strong evidence, while four factors agreeing inside 30
    minutes is routinely a legitimate second payment."""
    fake_db["rows"] = [_row(1, signal="exact_idempotency_key"),
                       _row(2, signal="heuristic_30_minute_candidate")]

    items = _client().get("/reconciliation/review-queue",
                          headers=_headers(keys)).json()["items"]

    assert {i["id"]: i["signal_category"] for i in items} == {1: "exact",
                                                             2: "heuristic"}


def test_the_counts_carry_no_payment_or_person(keys, fake_db):
    """Exactly what the client permitted telemetry to carry: that items exist,
    the queue, the signal category, the status."""
    fake_db["rows"] = [_row(1, signal="exact_idempotency_key"),
                       _row(2, signal="heuristic_30_minute_candidate"),
                       _row(3, status="reviewed", disposition="confirmed_duplicate")]

    counts = _client().get("/reconciliation/review-queue",
                           headers=_headers(keys)).json()["counts"]

    assert counts == {"open_exact": 1, "open_heuristic": 1, "reviewed": 1}


# --- answering it -------------------------------------------------------------


def test_a_disposition_records_the_verified_human_not_the_claimed_header(
        keys, fake_db, no_money):
    """The whole reason this route lives in servicing rather than beside the
    detector: the name stored beside the answer is verified."""
    response = _client().post(
        "/reconciliation/review-queue/1/disposition",
        json={"disposition": "legitimate_distinct_payment", "note": "two rent cheques"},
        headers=_headers(keys, sub="7", role="csr"))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "reviewed"
    assert body["disposition"] == "legitimate_distinct_payment"
    assert body["reviewed_by"] == "7" and body["reviewed_by_role"] == "csr"


def test_a_disposition_moves_no_money(keys, fake_db, no_money):
    """`confirmed_duplicate` is the answer most likely to be mistaken for an
    instruction. Recording it must leave the ledger, the balances projection and
    the payment itself untouched -- a reversal still goes through the
    maker-checker, with the second person that requires."""
    _client().post("/reconciliation/review-queue/1/disposition",
                   json={"disposition": "confirmed_duplicate"},
                   headers=_headers(keys))

    written = [sql for sql, _ in fake_db["sql"]
               if sql.startswith(("UPDATE", "INSERT", "DELETE"))]
    assert len(written) == 1, written
    assert written[0].startswith("UPDATE reconciliation_review_items")
    for table in ("ledger_entries", "balances", "payments", "pending_movements"):
        assert not any(table in sql for sql in written), (
            "recording a review answer wrote to %s" % table)


def test_a_disposition_is_write_once(keys, fake_db, no_money):
    """A human classification is evidence, and evidence that can be quietly
    rewritten is not evidence."""
    first = _client().post("/reconciliation/review-queue/1/disposition",
                           json={"disposition": "confirmed_duplicate"},
                           headers=_headers(keys))
    assert first.status_code == 200, first.text

    second = _client().post("/reconciliation/review-queue/1/disposition",
                            json={"disposition": "legitimate_distinct_payment"},
                            headers=_headers(keys, sub="9", role="admin"))

    assert second.status_code == 409, second.text
    assert "write-once" in second.text
    # And the first answer survived, unchanged.
    assert fake_db["rows"][0]["disposition"] == "confirmed_duplicate"
    assert fake_db["rows"][0]["reviewed_by"] == "7"


def test_the_update_is_conditional_on_the_item_still_being_open(keys, fake_db, no_money):
    """Not a read-then-write. Two reviewers opening the same item is the ordinary
    case in a shared queue, and a check-first would let the second overwrite the
    first in the gap between."""
    _client().post("/reconciliation/review-queue/1/disposition",
                   json={"disposition": "confirmed_duplicate"},
                   headers=_headers(keys))

    update = next(sql for sql, _ in fake_db["sql"]
                  if sql.startswith("UPDATE reconciliation_review_items"))

    assert "status = 'open'" in update, (
        "the disposition UPDATE does not require the item to still be open, so "
        "a second reviewer can overwrite the first one's answer")


def test_a_missing_item_is_a_conflict_not_a_crash(keys, fake_db, no_money):
    response = _client().post("/reconciliation/review-queue/9999/disposition",
                              json={"disposition": "requires_further_review"},
                              headers=_headers(keys))

    assert response.status_code == 409
    assert "does not exist" in response.text


def test_a_fourth_disposition_is_refused(keys, fake_db, no_money):
    """The three the client authorised, and no fourth. A fourth would be a
    policy this repository has no authority to invent."""
    response = _client().post("/reconciliation/review-queue/1/disposition",
                              json={"disposition": "not_a_duplicate"},
                              headers=_headers(keys))

    assert response.status_code == 422
    assert not fake_db["updates"], "a rejected disposition still reached the database"


def test_the_route_and_the_module_permit_the_same_three(keys):
    """Two lists, one policy. The route validates with a `Literal` (which needs
    constants at class-creation time) and the module holds the tuple, so this
    fails the moment they disagree."""
    literal = main.DispositionIn.model_fields["disposition"].annotation

    assert set(literal.__args__) == set(review_queue.DISPOSITIONS)


def test_a_disposition_refuses_an_unverified_caller(keys, fake_db, no_money):
    response = _client().post("/reconciliation/review-queue/1/disposition",
                              json={"disposition": "confirmed_duplicate"},
                              headers={"X-Internal-Token": TOKEN})

    assert response.status_code == 401
    assert not fake_db["updates"]


def test_a_disposition_refuses_a_borrower(keys, fake_db, no_money):
    response = _client().post("/reconciliation/review-queue/1/disposition",
                              json={"disposition": "confirmed_duplicate"},
                              headers=_headers(keys, role="borrower"))

    assert response.status_code == 403
    assert not fake_db["updates"]


def test_an_extra_field_is_refused(keys, fake_db, no_money):
    """`extra: forbid`. A caller sending `reverse: true` should be told the field
    means nothing here, not have it silently dropped."""
    response = _client().post(
        "/reconciliation/review-queue/1/disposition",
        json={"disposition": "confirmed_duplicate", "reverse_payment": True},
        headers=_headers(keys))

    assert response.status_code == 422
