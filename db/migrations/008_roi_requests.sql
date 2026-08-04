-- 008_roi_requests — release-of-information request intake
-- 2026-05-12 · Helix Digital Partners
-- Starts moving ROI off email/PDF and into the portal. Captures who wants what
-- and why.
-- TODO (before go-live): add authorization_id + a link to the signed 164.508
-- authorization, and 164.522 restriction tracking. Not done yet — the fulfill
-- path currently releases records without checking any of this.

CREATE TABLE IF NOT EXISTS roi_requests (
    id               SERIAL PRIMARY KEY,
    patient_id       INTEGER NOT NULL REFERENCES patients(id),
    requested_by     TEXT,
    recipient        TEXT,
    recipient_type   TEXT,
    purpose          TEXT,
    date_range_start TEXT,
    date_range_end   TEXT,
    status           TEXT NOT NULL DEFAULT 'pending',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE disclosures ADD COLUMN IF NOT EXISTS roi_request_id INTEGER REFERENCES roi_requests(id);
