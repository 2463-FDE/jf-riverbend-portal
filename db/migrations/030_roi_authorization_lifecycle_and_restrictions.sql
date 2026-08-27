-- 030_roi_authorization_lifecycle_and_restrictions.sql — w8-planner-2, P4
-- follow-up: a persisted, human-reviewed authorization record replaces
-- trusting a fresh signer/reference/timestamp payload supplied at the
-- moment of fulfillment (029's original design), plus a narrowly scoped
-- patient disclosure-restriction check.
--
-- WHY 029's DESIGN WASN'T ENOUGH. A caller who could reach fulfill could
-- assert ANY authorization_reference/signed_by/signed_at string it liked —
-- Pydantic proved the fields were non-empty, not that a real, reviewed,
-- unexpired, unrevoked authorization actually exists for this patient and
-- this recipient. That is not a 164.508 gate, it is a required-field
-- check. This migration makes the authorization a real, addressable
-- record with a lifecycle fulfillment must load and revalidate.
--
-- roi_requests/disclosures keep their 029 authorization_reference/
-- signed_at/signed_by/purpose columns unchanged, still populated at
-- fulfillment time — but now copied FROM the loaded, validated
-- roi_authorizations row, never from caller-supplied input. That is what
-- authorization_id below is for.

CREATE TABLE IF NOT EXISTS roi_authorizations (
    id                          SERIAL PRIMARY KEY,
    patient_id                  INTEGER NOT NULL REFERENCES patients(id),
    recipient                   TEXT NOT NULL,
    purpose                     TEXT,
    -- NULL scope_start/scope_end means "no date-range limit stated" — a
    -- fulfillment request's OWN date_range_start/end must fall within a
    -- non-null bound, checked at fulfillment time (see app.py).
    scope_start                 TEXT,
    scope_end                   TEXT,
    -- A pointer to where the actual signed document lives, plus an
    -- optional content digest — this table stores evidence of a
    -- signature, never the document itself.
    signature_evidence_reference TEXT NOT NULL,
    signature_evidence_digest    TEXT,
    signed_by                   TEXT NOT NULL,
    signed_at                   TIMESTAMPTZ NOT NULL,
    expires_at                  TIMESTAMPTZ,
    -- pending (default, awaiting human review) | valid | rejected | revoked
    status                      TEXT NOT NULL DEFAULT 'pending',
    reviewed_by                 TEXT,
    reviewed_at                 TIMESTAMPTZ,
    revoked_at                  TIMESTAMPTZ,
    -- Set only when the signer is not the patient themselves (e.g. legal
    -- guardian, power of attorney) — NULL means the patient signed for
    -- their own record.
    representative_authority    TEXT,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE roi_requests ADD COLUMN IF NOT EXISTS authorization_id INTEGER REFERENCES roi_authorizations(id);
ALTER TABLE disclosures ADD COLUMN IF NOT EXISTS authorization_id INTEGER REFERENCES roi_authorizations(id);

-- Narrowly scoped: a patient-level "do not disclose" flag, optionally
-- limited to one named recipient. NOT a general consent-management
-- platform — no category taxonomy, no per-record-type scoping, no
-- expiration logic beyond an explicit revoke.
CREATE TABLE IF NOT EXISTS roi_disclosure_restrictions (
    id          SERIAL PRIMARY KEY,
    patient_id  INTEGER NOT NULL REFERENCES patients(id),
    -- NULL = blanket restriction (blocks every recipient); set = blocks
    -- only disclosures to this exact recipient string.
    recipient   TEXT,
    reason      TEXT,
    active      BOOLEAN NOT NULL DEFAULT true,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at  TIMESTAMPTZ
);
