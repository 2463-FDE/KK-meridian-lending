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
from fastapi import APIRouter, Header, HTTPException

from .. import config, db, kyc
from ..logging_config import get_logger
from ..schemas import CipCheckIn, CipCheckOut

log = get_logger("kyc-api")
router = APIRouter(prefix="/kyc", tags=["kyc"])


@router.post("/check", response_model=CipCheckOut)
def kyc_check(
    body: CipCheckIn,
    x_internal_token: str | None = Header(None, alias="X-Internal-Token"),
):
    # Checked before anything else runs: a rejected caller must not reach
    # run_cip(), the INSERT, or the log line -- an unauthorized request should
    # leave no trace of its payload anywhere.
    if not config.INTERNAL_SERVICE_TOKEN or x_internal_token != config.INTERNAL_SERVICE_TOKEN:
        raise HTTPException(status_code=401, detail="not authorized")

    payload = body.model_dump()
    # Gap C (PR #6 review): this used to log the whole CIP payload -- name, DOB,
    # SSN and address -- at INFO on every identity check. Identifiers only now.
    # Not "redacted": the request body simply never reaches a log line, which is
    # the stronger guarantee. The verification RESULT below is the audit record
    # that legitimately needs keeping, and it holds booleans, not identity data.
    log.info(
        "POST /kyc/check application_id=%s applicant_id=%s",
        body.application_id, body.applicant_id,
    )
    cip = kyc.run_cip(payload)  # CIP only — no sanctions / UBO / monitoring (debt D11)

    # CIP "passes" if name + address verified. Entity applicants (no dob/ssn) still pass —
    # an LLC clears with no real person verified, and no UBO captured. (debt D11)
    cip_passed = bool(cip["name_verified"] and cip["address_verified"])
    status = "pass" if cip_passed else "fail"

    # persist the CIP result (still no sanctions/ubo columns to persist — debt preserved).
    # Raw psycopg2 write path, matching how origination wrote it.
    check_id = -1
    try:
        rows = db.query(
            "INSERT INTO kyc_checks (applicant_id, name_verified, dob_verified, "
            "address_verified, ssn_verified) VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (body.applicant_id, cip["name_verified"], cip["dob_verified"],
             cip["address_verified"], cip["ssn_verified"]),
        )
        check_id = rows[0]["id"] if rows else -1
    except Exception as e:  # noqa
        log.warning("could not persist kyc: %s", e)

    return CipCheckOut(
        check_id=check_id,
        application_id=body.application_id,
        status=status,
        cip_passed=cip_passed,
        sanctions_screened=False,  # no OFAC/sanctions screening (debt D11)
        ubo_captured=False,        # no beneficial-owner capture (debt D11)
        notes="CIP only; no sanctions/OFAC, no UBO, no ongoing monitoring, no SAR path.",
    )
