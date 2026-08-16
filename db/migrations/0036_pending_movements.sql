-- 0036 -- ADR 0011 step 1: the maker-checker schema. NO application writers.
--
-- This migration creates the table a staff money movement is proposed into, the
-- link that ties an approved proposal to the one ledger entry it authorised, and
-- the triggers that make the whole thing hold without trusting the application.
-- It does NOT create the proposal or approval endpoints, and it does NOT create
-- `resolve_pending_movement` -- ADR 0011 assigns that body to the cutover step,
-- where it can be executed and tested against a real approval.
--
-- **D8 is not closed by this file.** Nothing writes a `pending_movements` row
-- yet, so `adjust-balance` and `waive-fee` still move money on one person's
-- say-so. What lands here is the shape the control needs, and the guarantees
-- that shape enforces on its own -- which is worth having early precisely
-- because the cutover then has nothing to get subtly wrong in SQL while it is
-- also getting the API right.
--
-- Invariants this migration is responsible for (ADR 0011's binding list):
--   1. a proposal's substance is immutable      -- pending_movements_one_way
--   2. exactly one terminal transition          -- pending_movements_one_way
--   3. no self-approval                         -- no_self_approval + the entry trigger
--   4. an approval yields exactly one entry,
--      a rejection yields none                  -- pending_movements_resolution_complete
--   5. proposals are retained, never deleted    -- pending_movements_no_delete
--   6. the entry's actor is the APPROVER        -- ledger_entries_match_proposal
--   7. an approved entry matches its proposal   -- ledger_entries_match_proposal
--
-- The two configured limits -- MAKER_CHECKER_ADMIN_THRESHOLD and
-- MAKER_CHECKER_MAX_DELTA -- are deliberately ABSENT from this schema. They are
-- human-approved configuration, not database facts: baking either into a CHECK
-- would make a policy change a migration, and would hard-code a cohort/demo
-- figure into the shape of the data. They are enforced at the API boundary in
-- the cutover step, read from the environment, failing closed when unset.

BEGIN;

-- Refuse to proceed if the ledger already holds a human-authorised entry that no
-- proposal could explain. `approved_entries_have_a_proposal` below would fail on
-- such a row anyway -- this reports WHICH rows and why, instead of a constraint
-- violation naming only the constraint.
--
-- Expected to find nothing: no writer emits 'adjustment' or 'fee_waived' today
-- (the compatibility bridge writes 'legacy_direct_write', payments write
-- 'payment'). Checked rather than assumed, because "no writer does that" is the
-- kind of claim this repository has repeatedly found to be one grep out of date.
DO $$
DECLARE
    orphaned INTEGER;
BEGIN
    SELECT count(*) INTO orphaned
      FROM ledger_entries
     WHERE entry_type IN ('adjustment', 'fee_waived');

    IF orphaned > 0 THEN
        RAISE EXCEPTION
            '0036: % ledger entries are already typed adjustment/fee_waived and '
            'cannot name an approving proposal, because none exist yet. Reclassify '
            'or back-fill them before applying this migration -- adding the '
            'constraint without deciding what they were would either fail opaquely '
            'or retro-label unapproved movements as approved.', orphaned;
    END IF;
END $$;


CREATE TABLE IF NOT EXISTS pending_movements (
    id            BIGSERIAL   PRIMARY KEY,
    loan_id       INTEGER     NOT NULL REFERENCES loans(id),
    component     TEXT        NOT NULL,
    amount        NUMERIC(14,2) NOT NULL,
    entry_type    TEXT        NOT NULL CHECK (entry_type IN ('adjustment','fee_waived')),
    -- Required: a proposal without a reason is unreviewable. The approver is
    -- otherwise being asked to authorise a number with no account of why.
    reason        TEXT        NOT NULL,
    requested_by  INTEGER     NOT NULL,
    -- The role is stored beside the id on both sides because the ledger's actor
    -- constraint needs both, and the entry's actor is written FROM this row
    -- rather than from the caller. A proposal recording only ids would leave the
    -- role to whoever inserts the entry.
    requested_role TEXT       NOT NULL,
    requested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Terminal state, written once. This table is NOT append-only: resolving a
    -- proposal is the one legitimate mutation in the design, confined here
    -- precisely so the ledger's own guarantee stays absolute.
    resolution    TEXT        CHECK (resolution IN ('approved','rejected')),
    resolved_by   INTEGER,
    resolved_role TEXT,
    resolved_at   TIMESTAMPTZ,
    ledger_entry_id BIGINT    REFERENCES ledger_entries(id),

    -- The threshold this resolution was judged against, recorded at resolution
    -- time (spec 0002 AC-22). A history of approvals is unreadable if the bar
    -- moved and nothing says when -- the same rule `reconciliation_runs`
    -- follows for its own threshold. NULL until resolved; the cutover writes it
    -- from configuration, and this schema states no figure of its own.
    resolved_threshold NUMERIC(14,2),

    CONSTRAINT no_self_approval CHECK (resolved_by IS NULL OR resolved_by <> requested_by),
    -- PM-THRESHOLD-001: `resolved_threshold` is part of a complete resolution.
    -- It was nullable and absent from this constraint, so a resolution could
    -- commit without recording the bar it was judged against -- which is the one
    -- thing spec 0002 AC-22 says the column exists to preserve, and a history of
    -- approvals is unreadable if the bar moved and nothing says when.
    CONSTRAINT resolution_complete CHECK (
        (resolution IS NULL
            AND resolved_by IS NULL AND resolved_role IS NULL AND resolved_at IS NULL
            AND resolved_threshold IS NULL)
     OR (resolution IS NOT NULL
            AND resolved_by IS NOT NULL AND resolved_role IS NOT NULL
            AND resolved_at IS NOT NULL AND resolved_threshold IS NOT NULL)
    ),
    -- PM-REASON-001: `NOT NULL` admits '' and '   '. The reason is the evidence
    -- D8 names as missing, and an empty one is the same absence wearing a value.
    -- Matched on a non-space character rather than btrim() so tabs and newlines
    -- are covered too.
    CONSTRAINT pending_reason_not_blank CHECK (reason ~ '[^[:space:]]'),
    -- PM-TERMS-001: a proposal the ledger can never execute must not reach an
    -- approver's queue. These mirror ADR 0010's executable constraints, so the
    -- refusal happens at creation with a message the requester can act on,
    -- rather than at approval -- after a second person has reviewed and accepted
    -- a request the system was always going to reject.
    CONSTRAINT pending_amount_nonzero CHECK (amount <> 0),
    CONSTRAINT pending_fee_waiver_reduces CHECK (
        entry_type <> 'fee_waived' OR amount < 0
    ),
    -- "an approval produces exactly one ledger entry, a rejection produces none"
    -- is NOT a CHECK. It cannot be: the entry is inserted after the row is marked
    -- approved, so an immediate CHECK would fail mid-transaction on a state that
    -- is legitimately transient. It is enforced at COMMIT instead, by the
    -- deferred constraint trigger below -- PostgreSQL CHECK constraints cannot be
    -- DEFERRABLE, which is exactly why that is a constraint trigger.

    -- Same component vocabulary as the ledger. Without this a proposal could
    -- name a component the ledger cannot hold, and the mismatch would surface
    -- only at approval -- after a human had reviewed and accepted it.
    -- The ledger holds an `adjustment` against principal or fees only
    -- (`ledger_type_matches_component`, ADR 0010). `interest` is in the
    -- vocabulary for the components the ledger CAN hold generally, and an
    -- interest adjustment is not one of them -- so proposing one would queue a
    -- movement that fails at execution. Spec 0002 REQ-VAL-2 lists all three; the
    -- narrowing to what is executable is recorded there rather than left as a
    -- silent difference between the document and the database.
    CONSTRAINT pending_component CHECK (
        (entry_type = 'adjustment'  AND component IN ('principal','fees'))
     OR (entry_type = 'fee_waived'  AND component = 'fees')
    ),

    -- A fee waiver moves fees. ADR 0010 fixes `fee_waived` to the `fees`
    -- component, so a proposal naming another describes a movement the ledger
    -- cannot represent, and it would fail at the entry insert AFTER a second
    -- person approved it. `adjustment` is deliberately open to all three:
    -- correcting principal, interest or fees are all real corrections.
    CONSTRAINT pending_fee_waiver_is_fees CHECK (
        entry_type <> 'fee_waived' OR component = 'fees'
    )
);

-- The queue a reviewer reads: unresolved proposals, oldest first.
CREATE INDEX IF NOT EXISTS pending_movements_queue
    ON pending_movements (requested_at) WHERE resolution IS NULL;

CREATE INDEX IF NOT EXISTS pending_movements_loan
    ON pending_movements (loan_id, requested_at);


-- --- the link to the ledger ---------------------------------------------------

ALTER TABLE ledger_entries
    ADD COLUMN IF NOT EXISTS pending_movement_id BIGINT;

-- Existence checked against THIS table, not against the constraint name alone.
-- `pg_constraint` is global: a name-only lookup finds a constraint of the same
-- name in any other schema, so on a database that already holds one -- a parity
-- fixture, a test schema, a staging copy -- these blocks silently skipped, and
-- the UNIQUE guarantee that stops one approval yielding two ledger entries was
-- never created. Caught by `test_one_approval_cannot_yield_two_entries`, which
-- inserted a second entry successfully.
--
-- `conrelid = 'ledger_entries'::regclass` resolves through the search_path, so
-- it asks about the table this migration is actually altering.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conrelid = 'ledger_entries'::regclass
                      AND conname = 'ledger_entries_pending_movement_key') THEN
        -- UNIQUE: one approval can never yield two entries.
        ALTER TABLE ledger_entries
            ADD CONSTRAINT ledger_entries_pending_movement_key UNIQUE (pending_movement_id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conrelid = 'ledger_entries'::regclass
                      AND conname = 'ledger_entries_pending_movement_fk') THEN
        ALTER TABLE ledger_entries
            ADD CONSTRAINT ledger_entries_pending_movement_fk
            FOREIGN KEY (pending_movement_id) REFERENCES pending_movements(id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conrelid = 'ledger_entries'::regclass
                      AND conname = 'approved_entries_have_a_proposal') THEN
        -- Which entry types must come from an approval at all. The maker-checker
        -- subjects may only enter the ledger through a proposal; the
        -- machine-originated types must carry none, so a payment cannot be
        -- dressed up as an approved adjustment.
        ALTER TABLE ledger_entries ADD CONSTRAINT approved_entries_have_a_proposal CHECK (
            (entry_type IN ('adjustment','fee_waived') AND pending_movement_id IS NOT NULL)
         OR (entry_type NOT IN ('adjustment','fee_waived') AND pending_movement_id IS NULL)
        );
    END IF;
END $$;


-- --- invariants 1 and 2: one terminal transition, and nothing else changes -----

CREATE OR REPLACE FUNCTION pending_movements_single_transition() RETURNS trigger AS $$
BEGIN
    -- The substance never changes, resolved or not. `reason` and `requested_at`
    -- are in this list, not merely the fields describing the money: anything
    -- holding the application role could otherwise rewrite WHY a movement was
    -- requested, after a second person approved the reason they were shown. The
    -- reason is the evidence D8 says is missing; a rewritable reason is a note.
    IF NEW.loan_id        IS DISTINCT FROM OLD.loan_id
    OR NEW.component      IS DISTINCT FROM OLD.component
    OR NEW.amount         IS DISTINCT FROM OLD.amount
    OR NEW.entry_type     IS DISTINCT FROM OLD.entry_type
    OR NEW.reason         IS DISTINCT FROM OLD.reason
    OR NEW.requested_by   IS DISTINCT FROM OLD.requested_by
    OR NEW.requested_role IS DISTINCT FROM OLD.requested_role
    OR NEW.requested_at   IS DISTINCT FROM OLD.requested_at THEN
        RAISE EXCEPTION 'the substance of a pending movement is immutable';
    END IF;

    -- PM-LINK-001. The link may not be attached on the SAME update that resolves
    -- the proposal. Until this, the first resolution could set `resolution` and
    -- `ledger_entry_id` together -- and nothing checked that the entry it pointed
    -- at belonged to THIS proposal, so an approval could commit citing another
    -- proposal's ledger row. Invariants 6 and 7 were enforced on the entry's way
    -- in and not on the proposal's way out.
    --
    -- Forcing a separate update is what makes the reciprocal checkable: the entry
    -- must already exist, and by then it carries a `pending_movement_id` the
    -- deferred check below compares against this row.
    IF OLD.resolution IS NULL AND NEW.ledger_entry_id IS NOT NULL THEN
        RAISE EXCEPTION 'a pending movement may not gain a ledger entry link in '
                        'the same statement that resolves it -- insert the entry '
                        'first, then attach it';
    END IF;

    IF OLD.resolution IS NOT NULL THEN
        -- Already resolved. Exactly ONE further write is legal: attaching the
        -- ledger entry this approval produced, once, from NULL. An earlier
        -- revision refused every post-resolution UPDATE outright, which made the
        -- approval order impossible -- mark approved, insert the entry, write the
        -- link back -- so no staff movement could ever complete.
        IF OLD.ledger_entry_id IS NOT NULL THEN
            RAISE EXCEPTION 'pending movement % is already linked to entry %',
                OLD.id, OLD.ledger_entry_id;
        END IF;
        IF NEW.ledger_entry_id IS NULL THEN
            RAISE EXCEPTION 'pending movement % is already %', OLD.id, OLD.resolution;
        END IF;
        IF OLD.resolution <> 'approved' THEN
            RAISE EXCEPTION 'pending movement % was %, so it produces no ledger entry',
                OLD.id, OLD.resolution;
        END IF;
        IF NEW.resolution    IS DISTINCT FROM OLD.resolution
        OR NEW.resolved_by   IS DISTINCT FROM OLD.resolved_by
        OR NEW.resolved_role IS DISTINCT FROM OLD.resolved_role
        OR NEW.resolved_at   IS DISTINCT FROM OLD.resolved_at
        OR NEW.resolved_threshold IS DISTINCT FROM OLD.resolved_threshold THEN
            RAISE EXCEPTION 'a resolved movement may only gain its ledger entry link';
        END IF;
        RETURN NEW;
    END IF;

    RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS pending_movements_one_way ON pending_movements;
CREATE TRIGGER pending_movements_one_way
    BEFORE UPDATE ON pending_movements
    FOR EACH ROW EXECUTE FUNCTION pending_movements_single_transition();


-- --- invariant 5: retention is a delete guard, not a promise -------------------

CREATE OR REPLACE FUNCTION pending_movements_are_retained() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'pending movement % may not be deleted: proposals are retained as the '
        'evidence of what staff asked for (%)',
        OLD.id, COALESCE(OLD.resolution, 'pending');
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS pending_movements_no_delete ON pending_movements;
CREATE TRIGGER pending_movements_no_delete
    BEFORE DELETE ON pending_movements
    FOR EACH ROW EXECUTE FUNCTION pending_movements_are_retained();


-- --- invariant 4: checked at COMMIT, because the intermediate state is legal ---

CREATE OR REPLACE FUNCTION pending_movement_resolution_is_complete() RETURNS trigger AS $$
DECLARE
    final_resolution TEXT;
    final_entry      BIGINT;
BEGIN
    -- The row AS IT STANDS NOW, not as it stood when this event was queued.
    -- Running at COMMIT inside the same transaction, this sees every earlier
    -- statement's effect, so the transient approved-without-entry state that
    -- queued the event is no longer what is validated. Both queued events re-read
    -- the same final row and agree, which makes the check idempotent rather than
    -- order-dependent -- an implementation adding a third UPDATE must not be able
    -- to break it by doing so.
    SELECT resolution, ledger_entry_id
      INTO final_resolution, final_entry
      FROM pending_movements
     WHERE id = NEW.id;

    IF NOT FOUND THEN
        -- Inserted and deleted in the same transaction. Nothing is committed
        -- about this proposal, so there is nothing to validate.
        RETURN NULL;
    END IF;

    IF final_resolution = 'approved' AND final_entry IS NULL THEN
        RAISE EXCEPTION 'approved movement % has no ledger entry', NEW.id;
    END IF;
    IF final_resolution IS DISTINCT FROM 'approved' AND final_entry IS NOT NULL THEN
        RAISE EXCEPTION 'movement % is %, so it must have no ledger entry',
                        NEW.id, COALESCE(final_resolution, 'pending');
    END IF;

    -- PM-LINK-001. Non-null is not enough: the entry must be THIS proposal's.
    -- Checked in both directions, because each catches a different mistake --
    -- a proposal pointing at a foreign entry, and an entry whose own
    -- `pending_movement_id` names someone else.
    IF final_entry IS NOT NULL THEN
        PERFORM 1 FROM ledger_entries
         WHERE id = final_entry AND pending_movement_id = NEW.id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'movement % points at ledger entry %, which does not '
                            'name it -- an approval may only link the entry it '
                            'authorised', NEW.id, final_entry;
        END IF;
    END IF;
    RETURN NULL;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS pending_movements_resolution_complete ON pending_movements;
CREATE CONSTRAINT TRIGGER pending_movements_resolution_complete
    AFTER INSERT OR UPDATE ON pending_movements
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION pending_movement_resolution_is_complete();


-- --- invariants 3, 6 and 7: the entry must BE the movement that was approved ---

CREATE OR REPLACE FUNCTION ledger_entry_matches_its_proposal() RETURNS trigger AS $$
DECLARE
    proposal pending_movements;
BEGIN
    -- Machine-originated entries have no proposal and are not this trigger's
    -- business. `approved_entries_have_a_proposal` already refuses them a
    -- pending_movement_id.
    IF NEW.entry_type NOT IN ('adjustment','fee_waived') THEN
        RETURN NEW;
    END IF;

    IF NEW.pending_movement_id IS NULL THEN
        RAISE EXCEPTION 'a % entry must name the proposal that authorised it',
                        NEW.entry_type;
    END IF;

    -- FOR SHARE, not a plain read: the proposal must not be resolved differently
    -- by another transaction between this check and the commit depending on it.
    SELECT * INTO proposal FROM pending_movements
     WHERE id = NEW.pending_movement_id FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'pending movement % does not exist', NEW.pending_movement_id;
    END IF;

    IF proposal.resolution IS DISTINCT FROM 'approved' THEN
        RAISE EXCEPTION 'pending movement % is %, so it authorises no entry',
                        proposal.id, COALESCE(proposal.resolution, 'pending');
    END IF;

    -- Belt and braces with `no_self_approval` on the table. This is the path that
    -- writes the money, so it re-checks rather than assuming the constraint
    -- guarding the other path was never dropped.
    IF proposal.resolved_by IS NULL OR proposal.resolved_by = proposal.requested_by THEN
        RAISE EXCEPTION 'pending movement % has no distinct approver', proposal.id;
    END IF;

    IF NEW.loan_id    IS DISTINCT FROM proposal.loan_id
    OR NEW.component  IS DISTINCT FROM proposal.component
    OR NEW.amount     IS DISTINCT FROM proposal.amount
    OR NEW.entry_type IS DISTINCT FROM proposal.entry_type THEN
        RAISE EXCEPTION 'entry does not match pending movement % -- an approval '
                        'may not authorise different terms than the ones reviewed',
                        proposal.id;
    END IF;

    -- Overwritten, not validated: the actor on a human-authorised entry is the
    -- APPROVER, and a caller reproducing everything else correctly must not get
    -- to choose who is credited with authorising it. Both fields, because
    -- ledger_actor_required needs both and overwriting only the id would leave
    -- the role caller-supplied on exactly the row that exists to record it.
    NEW.actor_id   := proposal.resolved_by;
    NEW.actor_role := proposal.resolved_role;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS ledger_entries_match_proposal ON ledger_entries;
CREATE TRIGGER ledger_entries_match_proposal
    BEFORE INSERT ON ledger_entries
    FOR EACH ROW EXECUTE FUNCTION ledger_entry_matches_its_proposal();

COMMIT;
