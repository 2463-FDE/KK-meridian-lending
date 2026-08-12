"""Application intake + the LOS->LSS 'boarding' seam.

A funded loan is boarded to servicing by a DIRECT INSERT into the servicing tables
(`loans`, `balances`) from this origination code path. No boarding API, no event,
no contract. (brownfield seam #1 — see docs/architecture.md, ADR 0002)
"""
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


class ResumeNotAuthorized(Exception):
    """The key named an application and the caller could not prove it may recover it.

    One exception for every failure -- missing token, wrong token, expired,
    already consumed. The caller must answer identically to all of them: telling
    them apart tells an attacker which one they achieved, and "expired" in
    particular confirms the application exists.
    """


def resume_application(idempotency_key: str, resume_token: str | None):
    """Recover an incomplete application. Requires the KEY and the TOKEN.

    Returns (app_id, raw_access_token, raw_resume_token), or None if the key names
    no application -- which is the ordinary first-attempt case and means "create
    one".

    Raises ResumeNotAuthorized if the key names an application and the token does
    not authorise it.

    **Why both.** 0036 let the key alone mint a fresh access token, on the
    reasoning -- written into the docstring -- that presenting the key proved
    ownership. It does not. A client-chosen key is not a secret: it travels in
    request bodies, proxy logs and client-side code, and it can be guessed.
    Anyone holding one could obtain a live access token and from there request a
    decision, read the application and trigger a credit pull. That is application
    takeover, through the path added to make retries safe.

    So the key IDENTIFIES and the token AUTHORISES:

    - the key says which application a retry belongs to;
    - the resume token, server-generated and 32 bytes of `secrets`, says the
      caller is the one that started it.

    The resume token is rotated on every successful recovery and the old one
    consumed, so a token captured from a log cannot be replayed after the
    legitimate client has used it.
    """
    rows = db.query(
        "SELECT id, resume_token_hash, resume_token_expires_at, "
        "       resume_token_consumed_at "
        "  FROM applications WHERE idempotency_key = %s",
        (idempotency_key,),
    )
    if not rows:
        return None

    row = rows[0]
    if not decision_state.resume_token_matches(
        row["resume_token_hash"], resume_token,
        row["resume_token_expires_at"], row["resume_token_consumed_at"],
    ):
        # Identifiers only, and never the token or which check failed.
        log.warning("refused a resume for app_id=%s -- resume token did not "
                    "authorise it", row["id"])
        raise ResumeNotAuthorized()

    app_id = row["id"]
    raw_access_token, access_token_hash = decision_state.new_access_token()
    raw_resume_token, resume_hash = decision_state.new_resume_token()
    db.query(
        "UPDATE applications SET access_token_hash = %s, "
        "       access_token_expires_at = now() + (%s || ' seconds')::interval, "
        "       access_token_consumed_at = NULL, "
        # Rotated, not reused: the presented token is spent by this recovery.
        "       resume_token_hash = %s, "
        "       resume_token_expires_at = now() + (%s || ' seconds')::interval, "
        "       resume_token_consumed_at = NULL "
        " WHERE id = %s",
        (access_token_hash, config.ACCESS_TOKEN_TTL_SECONDS,
         resume_hash, config.ACCESS_TOKEN_TTL_SECONDS, app_id),
    )
    log.info("intake resumed an existing application app_id=%s", app_id)
    return app_id, raw_access_token, raw_resume_token


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

    # A retry of a submission that already exists resumes it. Checked before the
    # applicant INSERT, because the duplicate this prevents is a duplicate PERSON
    # as much as a duplicate application -- deduplicating only the application
    # would still leave an orphan applicant row per retry.
    if idempotency_key:
        existing = resume_application(idempotency_key, resume_token)
        if existing:
            return existing

    raw_access_token, access_token_hash = decision_state.new_access_token()
    raw_resume_token, resume_hash = decision_state.new_resume_token()


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
                "resume_token_expires_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "        now() + (%s || ' seconds')::interval, %s, %s, "
                "        now() + (%s || ' seconds')::interval) "
                "ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL "
                "DO NOTHING RETURNING id",
                (
                    applicant_id, payload.get("amount"), payload.get("term_months", 36),
                    payload.get("purpose"), payload.get("income"),
                    payload.get("employer"), payload.get("job_title"),
                    payload.get("employment_years"),
                    access_token_hash, config.ACCESS_TOKEN_TTL_SECONDS, idempotency_key,
                    resume_hash, config.ACCESS_TOKEN_TTL_SECONDS,
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
        "INSERT INTO loans (app_id, applicant_name, principal, apr, term_months, "
        "regular_payment, regular_payment_count, final_payment, schedule_version) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (app_id, applicant_name, principal, annual_rate_pct, term_months,
         regular_payment, regular_payment_count, final_payment, schedule_version),
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
        "INSERT INTO loans (app_id, applicant_name, principal, apr, term_months, "
        "regular_payment, regular_payment_count, final_payment, schedule_version) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (app_id, applicant_name, principal, annual_rate_pct, term_months,
         regular_payment, regular_payment_count, final_payment, schedule_version),
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
