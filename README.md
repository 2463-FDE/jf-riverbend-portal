PHI in this system is **not** encrypted at rest, and the system is **not** HIPAA compliant. See Compliance below and `adr/0008`.

# Riverbend Patient Portal

Patient intake + records portal for **Riverbend Community Health**, a multi-clinic
community health network. Patients self-register, front-desk staff verify insurance
eligibility, clinicians view records, schedulers book appointments, and ROI clerks
process release-of-information requests.

Built by Helix Digital Partners under contract. Handed off as-is.

## Architecture

```
                       ┌─────────────────────────┐
  Next.js portal  ───► │   gateway (FastAPI BFF)  │  login / sessions
  (frontend/)          └────────────┬────────────┘
                                    │
   ┌──────────┬──────────┬─────────┼──────────┬──────────────┬───────────┐
   ▼          ▼          ▼         ▼          ▼              ▼           ▼
 intake-   eligibility- records-  scheduling- interop-      roi-
 service    service     service   service     service       service
(register, (270/271    (records  (Appointment (HL7 v2      (release of
 consent)   payer)      façade)   / Slot)      hospital     information)
   │                       │      │            feed)            │
   ▼                       ▼      ▼                             ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │  Postgres (users, patients, encounters, records, appointments,     │
   │  slots, insurance, consents, audit_logs, roi_requests, disclosures)│
   │  Redis (sessions, cache)                                           │
   └──────────────────────────────────────────────────────────────────┘
```

| Service | Stack | Port | Purpose |
|---------|-------|------|---------|
| `frontend/` | Next.js 15 (App Router, TS) | 3070 | Provider-facing patient portal |
| `services/gateway/` | FastAPI + Postgres + Redis | 8070 | BFF / API gateway, login + sessions |
| `services/intake-service/` | FastAPI + Postgres | 8071 | Registration, insurance, consent, eligibility trigger |
| `services/eligibility-service/` | FastAPI | 8072 | Payer eligibility (X12 270/271) |
| `services/records-service/` | FastAPI + Postgres | 8073 | Patient + records read façade |
| `services/scheduling-service/` | FastAPI + Postgres | 8074 | Appointments / slots |
| `services/interop-service/` | FastAPI | 8075 | HL7 v2 ingest from hospital feed |
| `services/roi-service/` | FastAPI + Postgres | 8076 | Release-of-information / disclosures |

There is no shared Python library yet — services repeat the `config.py` / `db.py`
/ `models.py` / `logging_config.py` pattern by copy-paste (see `adr/0001`).

## Quick start

```bash
cp .env.example .env   # an .env with working dev credentials is already committed
make up                # docker compose up everything (Postgres seeds on first boot)
make seed              # load schema + demo data into an EMPTY db; a fresh
                       # volume already self-seeds, and this FAILS with
                       # duplicate-key errors against a populated one —
                       # to genuinely reload: docker compose down -v && make up
```

Portal: http://localhost:3070  ·  Gateway docs: http://localhost:8070/docs

**Demo logins** (all seeded with password `portal123`): `frontdesk`, `drnguyen`,
`roiclerk`, `mokonkwo`, … (see `db/seed/generate_seed.py`).

## Data + migrations

- `db/schema.sql` — flattened current schema, loaded on a fresh Postgres volume.
- `db/migrations/00N_*.sql` — ordered, forward-only migration history (hand-rolled,
  kept in sync with `schema.sql`).
- `db/seed/generate_seed.py` — deterministic generator → `db/seed/seed.sql`
  (~250 patients, hundreds of encounters/records/appointments).

## Tests

```bash
pip install -r requirements-dev.txt
pytest -m "not integration"     # unit tests (no infra)
pytest -m integration           # needs Postgres + Redis up
```

Coverage is uneven — the happy paths are covered, several security/edge paths
are not. See `tests/README.md`.

## Compliance

Riverbend Community Health is a HIPAA covered entity. **This application does
not currently meet the Security Rule.** The claim previously on this line —
that patient data was encrypted and the system was compliant — was false:
`dob`, `ssn` and clinical `notes` are stored in plain text (`db/schema.sql`).

What is implemented and tested:

- per-user authentication with a nine-role permission grid (`config/roles.yaml`)
- per-(actor, patient) authorization enforced at the data boundary, failing
  closed (`services/records-service/patient_access_gate.py`)
- clinical notes withheld from roles that may not read them
- caller verification between every service, refusing untokened in-network calls
- PHI-safe logging with redaction (`libs/safe_logging`)
- a default-deny clinician review gate over withheld patient-facing content

What is not:

- **no encryption at rest** — see `adr/0008` for the recorded risk decision
- no TLS on any hop
- no MFA outside a pilot-clinic scope
- no tamper-evident audit trail (audit rows are recorded, not protected)
- no signed-authorization check before a release of information
- one shared database credential; no deployment pipeline; no backup or
  recovery process; no security scanning in CI

Administrative, physical and organizational safeguards — risk analysis,
workforce training, sanction and breach policies, and Business Associate
Agreements — are the covered entity's responsibility and are **not evidenced in
this repository**. Technical controls alone do not make a system compliant.

See `adr/0002-data-and-compliance.md` and `adr/0008-phi-encryption-at-rest.md`.

---
Helix Digital Partners · handoff build · v1.4.0
