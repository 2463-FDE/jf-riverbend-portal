# ADR 0008 — PHI encryption at rest: recorded risk decision

**Date:** 2026-08-20
**Status:** Superseded in part — see 2026-08-26 update below
**Context:** 2026-08-28 HIPAA-readiness closure, item C3
**Supersedes:** the encryption claims previously in `README.md` and
`db/schema.sql`, both of which were false and are corrected in this change.

**Update, 2026-08-26 (w8-planner-2 P2):** the "owner: unassigned" /
"blocked on the deployment target being named" state below no longer
holds. The client made the layer-1 call this ADR flagged as theirs to
make — application-level field encryption, an HMAC blind index replacing
`ssn_digits`, environment-provided keys (no KMS integration exists to
target). The remediation plan's steps 1–4 are implemented in
`adr/0012-phi-field-encryption-shared-crypto-library.md` and
`db/migrations/031_phi_field_encryption.sql`: `ssn`, `dob`, and `notes` on
`patients` are AEAD-encrypted at the application layer before every write,
and `ssn_digits` is replaced by an HMAC-SHA256 blind index computed under a
key independent of the encryption key. Step 5 (the RAG/policy corpus) is confirmed out of scope for this specific
migration: `libs/rag_corpus` reads `patient_id` as a plain integer FK and
never touches `ssn`/`dob`/`notes`, and its corpus source is checked-in
fixture CSVs, not a live `patients` read — but it does still embed
`records.title`/`records.body` text, which is a separate PHI surface this
ADR does not evaluate and does not claim is handled. The rest of this document is
left as-written below: it is the accurate history of why the gap existed
and for how long, which the correction above does not erase.

## Context

PHI in this system is stored in plain text. `db/schema.sql`:

- `patients.dob` — `TEXT`
- `patients.ssn` — `TEXT`
- `patients.notes` — `TEXT`, free-text clinical notes
- `patients.ssn_digits` — a **generated column** holding the SSN with
  non-digits stripped, and it is **indexed** (`patients_ssn_digits_idx`)

There is no `pgcrypto`, no `pgp_sym_encrypt`, and no application-layer
cryptography anywhere in the repository.

Two documents claimed otherwise. `README.md:1` — the first line anyone reads —
asserted *"All PHI is encrypted and the system is fully HIPAA compliant."* and
`db/schema.sql:2` asserted *"All PHI is protected at the disk level (RDS volume
encryption)."* **There is no RDS in this deployment.** The stack is docker
compose with a local `pgdata` volume. The schema comment was addressed to
engineers, who had every reason to believe it.

### The constraint that makes this non-trivial

`ssn_digits` is derived from `ssn` and indexed. Encrypting `ssn` alone leaves
the SSN readable and searchable through the generated column. That column is
not removable either: `adr/0004` and RIV-160 depend on it for deterministic
`(dob, ssn)` duplicate-match lookup at intake, which is a shipped control.

Real column encryption therefore requires replacing `ssn_digits` with a keyed
digest, reworking the match-key lookup, and re-examining every consumer —
intake, records and the RAG corpus. That is multi-day work touching shipped
behaviour.

## Decision

**Do not implement encryption at rest in this cycle. Correct the false claims,
and record this as a risk decision rather than an omission.**

Encryption at rest is an **addressable** implementation specification under
45 CFR 164.312(a)(2)(iv) — not a required one. "Addressable" does not mean
optional: it obliges the covered entity to assess whether the safeguard is
reasonable and appropriate, and where it is not, to **document that
determination and implement an equivalent alternative measure if reasonable**.
This ADR is that documentation. Without it, "we chose not to encrypt" is an
audit finding; with it, it is a defensible position.

**Why it is not reasonable in this window:** six working days remain to
2026-08-28, the change touches a shipped match-key control, and no deployment
target exists against which a volume-level alternative could be evidenced (see
below). Shipping a partial encryption scheme that leaves an indexed plaintext
SSN behind would be worse than the current state, because it would invite the
belief that PHI is protected.

**Why no volume-level claim is made instead.** Volume or disk encryption was
considered as the equivalent alternative measure. It is rejected **as a claim**
because there is nothing to evidence it against: no deployment step exists
anywhere in the repository, no environment is described, and the RDS named in
the old schema comment does not exist. An unevidenced control is worse than a
stated gap — it is how the two false claims above came to be written in the
first place.

## Compensating controls that do exist

These are implemented and tested. They do not substitute for encryption at
rest, and this ADR does not claim they do — they reduce the exposure that
plaintext storage creates:

| Control | Where |
|---|---|
| Per-(actor, patient) authorization, failing closed before any patient row is read | `services/records-service/patient_access_gate.py` |
| Clinical notes withheld from roles without permission | `_redact_clinical_fields`, `services/records-service/app.py` |
| Caller verification on every service; untokened in-network calls refused | six services, `_verify_internal_token` |
| No domain service or Redis reachable from the host | `docker-compose.yml`, `tests/test_compose_port_exposure.py` |
| PHI-safe logging with redaction and a filter backstop | `libs/safe_logging` |
| SSN never returned to the browser for name resolution | `frontend/app/api/patients/[id]/name/route.ts` |

**What they do not cover:** anyone with database access, a database backup, or
the underlying volume reads every SSN and clinical note in plain text. Postgres
is still published to the host (`5432`), which widens that surface — tracked as
an open item in `tests/test_compose_port_exposure.py`.

## Remediation plan

| Step | Detail |
|---|---|
| 1 | Choose the layer: application-level column encryption, `pgcrypto`, or volume-level once a deployment target exists |
| 2 | Replace `ssn_digits` with a keyed digest (e.g. HMAC-SHA256 under a managed key) preserving deterministic match without a plaintext index |
| 3 | Rework `_find_match_candidates` in intake-service against the digest; re-verify RIV-160 duplicate detection end to end |
| 4 | Encrypt `ssn`, `notes`, `dob`; verify no plaintext remains in any index |
| 5 | Re-examine the RAG corpus, which embeds record text |

**Owner: unassigned.** There is no `CODEOWNERS` and no internal team named
anywhere in this repository — itself an open governance question across three
reporting cycles. This plan cannot be scheduled until someone owns it.

**Prerequisite:** step 1 is blocked on the deployment target being named. That
decision belongs to the client.

## Consequences

- The system must not be described as encrypting PHI or as HIPAA compliant.
  `README.md` and `db/schema.sql` now say so directly.
- This is a **stated, dated, accepted gap with a remediation plan** — the form
  the Security Rule asks for when an addressable specification is not
  implemented.
- No tests accompany this decision, deliberately: there is no control to test,
  and a test here would imply protection exists.
- The client must be told plainly. A covered entity cannot carry this risk
  without knowing it, and the previous README line means they may currently
  believe the opposite.

## Alternatives considered

**Encrypt `notes` and `ssn` only, with a keyed digest for matching.** The
correct eventual answer, and rejected only on timing — it is steps 2–4 above
compressed into six days alongside the rest of the compliance closure, against
a shipped match-key control.

**Claim volume-level encryption and produce a configuration reference.**
Rejected: it documents infrastructure nobody has verified exists, which is
precisely the failure being corrected here.

**Leave the claims and defer the whole area.** Rejected outright. The
implementation gap is arguable; the false statement is not.
