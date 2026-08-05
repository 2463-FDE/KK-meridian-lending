-- 0021 -- workflow requirement: an offer moves through OFFER_CREATED ->
-- OFFER_ACCEPTED -> BOARDED. accepted_at makes "the offer has been
-- accepted" a real, stored fact rather than an inferred one -- stamped the
-- moment accept_offer boards the loan (services/origination-service/app/
-- routers/applications.py). In this system accepting the offer and
-- boarding the loan happen as one atomic action (there is no separate
-- "accept only" step before boarding today), so accepted_at and the loan's
-- creation always land together -- see that router's own comment for why a
-- distinct "offer created but not yet accepted" blocking check would be
-- unreachable, not just untested.

ALTER TABLE offers
    ADD COLUMN IF NOT EXISTS accepted_at TIMESTAMPTZ;
