-- 0037 -- ADR 0011 step 2: approval is ONE function, not a sequence an
-- application is trusted to perform in the right order.
--
-- 0036 built the shape. This is the only path that resolves a proposal, and it
-- exists as a function rather than as application SQL for a specific reason: the
-- ordering the triggers require is not discoverable from the requirements, and
-- getting it wrong either deadlocks or leaves an approved proposal with no
-- entry. Putting it here means every caller gets that order, and a second caller
-- written later cannot get a different one.
--
-- What ADR 0011 requires this function to prove, each of which is a test:
--   1. it LOCKS the proposal before reading its state
--   2. exactly one transition, ever
--   3. the requester may not approve their own request
--   4. an approval writes exactly one entry; a rejection writes none
--   5. the entry is built FROM the locked row, never from caller input
--   6. the ledger actor is the approver
--
-- **No policy is encoded here.** The admin threshold, the maximum delta and the
-- permitted loan statuses are human-approved configuration for a cohort/demo
-- environment; they arrive as parameters from a caller that read them from the
-- environment and failed closed if they were missing. A figure baked into this
-- function would make a policy change a migration, and would put a demo number
-- in the database where a reader would take it for a rule.

BEGIN;

CREATE OR REPLACE FUNCTION resolve_pending_movement(
    p_movement_id        BIGINT,
    p_resolver           INTEGER,
    p_resolver_role      TEXT,
    p_resolution         TEXT,          -- 'approved' | 'rejected'
    p_threshold          NUMERIC,       -- the bar this decision is judged against
    p_permitted_statuses TEXT[]         -- loan statuses a movement may execute on
) RETURNS BIGINT AS $$
DECLARE
    proposal     pending_movements;
    loan_status  TEXT;
    component_now NUMERIC;
    new_entry    BIGINT;
BEGIN
    IF p_resolution NOT IN ('approved', 'rejected') THEN
        RAISE EXCEPTION 'resolution must be approved or rejected, not %', p_resolution;
    END IF;
    IF p_resolver IS NULL OR p_resolver_role IS NULL THEN
        RAISE EXCEPTION 'a resolution must name the human making it';
    END IF;
    IF p_threshold IS NULL THEN
        RAISE EXCEPTION 'a resolution must record the threshold it was judged '
                        'against -- an approval history is unreadable if the bar '
                        'moved and nothing says when';
    END IF;
    IF p_permitted_statuses IS NULL OR array_length(p_permitted_statuses, 1) IS NULL THEN
        RAISE EXCEPTION 'no permitted loan statuses were supplied, so no movement '
                        'can be shown to be executable -- refusing rather than '
                        'assuming a default';
    END IF;

    -- 1. LOCK FIRST. Two approvers clicking at once would otherwise both read
    -- `resolution IS NULL` and both proceed. Everything below reads the locked
    -- row, never the caller's idea of it.
    SELECT * INTO proposal FROM pending_movements
     WHERE id = p_movement_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'pending movement % does not exist', p_movement_id;
    END IF;

    -- 2. Exactly one transition. The loser of the race lands here.
    IF proposal.resolution IS NOT NULL THEN
        RAISE EXCEPTION 'pending movement % is already %',
                        proposal.id, proposal.resolution;
    END IF;

    -- 3. No self-approval, including admin. Checked here as well as by the table
    -- constraint because this is the path that writes the money.
    IF p_resolver = proposal.requested_by THEN
        RAISE EXCEPTION 'pending movement % was requested by %, who may not '
                        'resolve it', proposal.id, proposal.requested_by;
    END IF;

    -- Revalidate the whole executable target INSIDE the lock. A proposal that
    -- was valid when raised is not necessarily valid now: the loan may have
    -- closed, servicing may have been removed, the fees may have been paid down.
    -- A check performed when the proposal entered the queue is not evidence
    -- about the state when money moves.
    --
    -- Done for a rejection too, but only as far as reading -- a rejection moves
    -- nothing, so it must remain possible even for a target that has since
    -- become unexecutable. Otherwise a proposal against a closed loan could be
    -- neither approved nor rejected, and would sit in the queue for ever.
    IF p_resolution = 'approved' THEN
        SELECT l.status INTO loan_status
          FROM loans l
          JOIN balances b ON b.loan_id = l.id
         WHERE l.id = proposal.loan_id
         FOR SHARE OF l;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'loan % is no longer serviced (missing loan or '
                            'balances row), so movement % cannot execute',
                            proposal.loan_id, proposal.id;
        END IF;

        -- Exact match, including case: a status the caller does not recognise
        -- must refuse rather than be normalised into one it does.
        --
        -- `loan_status IS NULL` is tested SEPARATELY and first. `NULL = ANY(...)`
        -- is NULL, `NOT NULL` is NULL, and `IF NULL THEN` does not execute -- so
        -- without this a loan with no status at all would sail through the check
        -- that exists to refuse unrecognised ones. `loans.status` is a nullable
        -- TEXT column, so that row shape is reachable rather than theoretical.
        -- Found by parametrising the status test over NULL.
        IF loan_status IS NULL OR NOT (loan_status = ANY (p_permitted_statuses)) THEN
            RAISE EXCEPTION 'loan % is %, which is not a status a movement may '
                            'execute on', proposal.loan_id,
                            COALESCE(loan_status, 'unset');
        END IF;

        -- The component may not be driven below zero. Re-read now, not at
        -- creation: a waiver raised when fees were 80.00 and approved after they
        -- were paid down to 10.00 was valid when written and is not now.
        SELECT CASE proposal.component
                 WHEN 'fees' THEN COALESCE(b.past_due, 0)
                 ELSE b.balance
               END
          INTO component_now
          FROM balances b WHERE b.loan_id = proposal.loan_id;
        IF component_now + proposal.amount < 0 THEN
            RAISE EXCEPTION 'movement % would take % below zero (% + % < 0)',
                            proposal.id, proposal.component, component_now,
                            proposal.amount;
        END IF;
    END IF;

    -- The order below is the one the 0036 triggers require, and it is why this
    -- is a function. Mark resolved first; insert the entry second; attach the
    -- link third, in its own statement. Reversing the first two fails the
    -- entry's proposal check ("is pending, so it authorises no entry"), and
    -- combining the first and third is refused outright by the transition
    -- trigger, which is what makes the reciprocal link verifiable.
    UPDATE pending_movements
       SET resolution = p_resolution,
           resolved_by = p_resolver,
           resolved_role = p_resolver_role,
           resolved_at = now(),
           resolved_threshold = p_threshold
     WHERE id = proposal.id;

    IF p_resolution = 'rejected' THEN
        -- 4. A rejection writes no entry, and the proposal is retained as the
        -- evidence that a control refused something.
        RETURN NULL;
    END IF;

    -- 5. Built FROM the locked row. Nothing here reads a caller argument for the
    -- money: an approval that inserted different terms than the ones reviewed
    -- would be a bypass wearing the shape of an approval.
    --
    -- 6. actor_id/actor_role are supplied, and the ledger's own trigger
    -- overwrites them from the proposal regardless -- so a future caller of this
    -- function cannot smuggle a different actor in either.
    INSERT INTO ledger_entries
        (loan_id, component, amount, entry_type, reason,
         actor_id, actor_role, pending_movement_id)
    VALUES
        (proposal.loan_id, proposal.component, proposal.amount, proposal.entry_type,
         proposal.reason, p_resolver, p_resolver_role, proposal.id)
    RETURNING id INTO new_entry;

    UPDATE pending_movements
       SET ledger_entry_id = new_entry
     WHERE id = proposal.id;

    RETURN new_entry;
END $$ LANGUAGE plpgsql;

COMMENT ON FUNCTION resolve_pending_movement(BIGINT, INTEGER, TEXT, TEXT, NUMERIC, TEXT[]) IS
    'The only path that resolves a maker-checker proposal (ADR 0011). Locks the '
    'proposal, permits exactly one transition, refuses self-approval, revalidates '
    'the executable target inside the lock, and on approval writes exactly one '
    'ledger entry built from the locked row. Policy (threshold, permitted '
    'statuses) is passed in by a caller that read it from configuration -- this '
    'function encodes none.';

COMMIT;
