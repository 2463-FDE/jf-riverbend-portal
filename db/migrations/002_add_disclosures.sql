-- 002_add_disclosures — start of the ROI feature (incomplete)
-- TODO: add authorization_id, purpose, and restriction columns before go-live.

CREATE TABLE disclosures (
    id            SERIAL PRIMARY KEY,
    patient_id    INTEGER NOT NULL REFERENCES patients(id),
    disclosed_to  TEXT,
    disclosed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
