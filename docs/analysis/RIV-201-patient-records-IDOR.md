# RIV-201 — Cross-Patient Records IDOR

- **Status:** Confirmed, unremediated. Documented debt, not new.
- **Tracking:** Referred to as `RIV-201` in `services/gateway/app.py:130`,
  `.github/workflows/ci.yml:68`, `adr/0005` §"Outstanding considerations",
  `tests/README.md`, and as finding `AUD-02` in both prior system audits
  (`docs/analysis/system-audit-07-01-2026.md`,
  `docs/analysis/system-audit-07-18-2026.md`). It is **not** one of the four
  tickets exported from the contractor's own Jira board in
  `docs/handover/jira-tickets.md` (RIV-088, RIV-141, RIV-160, RIV-175) — those
  are end-user-reported symptoms. RIV-201 is the internal ID the codebase and
  audits already use for this specific authorization gap; this document is the
  first dedicated write-up of it rather than a passing mention.
- **Severity:** Critical (per system audit AUD-02) — full-chart PHI exposure
  (demographics, SSN, DOB, clinical notes, lab results) to any authenticated
  account, for any patient, with no per-patient authorization check anywhere
  in the request path.

## 1. Authentication vs. authorization (kept separate deliberately)

These are two different, independently-verified properties. RIV-201 is an
**authorization** gap; authentication is intact:

- **Authentication — present.** `services/gateway/app.py:57-62`
  (`require_session`) rejects any request without a valid, non-expired-looking
  Redis session token. `tests/integration/test_records_flow.py:28-31`
  (`test_records_require_authentication`) confirms a request with no bearer
  token gets `401`.
- **Authorization — absent.** Once a session exists, nothing checks whether
  the session's owner is entitled to the specific `patient_id` in the URL.
  `services/gateway/app.py:176-180` (`proxy_records`) forwards
  `GET /patients/{patient_id}/records` to `records-service` unconditionally,
  with a comment already admitting this:

  ```python
  @app.get("/patients/{patient_id}/records")
  def proxy_records(patient_id: int, session: dict = Depends(require_session)):
      # IDOR: a valid session is required, but it is never checked against
      # {patient_id}. {patient_id} is the sequential primary key.
      return _get("records", f"/patients/{patient_id}/records")
  ```

  `services/records-service/app.py:86-134`
  (`get_patient_records`, marked `DEBT D11`) has no caller-identity parameter
  or dependency at all — it will assemble and return a full chart for any
  integer `patient_id` that exists, with no concept of "whose session is this."

  There is no structural reason this is hard to trigger: `patients.id` is a
  `SERIAL PRIMARY KEY` (`db/schema.sql:29`, comment: "sequential, exposed in
  record URLs"), so IDs are dense, small integers assigned in insert order —
  no token, GUID, or per-patient secret stands between "I am logged in" and
  "I can read chart N."

- **Root cause, in one sentence:** `config/roles.yaml` gives every account the
  same flat `staff` role (with `records.read`/`records.write` permissions
  granted unconditionally), and `SESSION_TIMEOUT: never` in
  `services/gateway/auth.yaml` means a session, once issued, authorizes that
  same unrestricted access indefinitely. There is no per-action, per-object
  authorization layer to add a patient-scope check to — one would have to be
  built, not just "turned on."

## 2. Source evidence

| # | File:lines | What it shows |
|---|---|---|
| 1 | `services/gateway/app.py:57-62` | `require_session` authenticates only; no patient binding exists to check. |
| 2 | `services/gateway/app.py:176-180` | `proxy_records` forwards `patient_id` to records-service with zero authorization check; comment self-documents the IDOR. |
| 3 | `services/records-service/app.py:86-134` | `get_patient_records` (`DEBT D11`) has no caller-identity dependency; assembles any patient's full chart on request. |
| 4 | `db/schema.sql:28-41` | `patients.id` is a plain sequential `SERIAL`, explicitly commented as "exposed in record URLs." |
| 5 | `config/roles.yaml` | Single flat `staff` role for every account; `records.read` is an unconditional permission, not scoped to "own patients." |
| 6 | `services/gateway/auth.yaml` | `SESSION_TIMEOUT: never` — a compromised or shared session carries this same unrestricted access forever. |
| 7 | `tests/integration/test_records_flow.py:41-50` | `test_user_cannot_read_other_patients_chart` is `xfail(strict=False)` — the test that should catch this is present, documented, and expected to fail; not silently missing. |
| 8 | `.github/workflows/ci.yml:56-69` | Integration tests (where the above `xfail` lives) are excluded from CI (`pytest -m "not integration"`) — this gap does not run on every push. |
| 9 | `docs/analysis/system-audit-07-01-2026.md`, `-07-18-2026.md` | Finding AUD-02, confirmed Critical, confirmed unchanged across two audit passes (2026-07-01 → 2026-07-18). |

## 3. Sanitized reproduction (seeded data only)

`docs/handover/portal.har` contains two prior, already-authenticated requests
against seeded demo patient IDs. Only method, sanitized path, and status are
reproduced below — **no authorization headers, cookies, tokens, response
bodies, or names were extracted from the HAR or are reproduced anywhere in
this document**:

```
GET /api/patients/1042/records   -> 200
GET /api/patients/1043/records   -> 200
```

Both requests succeeded under whatever single session originated them. That
alone does not prove IDOR — a legitimate front-desk session could plausibly
have a business reason to view either chart once. The actual defect is that
**no request-time check exists that could ever tell those two cases apart**:
the code path for patient 1042 and patient 1043 is identical, and neither the
gateway nor records-service has a data point to compare `patient_id` against.

The deterministic, safe way to demonstrate this without touching any real or
HAR-derived credential is the integration test already in the repo:

```bash
make up
pytest -m integration tests/integration/test_records_flow.py -v
```

Expected, current behavior:

- `test_authenticated_user_can_read_a_chart` — passes. A `frontdesk` session
  can read seeded patient `1042`'s chart (`patients.csv` row: Maria Gonzalez,
  seeded demo record, not a real person).
- `test_user_cannot_read_other_patients_chart` — **xfail, non-strict.** The
  same `frontdesk` session is used to fetch seeded patient `1043`'s chart
  (James O'Brien, also seeded demo data). The test asserts `403` and gets
  `200` — i.e., an unrelated chart is returned with no error, using only
  usernames/passwords/IDs that already exist in the repo's own seed data
  (`db/seed/patients.csv`, `db/seed/generate_seed.py`). No new sample PHI was
  fabricated for this write-up.

This document does not run that reproduction as part of Stage 1 — Stage 1 is
documentation-only. The command above is provided so the finding can be
verified independently, using only committed seed fixtures.

## 4. Impact

- **Scope:** every patient record in the system, every authenticated account
  (front desk, billing, ROI clerks, clinicians — `config/roles.yaml` gives
  all of them the same permissions), reachable simply by incrementing an
  integer in a URL.
- **Data exposed:** full chart — demographics, SSN, DOB (`patients` table,
  plaintext per `adr/0002`), encounter summaries, allergies/medications
  free text, and every clinical record body (lab results, notes, imaging
  reports) tied to that patient.
- **No detection:** `audit_logs` (`db/schema.sql:131-137`) is a mutable
  request-dump table with no per-patient access accounting; per
  `docs/handover/auditor-questionnaire.md`, staff were already unable to
  answer a real auditor's request for a disclosure/access accounting. An
  enumeration attack using this path would leave no reliable, tamper-evident
  trail.
- **Regulatory:** unauthorized PHI access at this scope is a 45 CFR 164.312(a)
  access-control gap distinct from the ROI/164.508 gap tracked separately
  under AUD-03.

## 5. Containment (interim, non-code guidance only)

This document does not change code or configuration. If interim containment
is wanted before a real fix ships, options to bring to the team for a decision
(not implemented here) include: restricting which network principals can
reach `records-service:8073` directly (it is currently published to the host
per `docker-compose.yml`, compounding this with AUD-01), and tightening
monitoring/alerting on `records-service` request volume and ID-sequence
access patterns as a stopgap detective control. Neither is a substitute for
authorization and neither is evaluated in depth here.

## 6. Remediation sketch (not implemented in this document)

A real fix requires an authorization decision at or before the point of
retrieval, using an already-trustworthy ownership claim — not a client-
supplied `patient_id` and not an inference from the current flat `staff`
role, which has no per-patient concept at all. At minimum this means:

1. A patient-ownership or care-team-membership fact that a session can be
   checked against (does not exist today — `users` has no relationship to
   `patients`).
2. A deny-by-default check in the gateway and/or records-service, executed
   **before** `get_patient_records` runs any query, not as a post-hoc filter
   on the response.
3. A real, append-only access record so an auditor's disclosure-accounting
   question can be answered — the current `audit_logs` table cannot support
   this (mutable, soft-delete only, per `db/schema.sql:126-137`).
4. Making `test_user_cannot_read_other_patients_chart` pass for the right
   reason, then running it in CI (it currently doesn't run there at all —
   see gap below), removing the `xfail` marker.

Week 4's graph-boundary authorization port (Stage 2 of this plan) is a
**defense-in-depth prototype layer only**. It demonstrates what a
deterministic, fail-closed patient-scope check looks like and enforces it for
the new graph/agent code path. It intentionally does **not** touch
`services/gateway/app.py` or `services/records-service/app.py`, and it must
never be described as having fixed or mitigated RIV-201/AUD-02 in the actual
gateway or records-service endpoints. Those remain exploitable exactly as
described above until they are remediated directly.

## 7. Test gap

- `tests/integration/test_records_flow.py:41-50` already encodes the correct
  expected behavior (`403` on cross-patient access) and is `xfail(strict=False)`
  — it fails today (proving the bug) but won't break the suite if the bug
  disappears without anyone updating the marker, and won't flip the suite red
  either way.
- That test is an **integration** test and is excluded from every CI run
  (`.github/workflows/ci.yml:56-69` runs `pytest -m "not integration"` only).
  So today, RIV-201 has a codified regression test, and that test never runs
  automatically anywhere.
- No unit-level test exists that could catch this without live infrastructure,
  because there is no authorization module or seam to unit test against yet
  — that seam is what Stage 2 introduces, scoped to the new graph code path
  only.

## Non-goals of this document

This is a finding write-up, not a fix. It changes no application code, test
file, configuration, or database object. It does not claim the Week 4
graph/agent prototype remediates this finding.
