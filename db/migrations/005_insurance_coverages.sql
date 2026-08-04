-- 005_insurance_coverages — payer/eligibility records
-- 2026-03-01 · Helix Digital Partners
-- Stores the coverage captured at intake. eligibility-service verifies these
-- against the clearinghouse (X12 270/271).

CREATE TABLE IF NOT EXISTS insurance_coverages (
    id           SERIAL PRIMARY KEY,
    patient_id   INTEGER NOT NULL REFERENCES patients(id),
    payer_name   TEXT,
    member_id    TEXT,
    group_number TEXT,
    plan_type    TEXT,
    status       TEXT DEFAULT 'unknown',
    verified_at  TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
