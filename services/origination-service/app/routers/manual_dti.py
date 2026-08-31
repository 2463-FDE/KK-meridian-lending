"""Manual DTI as EVIDENCE. RF-25's API half.

WHAT THE CLIENT DECIDED (2026-08-29, `docs/DEBT.md` RF-25). Staff may apply DTI
manually, but only on a REFERRED application, only as an underwriter or admin,
and only from approved SYNTHETIC source documents. The evidence required is
gross monthly income, monthly debt obligations, source-document references, the
calculation, staff identity, role, timestamp and reason -- a bare percentage is
explicitly insufficient.

And the constraint that shapes every route here: **a manual DTI is human-review
evidence. It must not approve, deny, override, mutate a decision, or trigger
model output.** So this module writes to `manual_dti_*` and to nothing else. It
does not touch `decisions`, `applications.status`, `manual_reviews` or
`decision_attempts`, and it starts no decision attempt.
`test_manual_dti_changes_no_decision_surface` reads all four before and after.

WHAT IS NOT TRUSTED FROM THE CALLER

  * `assessed_by` -- taken from `X-User-Id`, which the gateway sets from the
    session and strips from client input. The body has no such field, and
    `extra="forbid"` means a caller that sends one is refused rather than
    quietly ignored.
  * `assessed_role` -- taken from `X-User-Role`, same provenance, and then
    checked against `users.role` by `manual_dti_is_permitted` in the database.
    A route bug cannot store an authority the person does not hold.
  * the ratio -- never accepted and never computed here. The INSERT sends the
    two inputs and lets Postgres evaluate `round(obligations * 10000 / income)`,
    which is the same expression `manual_dti_is_reproducible` checks the row
    against. One definition, in one place: a Python copy of that formula could
    drift from the constraint, and the drift would surface as an opaque CHECK
    violation rather than as a wrong number somebody could see.

NO DOCUMENT CONTENT MOVES THROUGH HERE. A source document is a REFERENCE to a
row in the approved synthetic registry. There is no upload, no OCR, no
extraction, no embedding, no external call and no file storage -- which is the
scope the client authorised, and deliberately not one step past it.
"""
from decimal import Decimal

import psycopg2.errors
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .. import db, staff_auth
from ..logging_config import get_logger

log = get_logger("manual_dti")

router = APIRouter(prefix="/applications", tags=["manual-dti"])
registry_router = APIRouter(prefix="/manual-dti", tags=["manual-dti"])

#: One message for every refusal on these routes.
_FORBIDDEN = "manual DTI evidence is limited to underwriter and admin"


def _require_underwriting(x_user_role, x_internal_token) -> None:
    staff_auth.require_role(x_user_role, x_internal_token,
                            staff_auth.UNDERWRITING_ROLES, _FORBIDDEN)


def _caller_id(x_user_id: str | None) -> int:
    """The staff user id, from the gateway's header rather than from the body.

    A staff session always carries one (`gateway/app/main.py` sets `X-User-Id`
    from the session and strips whatever the client sent). Its absence means the
    caller is not coming through a session, so it is refused with the same
    message as every other failure here rather than with a 422 that would
    distinguish "you are not staff" from "you are staff but sent no id".
    """
    try:
        user_id = int(x_user_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=403, detail=_FORBIDDEN)
    if user_id <= 0:
        raise HTTPException(status_code=403, detail=_FORBIDDEN)
    return user_id


class SourceDocument(BaseModel):
    doc_ref: str
    kind: str
    label: str


class ManualDtiIn(BaseModel):
    """The evidence, and only the evidence.

    `extra="forbid"`: a caller sending `assessed_by`, `assessed_role`, `dti`,
    `dti_bp` or anything else gets a 422 naming the field. Pydantic's default is
    to drop unknown fields silently, which would let a caller believe it had
    supplied a ratio or an identity that this route had in fact ignored -- one
    request, two different beliefs about what was recorded.
    """
    model_config = ConfigDict(extra="forbid")

    gross_monthly_income: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    monthly_debt_obligations: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    #: At least one, because "source-document references" is required evidence.
    #: The database enforces the same thing at COMMIT
    #: (`manual_dti_needs_a_document`); this is the readable refusal.
    document_refs: list[str] = Field(min_length=1, max_length=10)
    reason: str = Field(min_length=1, max_length=2000)


class ManualDtiOut(BaseModel):
    id: int
    app_id: int
    assessed_by: int
    assessed_role: str
    gross_monthly_income: Decimal
    monthly_debt_obligations: Decimal
    #: Basis points, as stored. Not a float and not a percentage string: the
    #: caller can render it, but the recorded figure stays exact.
    dti_bp: int
    reason: str
    assessed_at: str
    documents: list[SourceDocument]


@registry_router.get("/source-documents", response_model=list[SourceDocument])
def list_source_documents(
    x_user_role: str | None = Header(default=None, alias="X-User-Role"),
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
):
    """The approved synthetic documents an assessment may cite.

    APPROVED ONLY. The registry also holds an unapproved row, on purpose, so the
    refusal path has something real to refuse -- but a staff member choosing
    documents should not be offered one that cannot be used. The database
    refuses it as well (`manual_dti_document_is_approved`); this route simply
    does not put it on screen.
    """
    _require_underwriting(x_user_role, x_internal_token)
    rows = db.query(
        "SELECT doc_ref, kind, label FROM manual_dti_source_documents "
        " WHERE approved AND is_synthetic ORDER BY doc_ref")
    return [SourceDocument(**r) for r in rows]


def _documents_for(cur, assessment_ids: list[int]) -> dict[int, list[SourceDocument]]:
    if not assessment_ids:
        return {}
    cur.execute(
        "SELECT l.assessment_id, d.doc_ref, d.kind, d.label "
        "  FROM manual_dti_assessment_documents l "
        "  JOIN manual_dti_source_documents d ON d.id = l.document_id "
        " WHERE l.assessment_id = ANY(%s) "
        " ORDER BY d.doc_ref", (assessment_ids,))
    out: dict[int, list[SourceDocument]] = {}
    for row in cur.fetchall():
        out.setdefault(row["assessment_id"], []).append(
            SourceDocument(doc_ref=row["doc_ref"], kind=row["kind"], label=row["label"]))
    return out


@router.get("/{app_id}/manual-dti", response_model=list[ManualDtiOut])
def list_manual_dti(
    app_id: int,
    x_user_role: str | None = Header(default=None, alias="X-User-Role"),
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
):
    """Every assessment recorded for this application, newest first.

    A list rather than one row, because the register is append-only and a
    referred application may legitimately be assessed more than once -- a second
    reviewer, or the same one after new documents. Showing only the latest would
    hide the earlier evidence a later assessment may have been raised to correct.
    """
    _require_underwriting(x_user_role, x_internal_token)
    with db.transaction() as cur:
        cur.execute(
            "SELECT id, app_id, assessed_by, assessed_role, gross_monthly_income, "
            "       monthly_debt_obligations, dti_bp, reason, assessed_at "
            "  FROM manual_dti_assessments WHERE app_id = %s "
            " ORDER BY assessed_at DESC, id DESC", (app_id,))
        rows = cur.fetchall()
        docs = _documents_for(cur, [r["id"] for r in rows])
    return [
        ManualDtiOut(**{**r, "assessed_at": r["assessed_at"].isoformat(),
                        "documents": docs.get(r["id"], [])})
        for r in rows
    ]


@router.post("/{app_id}/manual-dti", response_model=ManualDtiOut, status_code=201)
def record_manual_dti(
    app_id: int,
    body: ManualDtiIn,
    x_user_role: str | None = Header(default=None, alias="X-User-Role"),
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
):
    """Record one manual DTI assessment against a referred application.

    NOT IDEMPOTENT, and deliberately not pretending to be. A repeated POST
    records a SECOND assessment rather than returning the first: the register is
    append-only and a second assessment on the same application is a legitimate
    thing for a second reviewer to record, so there is no basis in the schema or
    in the client's rule for collapsing the two. Nothing here is a money movement
    or a decision, so a duplicate is inert -- one more piece of evidence,
    attributed and timestamped, not a repeated effect. Making a retry silent
    would need an idempotency key, which needs a uniqueness rule the client has
    not given; inventing one would decide policy this PR has no authority over.

    ONE TRANSACTION. The assessment and its document links are written together,
    so a failure at any point leaves no assessment with no evidence behind it.
    The at-least-one-document trigger is DEFERRABLE INITIALLY DEFERRED and fires
    at COMMIT, which is what makes writing the row before its links safe.

    EVERY REFUSAL COMES FROM THE DATABASE, not from a pre-check here. Whether the
    application is referred, whether the role claimed is the role held, whether
    the account is active and whether a document is approved are all checked by
    triggers that hold row locks while they check. A read-then-write in Python
    would be the same check without the lock -- true when it ran and not
    necessarily true at COMMIT.
    """
    _require_underwriting(x_user_role, x_internal_token)
    user_id = _caller_id(x_user_id)

    # Whitespace-only, not merely empty. `Field(min_length=1)` accepts a single
    # space and the schema's CHECK strips every whitespace character before
    # measuring, so without this the request reached Postgres and came back as an
    # unhandled CheckViolation -- a 500 for what is plainly a bad request. Found
    # by the blank-reason cases parametrised over a tab and a newline.
    if not body.reason.strip():
        raise HTTPException(status_code=422, detail="reason is blank")

    refs = [r.strip() for r in body.document_refs]
    if any(not r for r in refs):
        raise HTTPException(status_code=422, detail="a document reference is blank")
    if len(set(refs)) != len(refs):
        raise HTTPException(status_code=422,
                            detail="the same document is cited more than once")

    try:
        with db.transaction() as cur:
            # The ratio is computed BY POSTGRES, with the same expression the
            # CHECK constraint verifies the row against. Sending a Python-side
            # figure would create a second definition of the calculation.
            cur.execute(
                "INSERT INTO manual_dti_assessments "
                "  (app_id, assessed_by, assessed_role, gross_monthly_income, "
                "   monthly_debt_obligations, dti_bp, reason) "
                "VALUES (%(app)s, %(uid)s, %(role)s, %(income)s, %(debt)s, "
                "        round(%(debt)s::numeric * 10000 / %(income)s::numeric), "
                "        %(reason)s) "
                "RETURNING id, app_id, assessed_by, assessed_role, "
                "          gross_monthly_income, monthly_debt_obligations, "
                "          dti_bp, reason, assessed_at",
                {"app": app_id, "uid": user_id, "role": x_user_role,
                 "income": body.gross_monthly_income,
                 "debt": body.monthly_debt_obligations,
                 "reason": body.reason})
            row = cur.fetchone()

            # Resolved inside the same transaction as the citation. A ref looked
            # up earlier and inserted later is a ref that could have been removed
            # in between; the approval trigger locks the registry row it is
            # handed, which is only worth anything if this is the read that
            # produced it.
            cur.execute(
                "SELECT id, doc_ref, kind, label FROM manual_dti_source_documents "
                " WHERE doc_ref = ANY(%s)", (refs,))
            found = {d["doc_ref"]: d for d in cur.fetchall()}
            missing = [r for r in refs if r not in found]
            if missing:
                raise HTTPException(
                    status_code=422,
                    detail="unknown source document reference(s): " + ", ".join(missing))

            for ref in refs:
                cur.execute(
                    "INSERT INTO manual_dti_assessment_documents "
                    "  (assessment_id, document_id) VALUES (%s, %s)",
                    (row["id"], found[ref]["id"]))

            documents = [SourceDocument(doc_ref=found[r]["doc_ref"],
                                        kind=found[r]["kind"],
                                        label=found[r]["label"])
                         for r in sorted(refs)]
    except HTTPException:
        raise
    except psycopg2.errors.RaiseException as exc:
        # Every trigger here raises a message written to be read by the person
        # who tripped it -- "application 41 is approve, not referred", "user 3
        # holds role csr, but the assessment claims underwriter". Passing it
        # through is the point: a generic 409 would hide which rule refused.
        #
        # These messages name an application id, a user id and a role. They do
        # not carry applicant PII, and the two monetary inputs never appear in
        # them, so there is nothing here to redact.
        detail = str(exc).split("\n", 1)[0].strip()
        log.info("manual DTI refused for app_id=%s: %s", app_id, detail)
        raise HTTPException(status_code=409, detail=detail)
    except psycopg2.errors.CheckViolation as exc:
        # A constraint the route did not pre-empt. Every one of them is a
        # statement about the caller's own numbers -- the reproducibility CHECK,
        # the positive-income CHECK, the non-blank reason -- so this is a 422
        # rather than a 500, and the constraint is NAMED: `diagnostic` gives the
        # reader something to look up instead of "invalid input".
        name = getattr(exc.diag, "constraint_name", None) or "a schema constraint"
        log.info("manual DTI refused for app_id=%s: %s", app_id, name)
        raise HTTPException(
            status_code=422,
            detail=f"the assessment violates {name}")
    except psycopg2.errors.ForeignKeyViolation:
        # An app_id or user id that does not exist. Deliberately the same 409
        # shape, with a message that does not confirm which of the two it was.
        raise HTTPException(
            status_code=409,
            detail="the application or the assessing user does not exist")

    log.info("manual DTI recorded id=%s app_id=%s role=%s documents=%d",
             row["id"], app_id, x_user_role, len(documents))
    return ManualDtiOut(**{**row, "assessed_at": row["assessed_at"].isoformat(),
                           "documents": documents})
