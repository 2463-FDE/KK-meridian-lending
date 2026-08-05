"""Application intake + the LOS->LSS 'boarding' seam.

A funded loan is boarded to servicing by a DIRECT INSERT into the servicing tables
(`loans`, `balances`) from this origination code path. No boarding API, no event,
no contract. (brownfield seam #1 — see docs/architecture.md, ADR 0002)
"""
from .logging_config import get_logger
from . import config, db, decision_state

log = get_logger("intake")


def create_application(payload: dict) -> tuple[int, str]:
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
    applicant = db.query(
        "INSERT INTO applicants (name, dob, ssn, ein, is_entity, email, phone, address, zip_code) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (
            payload.get("name"), payload.get("dob"), payload.get("ssn"),
            payload.get("ein"), payload.get("is_entity", False),
            payload.get("email"), payload.get("phone"), payload.get("address"),
            payload.get("zip_code"),
        ),
    )
    applicant_id = applicant[0]["id"]
    raw_access_token, access_token_hash = decision_state.new_access_token()
    app_row = db.query(
        "INSERT INTO applications (applicant_id, amount, term_months, purpose, income, "
        "employer, job_title, employment_years, access_token_hash, access_token_expires_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, "
        "        now() + (%s || ' seconds')::interval) RETURNING id",
        (
            applicant_id, payload.get("amount"), payload.get("term_months", 36),
            payload.get("purpose"), payload.get("income"),
            payload.get("employer"), payload.get("job_title"), payload.get("employment_years"),
            access_token_hash, config.ACCESS_TOKEN_TTL_SECONDS,
        ),
    )
    app_id = app_row[0]["id"]
    # Gap C: identifiers only. The intake payload carries SSN, DOB, address,
    # phone and email -- none of it belongs in an application log.
    log.info("application intake persisted app_id=%s applicant_id=%s", app_id, applicant_id)
    return app_id, raw_access_token


def board_to_servicing(app_id: int, applicant_name: str, principal: float,
                       annual_rate_pct: float, term_months: int) -> int:
    """Direct cross-schema insert into the LSS tables. The 'seam'."""
    loan = db.query(
        "INSERT INTO loans (app_id, applicant_name, principal, apr, term_months) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (app_id, applicant_name, principal, annual_rate_pct, term_months),
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
                          annual_rate_pct: float, term_months: int) -> int:
    """Same insert as board_to_servicing, but runs on a caller-supplied cursor
    so it lands in the SAME transaction as the caller's own statements (see
    routers/applications.py accept_offer + db.transaction()) -- a boarding
    failure then rolls back everything in that transaction together, instead
    of leaving a status flip committed with no loan behind it.
    """
    cur.execute(
        "INSERT INTO loans (app_id, applicant_name, principal, apr, term_months) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (app_id, applicant_name, principal, annual_rate_pct, term_months),
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
