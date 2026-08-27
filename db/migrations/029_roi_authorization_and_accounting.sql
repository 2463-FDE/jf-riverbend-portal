-- 029_roi_authorization_and_accounting.sql — ROI authorization gate + a real
-- accounting of disclosures (w8-planner-2, closes the "staff can't answer
-- Q7" gap docs/handover/auditor-questionnaire.md documents: "Produce an
-- accounting of disclosures for patient X ... including to whom and under
-- what authorization. We don't have a way to pull that.").
--
-- SCOPE. This closes two of the three legs services/roi-service/app.py's own
-- DEBT D12 comment named:
--   * 45 CFR 164.508 — no check for a signed authorization before releasing
--     PHI to a third party. roi-service now REJECTS fulfillment unless the
--     caller supplies authorization_reference/signed_at/signed_by.
--   * 45 CFR 164.528 — accounting of disclosures. Every fulfillment now
--     writes a disclosure row carrying who/when/why/under-what-authorization,
--     and GET /roi/patients/{id}/accounting reads it back directly.
-- Still explicitly OUT of scope, unchanged: 45 CFR 164.522 (honoring an
-- agreed restriction) — there is still nowhere to record one, and this
-- migration does not add that. Do not describe this as closing 164.522.
--
-- WHY THE DISCLOSURE ROW GETS ITS OWN COPY OF authorization_reference/purpose,
-- NOT JUST A FOREIGN KEY TO roi_requests. A 164.528 accounting must describe
-- what was actually disclosed and under what authorization AT THE TIME OF
-- DISCLOSURE — if the request row were edited afterward (a corrected
-- recipient, a re-used request), a FK-only design would silently rewrite
-- history the accounting depends on. Guarded/idempotent, safe to re-apply
-- (see apply.sh) — every ADD COLUMN is IF NOT EXISTS.

ALTER TABLE roi_requests ADD COLUMN IF NOT EXISTS authorization_reference TEXT;
ALTER TABLE roi_requests ADD COLUMN IF NOT EXISTS authorization_signed_at TIMESTAMPTZ;
ALTER TABLE roi_requests ADD COLUMN IF NOT EXISTS authorization_signed_by TEXT;

ALTER TABLE disclosures ADD COLUMN IF NOT EXISTS authorization_reference TEXT;
ALTER TABLE disclosures ADD COLUMN IF NOT EXISTS purpose TEXT;
