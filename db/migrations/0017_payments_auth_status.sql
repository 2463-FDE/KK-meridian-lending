-- 0017 — review fix: payment-service's charge() treated a processor_token as
-- proof of a real charge without ever calling a processor to confirm it --
-- a borrower could POST any made-up token and the code would write a
-- captured payment and reduce their loan balance for real. See
-- services/payment-service/app/processor.py::authorize_charge().
--
-- auth_status is written 'pending' on insert, before authorization is
-- confirmed, then flipped to 'captured' (processor approved) or 'failed'
-- (processor declined, or unreachable outside dev/test) once the processor
-- call actually returns. A row stuck at 'pending' means the process died
-- mid-authorization -- it was never silently treated as success. Historical
-- rows default to 'captured': they really were, just without a formal
-- record of it until now.

ALTER TABLE payments
    ADD COLUMN IF NOT EXISTS auth_status TEXT NOT NULL DEFAULT 'captured';
