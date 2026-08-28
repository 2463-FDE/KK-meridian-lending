"""KYC API: CIP-only verification + persistence.

CIP only — no sanctions / OFAC screening, no beneficial-owner (UBO) capture, no ongoing
monitoring, no SAR path (debt D11). The kyc_checks write below mirrors how origination
persisted the row: raw psycopg2 INSERT, only the four CIP boolean columns (there are no
sanctions/ubo columns to persist — debt preserved).

Authorization: this endpoint had none at all, and kyc-service was the one service
that was *both* host-published (`docker-compose.yml` mapped 8003:8003) and
tokenless — so `POST localhost:8003/kyc/check` reached the handler below with no
authentication, and wrote a kyc_checks row for any applicant_id the caller named.
That is CIP evidence in the record BSA/AML relies on, fabricable by anyone who
could reach the host. PR #6 closed the identical bypass for decision, disclosure,
payment and origination-service; kyc-service was left out of the fix *and* out of
`gateway/tests/test_decision_service_not_host_published.py`, which is why nothing
caught it for two months. ARCHITECTURE.md recorded it and named PR #8 as its
owner; PR #8 shipped tokenization instead. Both halves are closed here: the host
port is gone, and this check is the defense in depth behind it.
"""
import secrets
from fastapi import APIRouter, Header, HTTPException

from .. import config, db, kyc
from ..logging_config import get_logger
from ..schemas import CipCheckIn, CipCheckOut

log = get_logger("kyc-api")
router = APIRouter(prefix="/kyc", tags=["kyc"])


def _require_matching_application(application_id: int, applicant_id: int) -> dict:
    """The application must exist and belong to this applicant. Returns the applicant.

    Review round 9 (high): this returned nothing, and the handler then ran CIP
    over the REQUEST BODY. So the pairing was verified and the evidence was not:
    anyone holding the shared internal token could post fabricated identity
    attributes against a real application and this service would persist a
    passing compliance record for them. The linkage check proved the IDs were
    real while the thing being recorded came from the caller -- which is the
    forgery it was written to stop, moved one field along.

    It returns the stored applicant now, and the verdict is computed from those
    columns. Identity evidence has to be about what the system knows, not about
    what the request says.

    Deliberately fails CLOSED on a database error. The alternative -- treating an
    unreadable applications table as "cannot disprove the link, so allow it" --
    would turn any transient read failure into an open door for exactly the
    forgery this check exists to stop, and a caller who can cause read failures
    can choose when to try.

    404 rather than 403 for a mismatch: the caller is asserting a relationship
    that does not exist, and distinguishing "no such application" from "not your
    application" would make this an existence oracle over applicants.
    """
    try:
        rows = db.query(
            "SELECT p.name, p.dob, p.ssn, p.address, "
            "       COALESCE(p.is_entity, false) AS is_entity "
            "  FROM applications a "
            "  JOIN applicants p ON p.id = a.applicant_id "
            " WHERE a.id = %s AND a.applicant_id = %s LIMIT 1",
            (application_id, applicant_id),
        )
    except Exception as e:  # noqa
        log.error(
            "could not verify application/applicant linkage application_id=%s applicant_id=%s: %s",
            application_id, applicant_id, e,
        )
        raise HTTPException(status_code=503, detail="could not verify the application")
    if not rows:
        log.warning(
            "refused a CIP check for an unlinked pair application_id=%s applicant_id=%s",
            application_id, applicant_id,
        )
        raise HTTPException(
            status_code=404,
            detail="no such application for this applicant",
        )
    return rows[0]


@router.post("/check", response_model=CipCheckOut)
def kyc_check(
    body: CipCheckIn,
    x_internal_token: str | None = Header(None, alias="X-Internal-Token"),
):
    # Checked before anything else runs: a rejected caller must not reach
    # run_cip(), the INSERT, or the log line -- an unauthorized request should
    # leave no trace of its payload anywhere.
    if (not config.INTERNAL_SERVICE_TOKEN or not x_internal_token
            or not secrets.compare_digest(x_internal_token.encode("utf-8"),
                                      config.INTERNAL_SERVICE_TOKEN.encode("utf-8"))):
        raise HTTPException(status_code=401, detail="not authorized")

    # Gap C (PR #6 review): this used to log the whole CIP payload -- name, DOB,
    # SSN and address -- at INFO on every identity check. Identifiers only now.
    # Not "redacted": the request body simply never reaches a log line, which is
    # the stronger guarantee. The verification RESULT below is the audit record
    # that legitimately needs keeping, and it holds booleans, not identity data.
    log.info(
        "POST /kyc/check application_id=%s applicant_id=%s",
        body.application_id, body.applicant_id,
    )
    # Review finding (high): the caller used to name any applicant_id it liked
    # and this service persisted it. Holding the internal token proved the
    # request came from the gateway or origination, and nothing more -- so a
    # caller who could reach a route that attaches the token could mint CIP
    # evidence against a stranger's applicant_id.
    #
    # The pairing is verified against origination's own record instead of being
    # trusted: the application must exist and must actually belong to this
    # applicant. That makes the body a claim about existing state rather than an
    # instruction to create it.
    applicant = _require_matching_application(body.application_id, body.applicant_id)

    # CIP runs over the STORED applicant, never the request body. The body's
    # identity fields are accepted for compatibility and deliberately unused:
    # a caller holding the internal token could otherwise post any non-empty
    # strings and have this service persist a passing compliance record from
    # them, against a real application. See _require_matching_application.
    #
    # CIP only -- no sanctions / UBO / monitoring (debt D11).
    cip = kyc.run_cip({
        "name": applicant["name"],
        # These arrive typed off the row (DATE, TEXT). run_cip asks only whether
        # a field is present, so str() is enough and str(None) must not become
        # the string "None" -- hence the explicit conditional.
        "dob": str(applicant["dob"]) if applicant["dob"] is not None else None,
        "ssn": applicant["ssn"],
        "address": applicant["address"],
    })

    # Review round 6 (high): this was `name_verified and address_verified` for
    # EVERYONE, which was survivable only because the schema forced every
    # individual to send a dob and an ssn -- a 422 was doing the work the
    # predicate looked like it was doing. Making those fields optional (round 5,
    # so entity applicants could be accepted at all) removed that accident, and
    # a natural person with a name and an address but neither a date of birth
    # nor an SSN got a durable `pass` row. Everything downstream trusts this one
    # boolean, so that person could reach decisioning and approval as verified.
    #
    # The rule is applicant-type aware now. An individual is identified by name,
    # address, date of birth and SSN -- the four factors CIP exists to collect,
    # and the same four the intake form already requires.
    # From the applicants row, not `body.entity_type`. The entity branch is the
    # WEAKER rule -- an LLC clears on name and address alone (D11) -- so letting
    # the caller assert it would let a natural person be graded as a company by
    # setting one field.
    is_entity = bool(applicant["is_entity"])
    if is_entity:
        # Unchanged, and still debt D11: an LLC clears on name and address with
        # no natural person verified and no beneficial owner captured. That gap
        # is deliberate and tracked; it is not a reason to extend it to people.
        cip_passed = bool(cip["name_verified"] and cip["address_verified"])
    else:
        cip_passed = bool(
            cip["name_verified"] and cip["address_verified"]
            and cip["dob_verified"] and cip["ssn_verified"]
        )
    status = "pass" if cip_passed else "fail"

    # persist the CIP result (still no sanctions/ubo columns to persist — debt preserved).
    # Raw psycopg2 write path, matching how origination wrote it.
    # Review finding (high): this used to catch every INSERT failure, log a
    # warning, and still return 200 with check_id=-1 -- a "verified" answer with
    # no compliance record behind it. Origination trusted that response and told
    # the applicant they were submitted; the decision gate then blocked them
    # later, correctly but inexplicably, because the row the gate looks for was
    # never written. A DB permission problem, schema drift or a transient write
    # failure therefore produced a false successful intake and a dead-end
    # application.
    #
    # A persistence failure is a failed dependency, and it is reported as one.
    # There is no longer any path that returns a CIP result which was not
    # durably recorded, so check_id on a 200 is always a real row.
    try:
        rows = db.query(
            "INSERT INTO kyc_checks (applicant_id, application_id, name_verified, "
            "dob_verified, address_verified, ssn_verified, cip_passed) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (body.applicant_id, body.application_id,
             cip["name_verified"], cip["dob_verified"],
             cip["address_verified"], cip["ssn_verified"],
             # The verdict is persisted with the factors it came from, so a
             # reader never has to reapply an applicant-type-aware rule that
             # lives in this service. (db/migrations/0033)
             cip_passed),
        )
    except Exception as e:  # noqa
        log.error(
            "could not persist kyc result application_id=%s applicant_id=%s: %s",
            body.application_id, body.applicant_id, e,
        )
        raise HTTPException(
            status_code=503,
            detail="identity verification could not be recorded",
        )
    if not rows:
        # RETURNING produced nothing: the row is not there, whatever the reason.
        log.error(
            "kyc insert returned no row application_id=%s applicant_id=%s",
            body.application_id, body.applicant_id,
        )
        raise HTTPException(
            status_code=503,
            detail="identity verification could not be recorded",
        )
    check_id = rows[0]["id"]

    return CipCheckOut(
        check_id=check_id,
        application_id=body.application_id,
        status=status,
        cip_passed=cip_passed,
        # The recorded factors, not a reconstruction of them.
        name_verified=cip["name_verified"],
        dob_verified=cip["dob_verified"],
        address_verified=cip["address_verified"],
        ssn_verified=cip["ssn_verified"],
        sanctions_screened=False,  # no OFAC/sanctions screening (debt D11)
        ubo_captured=False,        # no beneficial-owner capture (debt D11)
        notes="CIP only; no sanctions/OFAC, no UBO, no ongoing monitoring, no SAR path.",
    )
