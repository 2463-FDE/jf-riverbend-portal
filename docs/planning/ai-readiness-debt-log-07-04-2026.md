# AI-Readiness Debt Log — 2026-07-04

## ID normalization note

The weekly deliverable references this entry as naming "D1, D9, and D3." Only
**D1** is a literal `DEBT D#` code-level marker that exists in this repository
(`services/intake-service/app.py`). There is no `DEBT D9` or `DEBT D3` marker
anywhere in source (verified by repo-wide search). "D9" and "D3" are
normalized here, for traceability, to their corresponding system-audit
finding IDs from `docs/analysis/system-audit-07-01-2026.md`:

- D9 → **AUD-09**
- D3 → **AUD-03**

This distinction is called out explicitly so this document never implies a
code marker exists where it doesn't.

## D1 — PHI logged in plaintext (code-level marker)

- **Evidence:** `services/intake-service/app.py:65`
  (`log.info('POST /intake body=%s', req.model_dump_json())`), writing to
  `logs/intake-service.log` at INFO per `services/intake-service/logging_config.py`.
- **What's in the payload:** name, DOB, SSN, free-text clinical notes — the
  full intake request body.
- **Business risk:** Anyone with read access to a log file (a broader,
  less-monitored surface than the database) gets PHI in the clear, with no
  audit trail of who read it. In business terms: a log-shipping misconfig, a
  support engineer tailing logs, or a log-aggregation breach becomes a PHI
  breach with no additional access barrier.
- **Relevance to this deliverable:** the LLM client (Commit 2) and any future
  AI feature built on top of it are one unguarded `log.info(prompt)` or
  `log.info(response)` away from repeating this exact failure with model
  input/output instead of intake bodies. The PHI-safe logging policy and
  redaction helper (Commit 3) exist specifically to prevent that recurrence
  in new code.
- **Status:** Known, tracked debt (see `docs/analysis/system-audit-07-01-2026.md`
  finding `AUD-06`). Not remediated by this deliverable — `app.py:65` is left
  untouched, per the project rule against opportunistically fixing documented
  debt as a side effect of unrelated work.

## AUD-09 (normalized "D9") — No master-patient-index / fragmented identity

- **Evidence:** `db/schema.sql` (no unique match key on name/dob/ssn),
  `intake.yaml` (`match_key: none`), real-world manifestation in
  `docs/handover/jira-tickets.md` RIV-160 (clinician Dr. Nguyen observed a
  patient's — Maria Gonzalez's — allergy list differ depending on which chart
  was opened).
- **Business risk:** Every `/intake` call can create a brand-new patient row
  instead of matching an existing one, so one person's clinical history can
  be split across multiple chart rows with no system-level indication this
  has happened.
- **Relevance to this deliverable:** an AI feature that summarizes "a
  patient's history" inherits whichever single chart row it's pointed at. If
  that row is one of several fragments, the summary is confidently wrong by
  omission, with nothing distinguishing it from a complete summary. This is a
  correctness/patient-safety risk, not just a data-quality one — it already
  compounded with a separate HL7 data-loss bug in the RIV-160 incident.
- **Status:** Known, tracked debt (`AUD-09`). No identity-resolution work is
  in scope for this deliverable; this entry exists so the risk is visible
  before any AI feature is scoped against patient history.

## AUD-03 (normalized "D3") — ROI/disclosure has no authorization enforcement

- **Evidence:** `services/roi-service/app.py:83-170` (`DEBT D12`, no check for
  a signed 45 CFR 164.508 authorization before releasing records);
  `db/schema.sql:160-167` (`disclosures` table has no `authorization_id`,
  purpose, or restriction tracking). Independently confirmed in
  `docs/handover/auditor-questionnaire.md`: a real payer pre-visit audit asked
  for an accounting of disclosures (Q7) and staff's answer was "We don't have
  a way to pull that."
- **Business risk:** PHI can already be released to a named recipient with no
  verification that a valid authorization exists, and the system cannot
  answer a real regulatory question about what was disclosed to whom. This
  has already happened as a real audit gap, not a hypothetical.
- **Relevance to this deliverable:** any AI-mediated read of PHI is itself a
  new access/disclosure path. Attaching one to a system that cannot already
  account for its existing disclosures would add a channel this audit gap
  can't even see, let alone govern.
- **Status:** Known, tracked debt (`AUD-03`, code-level marker `DEBT D12`). No
  authorization-framework work is in scope for this deliverable.

## Why this log exists

These three findings — one code-level (D1), two audit-level (AUD-09, AUD-03)
— are the specific, evidence-backed reasons the client's requested AI
assistant is not being built this week. This deliverable ships the safety
scaffolding (provider-swappable LLM client, PHI-safe logging policy) that a
future assistant would need to sit on top of safely, without building the
assistant itself on top of a data plane that already has these open gaps.
