# Responsibility matrix — training environment

**Date:** 2026-08-21 · **Status:** operational ownership **not applicable**

> **Scope decision, 2026-08-21.** This is a synthetic training project. There is
> **no production hosting target** — local Docker Compose only — and **no
> production operational handover or on-call ownership is in scope.** Bedrock is
> used solely as an external model-inference provider for synthetic-data
> testing; it hosts neither the application nor the database.
>
> `CODEOWNERS` and operational ownership are therefore **not applicable**, not
> merely unassigned. The rows below name the code area a future maintainer would
> read first, which is the part that remains useful.

Week 9 asks that outstanding items are merged or carry a blocker card with a
moved estimate and a named owner. In a training environment with no production
deployment, "owner" has no operational meaning: there is no on-call rotation to
join and no incident for anyone to be paged about.

So every row reads **N/A (training)** rather than a name or a placeholder. The
column is retained because the *code areas* are real and a future maintainer
needs the map; only the operational ownership is out of scope.

If this project were ever deployed for real use, every row would need a named
owner before that happened. That is recorded here as the condition, not as an
outstanding request.

## Areas

| Area | Repository surface | Owner | Notes |
|---|---|---|---|
| Gateway / authN + authZ | `services/gateway/` | N/A (training) | Session policy, RBAC grid loading, all outbound internal-token forwarding |
| Records + patient authorization | `services/records-service/` | N/A (training) | `patient_access_gate.py` is the RIV-201 control; the highest-risk file in the repo |
| Patient portal (invitation → summary → review) | `services/records-service/{patient_summary,review_queue}.py`, `frontend/app/{my-results,review-queue}` | N/A (training) | The purchased product |
| Intake + duplicate matching | `services/intake-service/` | N/A (training) | RIV-160 / `adr/0004` match key |
| Eligibility | `services/eligibility-service/`, `libs/eligibility_agent/` | N/A (training) | Async + circuit breaker. **Simulated** — no payer vendor or endpoint exists (B-2) |
| Scheduling | `services/scheduling-service/` | N/A (training) | Idempotency keys, booking constraints |
| HL7 interop | `services/interop-service/` | N/A (training) | Known gap: AL1/RXA dropped (`tests/test_hl7_parser.py` xfail) |
| Release of information | `services/roi-service/` | N/A (training) | No signed-authorization check; no accounting of disclosures |
| Roster / role migration | `db/migrations/scripts/roster_dry_run.py`, `db/seed/staff_roster_SYNTHETIC.csv` | N/A (training) | Client roster received 2026-08-19 |
| Database schema + migrations | `db/schema.sql`, `db/migrations/` | N/A (training) | `schema.sql` is hand-maintained alongside forward migrations |
| Secrets + configuration | `.env.example`, `docker-compose.yml`, `adr/0007` | N/A (training) | `.env` untracked; template credentials blank. `PAYER_API_KEY` stays blank by decision |
| CI | `.github/workflows/ci.yml` | N/A (training) | No dependency, container or secret scanning |
| Deployment + operations | *nothing in repo* | N/A (training) | No deploy step, by design — local Docker Compose only (B-1) |
| Backup / recovery | *nothing in repo* | N/A (training) | Out of scope: no production data to recover (B-1) |
| Frontend | `frontend/` | N/A (training) | Nine screens |

## Blocker cards

Three of the five cards previously here were **closed by scope decisions on
2026-08-21** rather than resolved by work. They are kept, marked closed, because
a card that silently disappears looks like it was done.

### B-1 · Deployment target — **CLOSED, not applicable**
- **Was:** blocking encryption-at-rest evidence, TLS termination, backup design
  and per-service database credentials.
- **Resolution:** there is no production hosting target. **Local Docker Compose
  only.** TLS, backup/recovery and production credential separation are
  therefore out of scope for this project, and must not be described as gaps
  against a deployment that does not exist.
- **Still true:** if this were ever deployed for real use, all four would be
  prerequisites.

### B-2 · Payer clearinghouse and BAA — **CLOSED, not applicable**
- **Was:** blocking the vendor-governance memo; eligibility cannot settle.
- **Resolution:** **no payer clearinghouse, live endpoint or real payer data
  exists in this simulation.** `PAYER_API_KEY` stays blank and eligibility
  behaviour is labelled simulated. **No BAA is executed, and none is represented
  as required for a real integration**, because there is no real integration.
- **Open engineering item, not a blocker:** the placeholder endpoint is still
  wired to a live `httpx` call, so an eligibility check attempts a real outbound
  request to a reserved domain and fails. That needs a simulation mode; it is
  work, not a client question.

### B-3 · Operational ownership — **CLOSED, not applicable**
- **Was:** no owner named for any area.
- **Resolution:** no production operational handover or on-call ownership is in
  scope for a training environment. `CODEOWNERS` is not applicable.

### B-4 · Runtime verification — **OPEN**
- **Blocks:** the integration/acceptance suite (95 tests collect), the five
  unbaselined client metrics, and every live denial proof.
- **Resolver:** local environment. `docker compose config` passes as of
  2026-08-21; the remaining step is starting the stack and running the suite.
- **Moved estimate:** hours. The tests exist.

### B-5 · Week 8 scope — **RESOLVED**
- **Was:** the deliverables document assigns a Safe-Harbor de-identification
  scrub, a data-flow/BAA memo and a recommendation gate; the planning skill
  listed de-identification as out of cycle.
- **Resolution:** both tracks, minimal each (2026-08-21). The scrub and the
  recommendation gate are built; the memo is outstanding and must be written to
  the simulation scope above — describing the pattern, not asserting a live
  disclosure or an executed agreement.

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
