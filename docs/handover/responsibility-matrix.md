# Responsibility matrix — OWNER UNASSIGNED

**Date:** 2026-08-21 · **Status:** every owner unassigned, deliberately

Week 9 asks that outstanding items are merged or carry a blocker card with a
moved estimate and a **named owner**. This repository names no owner for any
area: there is no `CODEOWNERS`, and no internal team, role or individual is
identified anywhere in the code, ADRs or docs. That gap has been open across
three reporting cycles.

**Every row below reads `OWNER UNASSIGNED`, and that is the point.** Inventing a
plausible name would satisfy the template and mislead whoever inherits this on
Monday — they would page someone who never agreed to be paged. The client
supplies these names, or the rows stay unassigned and visible.

Replace a row's owner only with a name the client has actually given.

## Areas

| Area | Repository surface | Owner | Notes |
|---|---|---|---|
| Gateway / authN + authZ | `services/gateway/` | OWNER UNASSIGNED | Session policy, RBAC grid loading, all outbound internal-token forwarding |
| Records + patient authorization | `services/records-service/` | OWNER UNASSIGNED | `patient_access_gate.py` is the RIV-201 control; the highest-risk file in the repo |
| Patient portal (invitation → summary → review) | `services/records-service/{patient_summary,review_queue}.py`, `frontend/app/{my-results,review-queue}` | OWNER UNASSIGNED | The purchased product |
| Intake + duplicate matching | `services/intake-service/` | OWNER UNASSIGNED | RIV-160 / `adr/0004` match key |
| Eligibility | `services/eligibility-service/`, `libs/eligibility_agent/` | OWNER UNASSIGNED | Async + circuit breaker; needs a payer credential to settle |
| Scheduling | `services/scheduling-service/` | OWNER UNASSIGNED | Idempotency keys, booking constraints |
| HL7 interop | `services/interop-service/` | OWNER UNASSIGNED | Known gap: AL1/RXA dropped (`tests/test_hl7_parser.py` xfail) |
| Release of information | `services/roi-service/` | OWNER UNASSIGNED | No signed-authorization check; no accounting of disclosures |
| Roster / role migration | `db/migrations/scripts/roster_dry_run.py`, `db/seed/staff_roster_SYNTHETIC.csv` | OWNER UNASSIGNED | Client roster received 2026-08-19 |
| Database schema + migrations | `db/schema.sql`, `db/migrations/` | OWNER UNASSIGNED | `schema.sql` is hand-maintained alongside forward migrations |
| Secrets + configuration | `.env.example`, `docker-compose.yml`, `adr/0007` | OWNER UNASSIGNED | Rotation of the three disclosed values is outstanding |
| CI | `.github/workflows/ci.yml` | OWNER UNASSIGNED | No dependency, container or secret scanning |
| Deployment + operations | *nothing in repo* | OWNER UNASSIGNED | **No deploy step exists anywhere.** See blocker B-1 |
| Backup / recovery | *nothing in repo* | OWNER UNASSIGNED | No `pg_dump`, no restore path |
| Frontend | `frontend/` | OWNER UNASSIGNED | Nine screens |

## Blocker cards

Each carries what is blocked, who must resolve it, and a moved estimate. **No
estimate here is a commitment**, because none of these has an owner to commit.

### B-1 · Deployment target unknown
- **Blocks:** encryption-at-rest evidence via a managed volume, TLS termination,
  backup/recovery design, per-service database credentials.
- **Resolver:** client. Open across three reporting cycles.
- **Moved estimate:** unschedulable until answered. AWS Bedrock has been named
  as the model provider, which does not say where Postgres or the services run.

### B-2 · Payer clearinghouse vendor identity and BAA status
- **Blocks:** the vendor-governance memo; eligibility cannot settle past
  `pending` without a payer credential.
- **Resolver:** client.
- **Moved estimate:** unschedulable.

### B-3 · No operational owner for any area
- **Blocks:** every row above; escalation after handover; W9's own acceptance
  criterion.
- **Resolver:** client.
- **Moved estimate:** unschedulable. This card is the reason this document
  exists rather than a `CODEOWNERS` file.

### B-4 · Runtime verification unavailable in the working environment
- **Blocks:** the integration/acceptance suite (95 tests collect but do not
  run), the five unbaselined client metrics, and any live denial proof.
- **Resolver:** local environment — `docker compose config` currently fails on a
  missing `INTERNAL_SERVICE_TOKEN`, which is set locally and never committed.
- **Moved estimate:** hours once the stack starts; the tests exist.

### B-5 · Week 8 scope conflict
- **Blocks:** knowing what Week 8 delivers. `Weekly-Deliverables-updatedAug10.docx`
  assigns a Safe-Harbor de-identification scrub, a data-flow/BAA memo and a
  recommendation gate. The planning skill lists de-identification and vendor
  assurance as explicitly out of cycle and plans compliance/roster work instead.
- **Resolver:** Jorge, then client.
- **Moved estimate:** the de-identification scrub is days of work and has not
  started.

## What someone inheriting this on Monday needs first

1. `docs/runbook.md` — the stack does not start without a locally-set
   `INTERNAL_SERVICE_TOKEN`; that is documented, not a defect.
2. `README.md` states the actual posture. **PHI is not encrypted at rest and
   this system is not HIPAA compliant.** Earlier versions claimed otherwise;
   `adr/0008` records why, and `tests/test_compliance_claims.py` fails if the
   false claims return.
3. `adr/` 0001–0008 in order. Every decision that matters is there.
4. `db/seed/README-staff-roster.md` before touching the roster migration.
5. The three disclosed secrets in `adr/0007` still need rotating.
