"""Proposing, queueing and resolving staff money movements.

This is the half of D8 that the signed principal made possible and the schema
made safe. Before it, `adjust-balance` and `waive-fee` moved money the moment one
authorised person called them. Now they write a **proposal**, nothing moves, and
a *different* authorised person has to approve it.

**Where each rule is enforced, and why there.**

  * *Identity* — `principal.require_staff_principal`, from a signature the caller
    cannot forge. Nothing here reads `X-User-*`. Every route that touches a
    proposal uses it: `adjust-balance`, `waive-fee`, the queue read and
    `resolve`.
  * *Role* — this module's own matrix, because the rule is not one bit.
    `PROPOSER_ROLES` admits csr, underwriter and admin — any staff member may
    ask. Approval is narrower and threshold-dependent:
    `APPROVER_ROLES_AT_OR_BELOW_THRESHOLD` (underwriter or admin) and
    `APPROVER_ROLES_ABOVE_THRESHOLD` (admin only), with csr never approving. A
    single csr/admin "may move money" bit cannot express that, which is why the
    money-mover guard is not what these routes call.

    *Historical:* this bullet read "*Identity and role* —
    `principal.require_money_principal`". That guard is the csr/admin money bit,
    and after the maker-checker cutover it is used by `late-fee` alone
    (`main.py`); pointing a reader at it from here sent them to the wrong guard
    for the control this module implements.
  * *Refuse-at-creation* — here and in `pending_movements`' constraints. A
    proposal the ledger could never execute must not reach an approver's queue
    (ADR 0011 §3b): an approver should never be shown a request the system was
    always going to reject.
  * *The transition itself* — `resolve_pending_movement`, in the database. The
    lock, the single transition, the self-approval refusal, the target
    revalidation and the entry insert all happen inside one function so that no
    caller can perform them in a different order.
  * *Policy* — configuration, read at boot, failing closed. The approved figures
    are cohort/demo values, not Lending Operations policy, and neither this
    module nor the schema states one of its own.

**What this does not do.** There is no notification, no delegation and no
out-of-office routing (spec 0002 §8), and a proposal whose loan later closes
becomes unapprovable rather than expiring. Those are refusals rather than
features, and the operational cost is accepted rather than hidden.
"""
from decimal import Decimal

from fastapi import HTTPException

from . import config, db
from .logging_config import get_logger

log = get_logger("maker_checker")

#: Who may resolve, by amount. csr appears nowhere: a CSR may raise a proposal
#: and may never approve one, at any amount (spec 0002 §3). The tiers are read
#: against the CONFIGURED threshold -- this table says who, configuration says
#: where the line falls.
APPROVER_ROLES_AT_OR_BELOW_THRESHOLD = frozenset({"underwriter", "admin"})
APPROVER_ROLES_ABOVE_THRESHOLD = frozenset({"admin"})

#: Any staff role may raise a proposal. Approved as REQ-VAL-14 option 2 for this
#: cohort/demo environment: there is no staff-to-loan assignment anywhere in this
#: schema, so scope is "any serviced, current loan" and that is recorded as a
#: reviewed limitation rather than modelled with invented data. Production
#: adoption requires Lending Operations to replace or approve it.
PROPOSER_ROLES = frozenset({"csr", "underwriter", "admin"})

#: What a proposal may be. Mirrors the ledger's vocabulary; the database refuses
#: anything else too, and this refusal exists so the caller gets a message naming
#: the permitted set rather than a constraint violation.
ENTRY_TYPES = frozenset({"adjustment", "fee_waived"})
COMPONENTS_BY_TYPE = {
    "adjustment": frozenset({"principal", "fees"}),
    "fee_waived": frozenset({"fees"}),
}


def _bad_request(message: str) -> HTTPException:
    return HTTPException(status_code=422, detail=message)


def propose(loan_id: int, *, component: str, amount, entry_type: str, reason: str,
            actor) -> dict:
    """Raise a proposal. Moves no money, by construction: this writes one row to
    `pending_movements` and touches neither `balances` nor `ledger_entries`.
    """
    if actor.role not in PROPOSER_ROLES:
        raise HTTPException(status_code=403, detail="staff only")

    if entry_type not in ENTRY_TYPES:
        raise _bad_request(
            f"entry_type must be one of {sorted(ENTRY_TYPES)}; a maker-checker "
            f"proposal covers staff-directed movements only"
        )
    permitted = COMPONENTS_BY_TYPE[entry_type]
    if component not in permitted:
        raise _bad_request(
            f"a {entry_type} may only move {sorted(permitted)}, not {component!r}"
        )

    try:
        delta = Decimal(str(amount))
    except Exception as exc:  # noqa: BLE001
        raise _bad_request(f"amount {amount!r} is not a number") from exc
    if not delta.is_finite():
        raise _bad_request("amount must be a finite number")
    if delta == 0:
        raise _bad_request(
            "a zero movement is not a correction; the ledger refuses it and an "
            "approver should not be asked to review it"
        )
    if entry_type == "fee_waived" and delta > 0:
        raise _bad_request(
            "a fee waiver reduces what the borrower owes, so its amount is negative"
        )

    # The configured cap, refused at creation for every role including admin.
    # REQ-VAL-6: this says what may be ASKED, which is a different question from
    # who may say yes.
    cap = config.max_delta()
    if abs(delta) > cap:
        raise _bad_request(
            f"amount {delta} exceeds the maximum a movement may be proposed for "
            f"({cap}); this limit applies to every role"
        )

    if not reason or not reason.strip():
        raise _bad_request(
            "a proposal needs a reason: without one an approver is being asked to "
            "authorise a number with no account of why"
        )

    # The target must be one the system can and should move. Checked here so the
    # requester gets a message, and re-checked inside the approval transaction
    # because state moves while a proposal sits in a queue.
    rows = db.query(
        "SELECT l.status AS status, b.loan_id AS serviced, "
        "       b.balance AS balance, COALESCE(b.past_due, 0) AS past_due "
        "FROM loans l LEFT JOIN balances b ON b.loan_id = l.id WHERE l.id = %s",
        (loan_id,),
    )
    if not rows:
        raise _bad_request(f"loan {loan_id} does not exist")
    if rows[0]["serviced"] is None:
        raise _bad_request(
            f"loan {loan_id} has no balances row, so an approved movement would "
            f"land nowhere"
        )
    status = rows[0]["status"]
    permitted_statuses = config.permitted_loan_statuses()
    if status not in permitted_statuses:
        raise _bad_request(
            f"loan {loan_id} is {status or 'unset'!r}; a movement may only be "
            f"proposed on {sorted(permitted_statuses)}"
        )

    # REQ-VAL-8 / AC-20: a movement may not take the targeted component below
    # zero, checked HERE as well as inside the approval transaction. Only the
    # approval check existed, so a waiver larger than the fees owed was accepted,
    # returned 202, and sat in the queue until an approver hit the failure --
    # the maker never learning their request was impossible. The approval check
    # stays because the balance moves while a proposal waits; this one exists so
    # the person who can fix the request is the one who is told.
    component_now = Decimal(str(
        rows[0]["past_due"] if component == "fees" else rows[0]["balance"]))
    if component_now + delta < 0:
        raise _bad_request(
            f"a movement of {delta} would take {component} below zero "
            f"({component_now} + {delta}); the ledger cannot hold a negative "
            f"{component}"
        )

    created = db.query(
        "INSERT INTO pending_movements "
        "(loan_id, component, amount, entry_type, reason, requested_by, requested_role) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id, requested_at",
        (loan_id, component, delta, entry_type, reason.strip(),
         int(actor.subject), actor.role),
    )[0]
    log.info("proposal %s raised loan_id=%s component=%s type=%s by subject=%s role=%s",
             created["id"], loan_id, component, entry_type, actor.subject, actor.role)
    return {
        "movement_id": created["id"],
        "loan_id": loan_id,
        "component": component,
        "amount": float(delta),
        "entry_type": entry_type,
        "status": "pending",
        "requested_by": actor.subject,
        "requested_role": actor.role,
        # Said explicitly in the response, because the caller's previous
        # experience of this endpoint was that the money had already moved.
        "balance_moved": False,
    }


#: Every column of a proposal, resolved half included. Named once because the
#: snapshot read below UNIONs two branches and they must agree exactly.
_COLUMNS = (
    "id, loan_id, component, amount, entry_type, reason, requested_by, "
    "requested_role, requested_at, resolution, resolved_by, resolved_role, "
    "resolved_at, ledger_entry_id, resolved_threshold"
)


def _shape(row: dict, *, resolved_half: bool) -> dict:
    """One row as the API returns it.

    `Decimal` and `datetime` do not survive JSON, and NULL is meaningful on
    `resolved_threshold` and `ledger_entry_id` -- an absent threshold is not a
    threshold of zero, and a missing entry is the account of a rejection having
    moved no money. Both stay null rather than being filled in.

    A PENDING row drops the resolved columns entirely rather than reporting them
    as null. They are not "not yet known" for a pending proposal, they do not
    apply, and the queue's response shape is one existing callers already have.
    """
    out = {
        "id": row["id"], "loan_id": row["loan_id"], "component": row["component"],
        "amount": float(row["amount"]), "entry_type": row["entry_type"],
        "reason": row["reason"], "requested_by": row["requested_by"],
        "requested_role": row["requested_role"],
        "requested_at": row["requested_at"].isoformat() if row["requested_at"] else None,
    }
    if not resolved_half:
        return out
    out.update({
        "resolution": row["resolution"],
        "resolved_by": row["resolved_by"],
        "resolved_role": row["resolved_role"],
        "resolved_at": row["resolved_at"].isoformat() if row["resolved_at"] else None,
        "ledger_entry_id": row["ledger_entry_id"],
        "resolved_threshold": (None if row["resolved_threshold"] is None
                               else float(row["resolved_threshold"])),
    })
    return out


def snapshot(pending_limit: int = 50, resolved_limit: int = 25) -> dict:
    """Both halves of the queue, read at ONE instant.

    The approvals page shows what is waiting and what was recently decided. Read
    as two requests those are two database snapshots, and a movement resolved by
    somebody else in between falls through the gap: read resolved-then-pending
    around a commit and the row is in NEITHER list; read them the other way and
    it is in BOTH. A movement vanishing from the screen is the exact defect the
    history panel was added to fix, so reproducing it here would be worse than
    not having the panel.

    Found in review (MC-RACE-01). The first version issued two requests inside
    one `Promise.all` and a comment claimed that made them simultaneous. It did
    not: `db.query` runs on an autocommit connection, so every call is its own
    snapshot, and two HTTP requests are two reads however they were started.

    ONE statement is the fix, and it is what makes the guarantee real rather
    than argued: a single `execute` sees a single snapshot, and
    `resolution IS NULL` / `IS NOT NULL` partition the table, so a proposal is
    in exactly one branch -- never both, never neither, whatever commits while
    the page is loading.

    Ordering is applied here rather than in SQL because a UNION has no defined
    order without an outer ORDER BY, and the two halves are ordered by different
    columns in different directions. Sorting the bounded result in Python is
    plainer than a CASE expression that has to reproduce both.
    """
    rows = db.query(
        f"(SELECT {_COLUMNS}, 0 AS bucket FROM pending_movements "
        "   WHERE resolution IS NULL ORDER BY requested_at ASC LIMIT %s) "
        "UNION ALL "
        f"(SELECT {_COLUMNS}, 1 AS bucket FROM pending_movements "
        "   WHERE resolution IS NOT NULL ORDER BY resolved_at DESC, id DESC LIMIT %s)",
        (pending_limit, resolved_limit),
    )
    pending = [r for r in rows if r["bucket"] == 0]
    done = [r for r in rows if r["bucket"] == 1]
    # Neither key can be NULL, so neither sort can raise on a None comparison:
    # `requested_at` is NOT NULL DEFAULT now(), and `resolution_complete` ties
    # `resolved_at` to `resolution` -- a row in the resolved half has one by
    # construction. Sorting on them directly says that, where a `is None` guard
    # would have implied the opposite while still failing on two null keys.
    #
    # Oldest first: the queue is work outstanding, and what has waited longest
    # is most overdue a decision.
    pending.sort(key=lambda r: (r["requested_at"], r["id"]))
    # Most recently resolved first: a recent-decisions panel whose newest row is
    # at the bottom is not one.
    done.sort(key=lambda r: (r["resolved_at"], r["id"]), reverse=True)
    return {
        "movements": [_shape(r, resolved_half=False) for r in pending],
        "resolved": [_shape(r, resolved_half=True) for r in done],
    }


def queue(limit: int = 50) -> list[dict]:
    """Unresolved proposals, oldest first. Visibility is not authority -- any
    staff role may read this, and none of them may approve from it alone."""
    rows = db.query(
        "SELECT id, loan_id, component, amount, entry_type, reason, requested_by, "
        "       requested_role, requested_at "
        "  FROM pending_movements WHERE resolution IS NULL "
        " ORDER BY requested_at ASC LIMIT %s", (limit,),
    )
    return [_shape(row, resolved_half=False) for row in rows]


def resolved(limit: int = 25) -> list[dict]:
    """Proposals that have been approved or rejected, most recently resolved first.

    The queue above answers "what is waiting on me". This answers the question
    an operator asked immediately afterwards and could not: "what happened to
    the one I raised?" A proposal left `queue()` the moment somebody resolved
    it, and nothing anywhere showed where it went -- so a rejection was
    indistinguishable from a proposal that had never been saved.

    Every column returned here was already written by `resolve_pending_movement`
    and has simply never been read by an API. This adds no table and records
    nothing new; it exposes the evidence the resolution already had to record in
    order to commit, because `resolution_complete` refuses a partial one.

    `ledger_entry_id` is the load-bearing field. An approval writes a ledger
    entry and a rejection does not, so the id being present or absent is the
    account of whether money actually moved -- not a status word that could
    drift from the ledger. A rejected row carries NULL here, and that is the
    answer rather than missing data.

    Bounded and ordered by `resolved_at DESC`: this is a recent-history panel,
    not an audit export. The immutable account of an approved movement is the
    ledger entry it points at.
    """
    rows = db.query(
        "SELECT id, loan_id, component, amount, entry_type, reason, requested_by, "
        "       requested_role, requested_at, resolution, resolved_by, resolved_role, "
        "       resolved_at, ledger_entry_id, resolved_threshold "
        "  FROM pending_movements WHERE resolution IS NOT NULL "
        " ORDER BY resolved_at DESC, id DESC LIMIT %s", (limit,),
    )
    return [_shape(row, resolved_half=True) for row in rows]


def _authority_for(amount: Decimal, role: str) -> None:
    """May this role resolve a movement of this size? Refuses, or returns."""
    threshold = config.admin_threshold()
    permitted = (APPROVER_ROLES_AT_OR_BELOW_THRESHOLD if abs(amount) <= threshold
                 else APPROVER_ROLES_ABOVE_THRESHOLD)
    if role not in permitted:
        # Names the threshold, not the approver set: telling a caller which
        # accounts could approve is an invitation to go and find one.
        raise HTTPException(
            status_code=403,
            detail=(f"a movement of {abs(amount)} is above the {threshold} "
                    f"threshold and needs a higher authority"
                    if abs(amount) > threshold
                    else f"role {role!r} may not approve a movement"),
        )


def resolve(movement_id: int, *, resolution: str, actor) -> dict:
    """Approve or reject, through the one database function that may.

    Everything that makes this safe happens inside `resolve_pending_movement`:
    the row is locked before its state is read, exactly one transition is
    permitted, self-approval is refused, the target is revalidated, and the entry
    is built from the locked row rather than from anything a caller sent.
    """
    if resolution not in ("approved", "rejected"):
        raise _bad_request("resolution must be 'approved' or 'rejected'")

    rows = db.query(
        "SELECT amount, requested_by, resolution FROM pending_movements WHERE id = %s",
        (movement_id,),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="no such movement")
    amount = Decimal(str(rows[0]["amount"]))

    # Authority is checked before the call, so a caller who may not approve gets
    # 403 rather than a database exception -- but it is NOT the guarantee. The
    # guarantees that matter (one transition, no self-approval, matching entry)
    # are enforced inside the function, where a second caller written later
    # cannot skip them.
    if resolution == "approved":
        _authority_for(amount, actor.role)
    elif actor.role not in APPROVER_ROLES_AT_OR_BELOW_THRESHOLD:
        # Rejecting is an authorisation decision too (spec 0002 §3): a CSR may
        # not dispose of a proposal by refusing it either.
        raise HTTPException(status_code=403, detail="role may not resolve a movement")

    threshold = config.admin_threshold()
    statuses = list(config.permitted_loan_statuses())
    try:
        result = db.query(
            "SELECT resolve_pending_movement(%s, %s, %s, %s, %s, %s) AS entry_id",
            (movement_id, int(actor.subject), actor.role, resolution,
             threshold, statuses),
        )
    except Exception as exc:  # noqa: BLE001
        message = str(exc).strip().splitlines()[0]
        log.warning("resolution refused movement=%s by subject=%s: %s",
                    movement_id, actor.subject, message)
        if "is already" in message:
            raise HTTPException(status_code=409, detail=message) from exc
        if "may not resolve it" in message:
            raise HTTPException(status_code=403, detail=message) from exc
        # A target that has moved on, or a limit that cannot be resolved: both
        # are refusals of this specific movement, not server faults.
        raise HTTPException(status_code=422, detail=message) from exc

    entry_id = result[0]["entry_id"]
    log.info("movement %s %s by subject=%s role=%s entry=%s",
             movement_id, resolution, actor.subject, actor.role, entry_id)
    return {
        "movement_id": movement_id,
        "resolution": resolution,
        "resolved_by": actor.subject,
        "resolved_role": actor.role,
        "threshold_applied": float(threshold),
        "ledger_entry_id": entry_id,
    }
