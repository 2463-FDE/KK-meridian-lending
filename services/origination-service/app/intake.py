"""Application intake + the LOS->LSS 'boarding' seam.

A funded loan is boarded to servicing by a DIRECT INSERT into the servicing tables
(`loans`, `balances`) from this origination code path. No boarding API, no event,
no contract. (brownfield seam #1 — see docs/architecture.md, ADR 0002)
"""
import hashlib

from .logging_config import get_logger
from . import config, db, decision_state

log = get_logger("intake")


class _LostIdempotencyRace(Exception):
    """Two requests carried the same idempotency key and this one lost.

    Raised INSIDE the transaction on purpose, so the applicant row inserted a
    moment earlier is rolled back with it. ON CONFLICT DO NOTHING does not raise,
    so without this the transaction would commit an applicant whose application
    was refused -- the orphan the key exists to prevent.
    """


class KeyedRequestNeedsResumeToken(Exception):
    """An idempotency key was sent without the recovery secret it requires.

    The key alone creates a row that CANNOT be recovered. If KYC then fails, the
    503 hands back `resume_token: null`, and the next POST with the same key
    finds the application, has nothing to authorise with, and is refused --
    leaving a recorded application the caller can only escape via support or a
    database repair.

    Refused before anything is written, so the caller retries with both or
    creates a clean unkeyed application. Persisting a keyed-but-unrecoverable
    row and reporting success is the worst of the three outcomes.
    """


class RetryPayloadMismatch(Exception):
    """A retry presented the right credentials and a different request.

    The key says WHICH application, the token says the caller MAY recover it,
    and neither says the retry is the SAME request. The browser deliberately
    keeps the credentials after a failure so the borrower can fix a mistake --
    so a corrected SSN, address or income arrived with a matching key, and the
    server served the stored copy and ran KYC and decisioning against the value
    that had just been corrected.
    """


class ResumeNotAuthorized(Exception):
    """The key named an application and the caller could not prove it may recover it.

    One exception for every failure -- missing token, wrong token, expired,
    already consumed. The caller must answer identically to all of them: telling
    them apart tells an attacker which one they achieved, and "expired" in
    particular confirms the application exists.
    """


# The fields a retry may not silently change: who the applicant is, and what
# the loan is. Derived from one tuple so the fingerprint and the documentation
# of it cannot drift -- a hand-maintained second copy of this list is the defect
# shape this repository keeps producing.
_FINGERPRINTED_FIELDS = (
    "name", "dob", "ssn", "ein", "is_entity", "email", "phone", "address",
    "zip_code", "amount", "term_months", "purpose", "income", "employer",
    "job_title", "employment_years",
)


def _request_fingerprint(payload: dict) -> str:
    """A stable sha256 over the identity and underwriting inputs.

    Normalised so cosmetic differences are not treated as a changed request:
    values are stringified, stripped, and compared case-insensitively, and the
    field order is fixed by the tuple above rather than by dict ordering. A
    borrower who retries the identical form must not get a 409 because a
    checkbox serialised as "False" instead of "false".

    The applicant's SSN is an INPUT to this hash and never stored by it -- the
    digest is one-way, and it is what goes in the column.
    """
    parts = []
    for field in _FINGERPRINTED_FIELDS:
        value = payload.get(field)
        parts.append("" if value is None else str(value).strip().lower())
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def resume_application(idempotency_key: str, resume_token: str | None,
                       fingerprint: str | None = None):
    """Recover an incomplete application. Requires the KEY and the CLIENT'S SECRET.

    Returns (app_id, raw_access_token, raw_resume_token) where the resume token
    echoed back is the caller's own secret, or None if the key names no
    application -- the ordinary first-attempt case, meaning "create one".

    Raises ResumeNotAuthorized if the key names an application and the secret
    does not authorise it.

    **Why the secret comes from the client.** An earlier version minted it on the
    server and returned it in the response. That strands the applicant in exactly
    the case this whole contract exists for: if the first POST commits the rows
    and the RESPONSE is lost -- gateway timeout, closed tab, dropped connection --
    the client never receives the token it is later required to present. It
    retries with the key alone, is refused, and has to start over. The duplicate
    it then creates is the defect the idempotency key was added to prevent.

    A credential the client generates before it sends anything is one it still
    holds when the network fails. The server stores only the sha256 hash, so the
    raw value exists in the browser and in one request body's worth of transit,
    never at rest.

    **It is not rotated.** Rotation was in the previous design and is incompatible
    with this one: handing back a NEW secret on each recovery reintroduces the
    same lost-response hole one attempt later. So the secret is stable for the
    life of the draft and cleared by the client when the application completes.
    The trade is deliberate and worth naming -- a stolen secret stays valid until
    the draft ends, which is the ordinary property of a bearer credential. The
    ACCESS token still rotates on every recovery, so the thing that authorises
    decisioning is not long-lived.
    """
    rows = db.query(
        "SELECT id, request_fingerprint FROM applications WHERE idempotency_key = %s",
        (idempotency_key,),
    )
    if not rows:
        return None

    # Same credentials, different request. Refused with a distinct error rather
    # than served the stored copy: the borrower can see the corrected value in
    # their own form, and a decision made against the old one is invisible to
    # them. A NULL fingerprint is a row written before migration 0038 -- it
    # cannot be verified, so it is accepted exactly as it was before, and an
    # upgrade does not strand an in-flight application.
    stored_fingerprint = rows[0]["request_fingerprint"]
    if fingerprint and stored_fingerprint and stored_fingerprint != fingerprint:
        log.warning(
            "refused a retry whose payload differs from the stored application "
            "(idempotency_key matched)"
        )
        raise RetryPayloadMismatch()
    if not resume_token:
        # The key names a real application and the caller offered no secret.
        # Refused, and indistinguishable from a wrong one.
        raise ResumeNotAuthorized()

    raw_access_token, access_token_hash = decision_state.new_access_token()

    # Compare-and-swap: the secret is checked inside the UPDATE, under the row
    # lock it takes, so two concurrent retries cannot both proceed on a stale
    # read. Only the ACCESS token is rotated -- the recovery secret is the
    # client's and stays put.
    updated = db.query(
        "UPDATE applications SET "
        # The token being displaced stays valid until its own original expiry
        # (migration 0039), so two overlapping retries both leave their caller
        # holding something that works. Without this the earlier caller's token
        # dies under it, and since intake clears the retry credentials on
        # success it has nothing left to recover with.
        "       prev_access_token_hash = access_token_hash, "
        "       prev_access_token_expires_at = access_token_expires_at, "
        "       access_token_hash = %s, "
        "       access_token_expires_at = now() + (%s || ' seconds')::interval, "
        "       access_token_consumed_at = NULL "
        " WHERE idempotency_key = %s "
        "   AND resume_token_hash = %s "
        "   AND resume_token_consumed_at IS NULL "
        "   AND resume_token_expires_at > now() "
        " RETURNING id",
        (access_token_hash, config.ACCESS_TOKEN_TTL_SECONDS,
         idempotency_key, decision_state.hash_access_token(resume_token)),
    )

    if not updated:
        log.warning("refused a resume -- the recovery secret did not match")
        raise ResumeNotAuthorized()

    app_id = updated[0]["id"]
    log.info("intake resumed an existing application app_id=%s", app_id)
    # The caller already has this; echoing it keeps one return shape.
    return app_id, raw_access_token, resume_token


def create_application(payload: dict, resume_token: str | None = None):
    """Insert applicant + application.

    Returns (app_id, raw_access_token). The token is minted here, at
    submission, and handed back to the caller exactly once (see
    routers/applications.py submit_application) -- the borrower has no account
    yet at this point, so this is what proves they're the one who submitted
    this application when they come back to request their first decision
    (app_id alone is a guessable integer -- see run_decision).

    Security fix (PR #6 review, Gap B): only the token's sha256 hash is
    persisted, alongside a Postgres-clock expiry and a single-use consumed
    marker. The raw value exists in this function and in the response body,
    nowhere else -- never in the database, never in a log line.
    """
    idempotency_key = payload.get("idempotency_key")
    fingerprint = _request_fingerprint(payload) if idempotency_key else None

    # A key with no recovery secret is refused BEFORE anything is written.
    #
    # The two travel together or not at all. Storing the key with a NULL
    # resume_token_hash creates a row that can never be resumed: a later KYC
    # failure returns `resume_token: null`, and the retry finds the application
    # and has nothing to authorise with. Better to refuse the request than to
    # record an application the caller cannot reach.
    if idempotency_key and not resume_token:
        raise KeyedRequestNeedsResumeToken()

    # A retry of a submission that already exists resumes it. Checked before the
    # applicant INSERT, because the duplicate this prevents is a duplicate PERSON
    # as much as a duplicate application -- deduplicating only the application
    # would still leave an orphan applicant row per retry.
    if idempotency_key:
        existing = resume_application(idempotency_key, resume_token, fingerprint)
        if existing:
            return existing

    raw_access_token, access_token_hash = decision_state.new_access_token()
    # The client's own secret, hashed. Not minted here: a credential the server
    # invents is one the client never receives if the response is lost, and the
    # applicant is then locked out of their own in-flight application.
    raw_resume_token = resume_token
    resume_hash = (decision_state.hash_access_token(resume_token)
                   if resume_token else None)


    # Both inserts in ONE transaction. Two concurrent retries with the same key
    # both miss the lookup above, and the partial unique index lets exactly one
    # application land -- but the loser has already inserted an applicant, and
    # outside a transaction that applicant would survive as an orphan. The whole
    # point of this change is that a retry leaves one person on record.
    try:
        with db.transaction() as cur:
            cur.execute(
                "INSERT INTO applicants (name, dob, ssn, ein, is_entity, email, phone, "
                "address, zip_code) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (
                    payload.get("name"), payload.get("dob"), payload.get("ssn"),
                    payload.get("ein"), payload.get("is_entity", False),
                    payload.get("email"), payload.get("phone"), payload.get("address"),
                    payload.get("zip_code"),
                ),
            )
            applicant_id = cur.fetchall()[0]["id"]
            cur.execute(
                "INSERT INTO applications (applicant_id, amount, term_months, purpose, income, "
                "employer, job_title, employment_years, access_token_hash, "
                "access_token_expires_at, idempotency_key, resume_token_hash, "
                "resume_token_expires_at, request_fingerprint) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "        now() + (%s || ' seconds')::interval, %s, %s, "
                "        now() + (%s || ' seconds')::interval, %s) "
                "ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL "
                "DO NOTHING RETURNING id",
                (
                    applicant_id, payload.get("amount"), payload.get("term_months", 36),
                    payload.get("purpose"), payload.get("income"),
                    payload.get("employer"), payload.get("job_title"),
                    payload.get("employment_years"),
                    access_token_hash, config.ACCESS_TOKEN_TTL_SECONDS, idempotency_key,
                    resume_hash, config.ACCESS_TOKEN_TTL_SECONDS, fingerprint,
                ),
            )
            inserted = cur.fetchall()
            if not inserted:
                # Lost the race with a concurrent retry carrying the same key.
                #
                # RAISE, so the transaction rolls back and takes the applicant with
                # it. ON CONFLICT DO NOTHING does not error -- it returns no row and
                # the transaction would COMMIT, leaving precisely the orphan
                # applicant this whole change exists to prevent. Detecting the
                # conflict is not enough; the insert that preceded it has to be
                # undone.
                raise _LostIdempotencyRace(idempotency_key)

    except _LostIdempotencyRace:
        # The other request won and its application is committed. Our own
        # applicant insert was rolled back with the transaction, so the retry
        # leaves exactly one person and one application on record.
        #
        # The loser cannot resume without the winner's resume token, and it does
        # not have one -- the winner's response carried it. Surfacing that as
        # ResumeNotAuthorized is correct: two concurrent first attempts with the
        # same key are indistinguishable from an attacker racing a real client,
        # and the safe answer to both is "prove it".
        existing = resume_application(idempotency_key, resume_token)
        if existing:
            return existing
        raise ResumeNotAuthorized()

    app_id = inserted[0]["id"]
    # Gap C: identifiers only. The intake payload carries SSN, DOB, address,
    # phone and email -- none of it belongs in an application log.
    log.info("application intake persisted app_id=%s applicant_id=%s", app_id, applicant_id)
    return app_id, raw_access_token, raw_resume_token


def board_to_servicing(app_id: int, applicant_name: str, principal: float,
                       annual_rate_pct: float, term_months: int,
                       *, regular_payment: float, regular_payment_count: int,
                       final_payment: float, schedule_version: str) -> int:
    """Direct cross-schema insert into the LSS tables. The 'seam'.

    The Model B schedule is copied from the offer, not recomputed here
    (db/migrations/0030). Recomputing is what drifts: servicing used to
    regenerate the schedule from principal/rate/term with whatever generator
    was deployed at read time, so a later rounding change silently altered the
    contractual terms of a loan somebody had already signed.

    Keyword-only and mandatory. Defaulting them to None would let a caller
    board a loan with no schedule by simply not knowing about the parameters --
    which is the state legacy loans are in, and which nothing new should be
    able to enter. loans_schedule_all_or_nothing would catch a partial write,
    but a silently complete-looking all-NULL write is exactly what it permits.
    """
    loan = db.query(
        # D19 contract: ONE rate column, and it says what it holds.
        #
        # `annual_rate_pct` is and always was the contractual NOTE RATE on this
        # path -- it was written to a column called `apr`, which under the
        # pre-change path held the DISCLOSED APR instead. 0038 added
        # `note_rate_pct` and this insert wrote both; 0039 dropped `apr`, so the
        # dual write is over and the name no longer lies about the figure.
        "INSERT INTO loans (app_id, applicant_name, principal, note_rate_pct, "
        "term_months, regular_payment, regular_payment_count, final_payment, "
        "schedule_version) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (app_id, applicant_name, principal, annual_rate_pct,
         term_months, regular_payment, regular_payment_count, final_payment,
         schedule_version),
    )
    loan_id = loan[0]["id"]
    # reach across into the servicing balances table directly
    db.query(
        "INSERT INTO balances (loan_id, balance) VALUES (%s, %s) "
        "ON CONFLICT (loan_id) DO NOTHING",
        (loan_id, float(principal)),   # money as float
    )
    log.info("boarded app_id=%s -> loan_id=%s (direct LSS insert)", app_id, loan_id)
    return loan_id


def board_to_servicing_tx(cur, app_id: int, applicant_name: str, principal: float,
                          annual_rate_pct: float, term_months: int,
                          *, regular_payment: float, regular_payment_count: int,
                          final_payment: float, schedule_version: str) -> int:
    """Same insert as board_to_servicing, but runs on a caller-supplied cursor
    so it lands in the SAME transaction as the caller's own statements (see
    routers/applications.py accept_offer + db.transaction()) -- a boarding
    failure then rolls back everything in that transaction together, instead
    of leaving a status flip committed with no loan behind it.

    The schedule copy is inside that same transaction for the same reason. A
    loan row committed without the terms it is to be billed on would be a
    funded contract whose payment amounts exist nowhere -- and the accept path
    has no second chance to write them, since it also marks the offer accepted
    and the offer is thereafter immutable.
    """
    cur.execute(
        # D19 contract: the transactional twin of the insert above, and it moved
        # in lockstep with it through both migrations. A column change applied to
        # only one of the two boarding paths leaves whichever loans went through
        # the other one carrying a NULL rate -- which 0039's gate 1 then refuses
        # to drop `apr` over, turning a missed edit here into a blocked release.
        "INSERT INTO loans (app_id, applicant_name, principal, note_rate_pct, "
        "term_months, regular_payment, regular_payment_count, final_payment, "
        "schedule_version) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (app_id, applicant_name, principal, annual_rate_pct,
         term_months, regular_payment, regular_payment_count, final_payment,
         schedule_version),
    )
    loan_id = cur.fetchone()["id"]
    cur.execute(
        "INSERT INTO balances (loan_id, balance) VALUES (%s, %s) "
        "ON CONFLICT (loan_id) DO NOTHING",
        (loan_id, float(principal)),
    )
    log.info("boarded app_id=%s -> loan_id=%s (direct LSS insert, in tx)", app_id, loan_id)
    return loan_id


# build_disclosure was removed: offer/disclosure build moved to disclosure-service, which
# now persists the offers row itself. The offers router calls it over HTTP (see clients.py).
