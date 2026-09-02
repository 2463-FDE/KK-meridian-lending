-- 0048 -- G-02: a resolver's authority must be CURRENT when money moves.
--
-- THE GAP THIS CLOSES, measured on a running stack before the change. The
-- gateway's Redis session is a snapshot taken at login and lives eight hours by
-- default, and nothing re-read the account behind it. With `users.is_active` set
-- false and the same bearer token reused, `POST /auth/login` correctly refused
-- with 401 while that session went on to APPROVE a pending movement and write a
-- real `ledger_entries` row -- an immutable record naming an approver whose
-- authority had already been withdrawn.
--
-- The gateway now refuses a deactivated account, which closes the eight-hour
-- window. This closes the millisecond one. A deactivation committing between the
-- gateway's check and the UPDATE below would still have written the entry;
-- re-reading the resolver `FOR SHARE`, inside the same lock that already
-- revalidates the loan and the component, makes the deactivation wait for this
-- transaction instead of interleaving with it.
--
-- Consistent with what this function already does: it re-reads the loan status
-- and the component balance inside the lock precisely because a fact that was
-- true when the proposal was raised is not evidence about the state when money
-- moves. The resolver's authority is another such fact. It is also the same
-- check `manual_dti_is_permitted` (BDTI-02) already makes for evidence rows,
-- generalised to the path that moves money.
--
-- REPLACES the 0037 body and changes nothing else about it: same signature, same
-- ordering, same policy-free parameters. Idempotent -- CREATE OR REPLACE, and
-- re-running it is a no-op.

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

    -- 3a. THE RESOLVER'S AUTHORITY MUST BE CURRENT (G-02). Locked and re-read
    -- here for the same reason the loan and the component are re-read below: the
    -- authority someone held when they opened the queue is not evidence about
    -- the authority they hold now, and this is the statement that moves money.
    --
    -- The gateway also refuses a deactivated account, and that is the boundary
    -- that closes the eight-hour session window. It cannot close this one. A
    -- deactivation committing between the gateway's check and this UPDATE would
    -- otherwise still write a ledger entry naming an approver whose authority
    -- had already been withdrawn -- a TOCTOU window measured in milliseconds
    -- rather than hours, but writing an immutable row that cannot be taken back.
    -- `FOR SHARE` makes the deactivation wait for this transaction rather than
    -- interleave with it, so the two orderings agree.
    --
    -- The ROLE is re-read too, not just the flag: `resolved_role` is written from
    -- the caller's claim, and a role that has since changed would be recorded as
    -- evidence of an authority the person no longer has. Same reasoning as
    -- `manual_dti_is_permitted` (BDTI-02), which this deliberately mirrors.
    --
    -- ONLY the resolver. The proposer is not re-checked, and that is a decision:
    -- a proposal moves nothing, so it stays answerable -- approvable or
    -- rejectable -- even if the person who raised it has since left. Refusing it
    -- would strand the row in the queue for ever, which is the same trap the
    -- rejection path above is shaped to avoid.
    PERFORM 1 FROM users
      WHERE id = p_resolver AND is_active AND role = p_resolver_role
      FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'resolver % does not hold current authority as % (account '
                        'missing, deactivated, or role changed), so movement % '
                        'may not be resolved', p_resolver, p_resolver_role,
                        proposal.id;
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
    'the executable target AND the resolver''s current authority inside the lock, '
    'and on approval writes exactly one ledger entry built from the locked row. '
    'Policy (threshold, permitted statuses) is passed in by a caller that read it '
    'from configuration -- this function encodes none.';

COMMIT;
