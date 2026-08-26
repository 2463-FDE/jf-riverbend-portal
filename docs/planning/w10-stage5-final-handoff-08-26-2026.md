# W10 Stage 5 — final exact-main handoff (2026-08-26)

**Status:** working-tree evidence at authoring time; open for review as PR
#81. **`6c7fc49` is the evidence baseline this report was produced
against — every command, freshness check, and rehearsal below ran on that
exact commit.** That does not make W10 complete by itself: **W10 closes
only when PR #81 merges**, at which point this report is amended once more
(§11) with the actual merge commit.

**PR #82 (the runbook/Makefile documentation corrections, §5) is explicitly
outside W10's closure condition.** Those two gaps are pre-existing
documentation debt this Stage 5 pass *discovered* while validating links
and instructions, not new work this stage created, and neither blocks any
of Stage 5's own acceptance criteria (CI, freshness, the two rehearsals, the
controlled fallback test, the before/after result). W10 closing on #81's
merge does not mean §5's two items are resolved — §8's table marks them
separately, and §9's roadmap items 3–4 remain open until #82 itself merges.

This report is reproducible from `6c7fc49` using the commands and
credentials recorded below. It does not implement any new feature — Stage 5
is verification and documentation only, per `w-10-planner`'s own operating
rules.

## 1. Exact-main CI

CI ran automatically on the merge of PR #80 (Week 9 reconciliation) —
[run 32918126756](https://github.com/2463-FDE/jf-riverbend-portal/actions/runs/32918126756),
commit `6c7fc49`, conclusion **success**. Not re-run separately; the merge
commit and the reported commit are the same commit.

## 2. RAG corpus freshness — confirmed, not re-ingested

```bash
docker compose up -d postgres
bash -c 'set -a; source .env; set +a; export DB_HOST=localhost; python3 db/policy_corpus_evaluate.py --verify-only'
```

Result: `fresh: true`, 15 active documents, 207/207 chunks, one embedding row
per chunk, `bedrock` / `amazon.titan-embed-text-v2:0`, dimension 1024, zero
mismatches, zero `fake-titan` rows (`policy_chunk_embeddings` totals exactly
207 rows, all `bedrock`). No re-ingestion was run or needed.

## 3. Integrated synthetic demo — rehearsed twice

Rehearsed via direct `curl` calls to the gateway API (`http://localhost:8070`)
with real session logins — not a browser walkthrough. All demo-account
passwords below are the repo's own seeded, synthetic, non-production
credentials. `jq` is used only to extract the bearer token from each login
response; every other step is a single `curl`.

**Setup for each rehearsal:**
```bash
docker compose up -d
make demo-reset
```

**Rehearsal 1 — patient 1738, clinician `drkim` — full replay:**

```bash
BASE=http://localhost:8070

# 1. Clinician login
KIM=$(curl -s -X POST $BASE/login -H 'Content-Type: application/json' \
  -d '{"username":"drkim","password":"portal123"}' | jq -r .token)

# 2. Generate a real, cited draft
curl -s -X POST $BASE/patients/1738/agent-draft \
  -H "Authorization: Bearer $KIM" | tee /tmp/draft.json
# -> 201, {"id":1,"version":1,"status":"validated","provenance_label":"real",
#    "model_id":"us.anthropic.claude-sonnet-4-6",
#    "citations":[{"citation_id":"POL-001@2026-08-01",...},
#                 {"citation_id":"TRN-014@2026-07-15",...}], ...}
DRAFT_ID=$(jq -r .id /tmp/draft.json)

# 3. Clinician approval
curl -s -X POST $BASE/agent-drafts/$DRAFT_ID/decision \
  -H "Authorization: Bearer $KIM" -H 'Content-Type: application/json' \
  -d '{"decision":"approved"}'
# -> 200, {"status":"approved", "provenance_label":"real", ...}

# 4. Patient login (note: portalportal123, NOT portal123 -- see PR #82)
PATIENT=$(curl -s -X POST $BASE/login -H 'Content-Type: application/json' \
  -d '{"username":"patient-1738","password":"portalportal123"}' | jq -r .token)

# 5. Patient views the approved-only summary
curl -s $BASE/patient/me/agent-summary -H "Authorization: Bearer $PATIENT"
# -> 200, {"status":"approved","provenance_label":"real","version":1,
#    "citations":[...same two as step 2...], "generated_text":"..."}
# Exact approved-only display: no pending/rejected content leaks through.

# 6. Clinician asks a real policy question
curl -s -X POST $BASE/policy/ask -H "Authorization: Bearer $KIM" \
  -H 'Content-Type: application/json' \
  -d '{"question":"What must a clinician confirm before releasing a critical lab result early?"}'
# Observed: 200, but {"label":"fallback","termination_reason":"provider_error",...}
# records-service log: error_type=GraphRecursionError -- the LangChain agent
# loop hit its turn bound without converging to a final answer. Caught by
# the generic exception handler in run_policy_navigator; returned the safe,
# truthful, generic reply with no citations and no raw error exposed.
# CLASSIFIED AS INTERMITTENT BOUNDED-LOOP EXHAUSTION -- an occasional, real
# failure mode of a bounded agent loop under load, not a configuration
# problem; it did not recur in rehearsal 2 against the identical question.
# This is an incidental, genuine observation of the fallback contract --
# it is NOT the planned provider-failure exercise (that is the separate,
# controlled test in §4).

# 7. Patient asks an out-of-their-workflow question
curl -s -X POST $BASE/policy/ask -H "Authorization: Bearer $PATIENT" \
  -H 'Content-Type: application/json' \
  -d '{"question":"What is the minimum-necessary rule for ROI disclosure record selection?"}'
# -> 200, {"label":"real","termination_reason":"answered",
#    "citations":[{"citation_id":"GUIDE-REC-ACCESS-001@1.1#explicit-role-boundaries",...},
#                 {"citation_id":"GUIDE-INTAKE-CONSENT-001@1.0#...",...}]}
# NOT ROI-DISC-001 (the ROI-clerk-scoped source) -- the retrieval boundary
# held: the patient got a real, scoped answer from material actually inside
# their own authorized audience, never the ROI-specific document.

# 8. Front-desk login
FRONTDESK=$(curl -s -X POST $BASE/login -H 'Content-Type: application/json' \
  -d '{"username":"frontdesk","password":"portal123"}' | jq -r .token)

# 9. Coverage load
curl -s $BASE/patients/1738/coverages -H "Authorization: Bearer $FRONTDESK"
# -> 200, {"items":[{"id":6,"status":"stale","payer_name":"Aetna"}]}

# 10. Eligibility chat -- {id} is obtained from this same appointments call,
#     never supplied by the browser/caller directly
curl -s "$BASE/appointments?patient_id=1738" -H "Authorization: Bearer $FRONTDESK" \
  | tee /tmp/appts.json
VISIT_ID=$(jq -r '.[0].id' /tmp/appts.json)   # 2 rows returned; first used here

curl -s -X POST $BASE/visits/$VISIT_ID/messages -H "Authorization: Bearer $FRONTDESK" \
  -H 'Content-Type: application/json' \
  -d '{"message":"Is this patient'"'"'s coverage still active?"}'
# -> 200, {"termination_reason":"answered", "reply":"...Payer: Aetna...
#    Stored Status: Stale...Live Verification: unavailable..."} -- a real,
# substantive reply naming the payer, masked member ID, and stored/live
# verification status.
```

**Rehearsal 2 — patient 1739, clinician `drnguyen` — same script, three
substitutions:** `drkim`→`drnguyen`, `1738`→`1739`, `patient-1738`→
`patient-1739`. Observed differences from rehearsal 1's output: step 2's
draft got `id=2`; step 6 (clinician policy question) this time returned
`label=real`, `termination_reason=answered`, citing
`LAB-REL-EXCEPTION-001` (the clinician-only early-release companion) and
`LAB-REL-001` — the `GraphRecursionError` from rehearsal 1 did not recur on
the identical question; step 9 coverage returned `status=unknown`,
`payer_name=UnitedHealthcare`; step 10 eligibility chat again `answered`,
correctly reporting it could not confirm live status and showing the
stored/masked data instead. Step 7 (patient ROI-boundary question) again
`answered` from in-scope role-boundary guidance, never `ROI-DISC-001`.

## 4. Provider-failure/fallback path — the planned, controlled exercise

The original plan was to use the eligibility chat's documented fail-closed
behavior for an unset `ELIGIBILITY_AGENT_RUNTIME`. That assumption was
**wrong**, corrected here rather than silently dropped:
`libs/eligibility_agent/runtime.py:50` defaults an unset
`ELIGIBILITY_AGENT_RUNTIME` to `"raw_bedrock"` (the working default), not a
fail-closed refusal — `docs/runbook.md`'s framing of this ("expect every
chat turn to return `termination_reason=provider_error`... until a real
model id/region/credential is configured") describes the state before this
environment's `.env` was configured with real Bedrock credentials, and is
now stale for this specific environment. Both rehearsals' eligibility chat
calls genuinely answered — that path could not serve as the fallback
exercise.

**The planned fallback exercise is a controlled, one-off provider-not-
configured test against the real policy-navigator runtime**, run without
editing `.env`:

`services/records-service/policy_navigator_path.py::ask_policy_navigator`
was called directly (real code, real Postgres, real embedding
provider/retrieval — only the chat model's `BEDROCK_MODEL_ID` was
overridden to `changeme` for this one container invocation) with a real
clinician question. Full, exact reproduction from `6c7fc49` — no file in
this repo needed, the script is written to disk and run in one step:

```bash
cat > /tmp/check.py <<'PYEOF'
import os
import sys

sys.path.insert(0, "/app")

from policy_navigator_path import ask_policy_navigator  # noqa: E402

assert os.getenv("BEDROCK_MODEL_ID") == "changeme", "test setup error: BEDROCK_MODEL_ID must be changeme"

result = ask_policy_navigator(
    "What must a clinician confirm before releasing a critical lab result early?",
    actor_role="clinician",
)

print("label:", result.label)
print("termination_reason:", result.termination_reason)
print("model_id:", result.model_id)
print("citations:", result.citations)

assert result.label == "fallback", f"expected label=fallback, got {result.label}"
assert result.termination_reason == "provider_error", f"expected termination_reason=provider_error, got {result.termination_reason}"
assert result.model_id is None, f"expected model_id=None, got {result.model_id}"
assert result.citations == (), f"expected zero citations, got {result.citations}"
print("ALL ASSERTIONS PASSED")
PYEOF

docker compose up -d postgres
docker compose run --rm -e BEDROCK_MODEL_ID=changeme \
  -v "/tmp/check.py:/tmp/check.py:ro" \
  records-service python /tmp/check.py
```

Observed and asserted:

```
label: fallback
termination_reason: provider_error
model_id: None
citations: ()
```

Records-service log showed `error_type=ProviderNotConfigured` — the exact,
intentional guard in `run_policy_navigator::_default_model()` that raises
before any network call when `BEDROCK_MODEL_ID` is unset or `"changeme"`.
Embedding/retrieval succeeded first (unaffected by the override), then
chat-model construction failed exactly as designed, and the safe fallback
was returned with no citations and no model id — never a raw error.

This, not the organic `GraphRecursionError` in §3 step 6, is the reported
planned provider-failure/fallback exercise for this handoff: deliberate,
reproducible on demand, isolated to one container invocation, and it never
touched `.env`. The `GraphRecursionError` remains recorded in §3 as a real,
separately-classified, intermittent observation of the same underlying
safety contract — evidence that the fallback also holds under an
unplanned failure mode, not the designated exercise itself.

## 5. Runbook/Makefile corrections — found here, drafted in PR #82

Per instruction, `Makefile` and `docs/runbook.md` were left untouched by
this report's own PR (#81). Two corrections were found; both have since
been applied on a separate draft PR, built in a clean temporary worktree
from exact `main` so this session's other preserved, unrelated
working-tree edits to these same two files stayed untouched:
[PR #82](https://github.com/2463-FDE/jf-riverbend-portal/pull/82),
latest commit `dc23001` (a review round added a third and fourth
correction — the clinician-count/reviewer claims and the `demo-reset`
sample-output shape — beyond the two found during this Stage 5 pass).
Still draft, still requires its own explicit merge approval — recorded
here, not assumed:

1. **`docs/runbook.md`'s "Demo accounts" section never documents the
   patient-portal password.** It states "All seeded users share password
   `portal123`" with no carve-out. `db/seed/generate_seed.py:109` defines
   `PATIENT_DEMO_PASSWORD = "portalportal123"` for activated patient
   accounts (`patient-1738`, `patient-1739`), with a comment claiming this
   is "documented in docs/runbook.md" — it is not. Anyone following the
   runbook's own demo-accounts section to log in as a patient will get a
   401. **Suggested fix:** add a line noting patient portal accounts use
   `portalportal123`, distinct from the shared staff password.
2. **`Makefile:22`'s `demo-reset` target comment is stale.** It reads
   "return the demo patient (1737) to a clean pre-demo state" (singular),
   but `db/seed/demo_reset.sql` — and `docs/runbook.md`'s own "Demo
   accounts" section — describe it as covering all four canonical patients
   (1042, 1737, 1738, 1739) since 2026-08-22. Confirmed directly: running
   `make demo-reset` printed one reset row for all four. **Suggested fix:**
   update the comment to say "all four canonical demo patients."

Both are fixed on PR #82, not on this PR, and not yet merged.

## 6. Meaningful before/after retrieval result

Per Stage 3 (real-vector evaluation, `docs/planning/policy-rag-evaluation-08-25-2026.md`):
vector retrieval's source-level precision@5 (**50.00%**) beat the
deterministic keyword baseline (**41.67%**) at identical recall (100%) and
identical citation-target accuracy (100%) over 10 runnable cases. This is
the reported before/after — not a larger document or embedding count.

## 7. Links and runnable instructions validated

- All file paths this report and the reconciled responsibility matrix cite
  (`db/migrations/apply.sh`, `db/migrations/scripts/check_grant_coverage.sh`,
  `db/migrations/scripts/reconcile_duplicate_confirmed_appointments.sql`,
  `db/seed/generate_seed.py`, `db/seed/README-staff-roster.md`,
  `tools/import_synthea.py`) exist at the reported commit.
- `make demo-reset`, `make up`, `db/policy_corpus_evaluate.py --verify-only`
  all ran successfully as documented above.
- `adr/` now runs 0001–0011 (see the Week 9 reconciliation, PR #80).
- No claim of production readiness or HIPAA compliance is made anywhere in
  this report or the artifacts it cites; `README.md`'s corrected posture
  and `adr/0008` remain the authoritative statement on that.

## 8. Weekly deliverables — closed and still open

| Item | Status | Evidence |
|---|---|---|
| Policy corpus adoption + real-vector evaluation (Stages 1–3) | **CLOSED** | PRs #74→#75→#76, `main`@`2d33fcf` |
| Week 6 — HL7 comprehension package | **CLOSED** | PR #77 |
| Week 7 — golden-signal metric | **CLOSED** | PR #78 |
| Week 8 — AI data-flow/vendor memo | **CLOSED** | PR #79 |
| Week 9 — responsibility-matrix reconciliation | **CLOSED** | PR #80, `main`@`6c7fc49` |
| Week 10 / Stage 5 — final exact-main handoff | **Evidence complete on `6c7fc49`; closes when PR #81 merges** | This document |
| Runbook/Makefile corrections | **Drafted, not merged** | PR #82 |
| AL1/RXA HL7 mapping implementation | **Deferred** (documented, not authorized) | `adr/0011` |
| `libs/deid` scrub wiring | **Deferred** (documented, not authorized) | Week 8 memo, `adr/0009` |
| Payer simulation mode | **Open** | `docs/handover/responsibility-matrix.md` B-2 |
| B-4 runtime-verification sub-items (front-desk denial re-check, A1c-explanation metric) | **Open** | `docs/handover/responsibility-matrix.md` B-4 |
| Runbook/Makefile documentation gaps (§5 above) | **Open, newly found** | This report |
| MFA rollout | **Parked** (client direction) | `feat/mfa-totp-parked`, unmerged |
| Roster/role migration off `staff` | **Open, roster-gated** | `docs/handover/responsibility-matrix.md` |
| Production deployment mechanism, secret/dependency/container scanning, backup/recovery | **Out of scope for this training project** | `docs/handover/responsibility-matrix.md` B-1 |

## 9. Dated roadmap with resolver roles

| # | Item | Resolver | Moved estimate |
|---|---|---|---|
| 1 | Payer simulation mode (`payer_client.py`) | `OWNER UNASSIGNED` — unassigned training maintainer | 1–2 hours (carried from B-2) |
| 2 | B-4 runtime-verification re-check (front-desk denial under current roles.yaml; A1c-explanation metric; fresh integration-suite run) | `OWNER UNASSIGNED` — unassigned training maintainer | 2–3 hours (carried from B-4) |
| 3 | Merge PR #82 (documents `portalportal123` in `docs/runbook.md`'s Demo accounts section) | `OWNER UNASSIGNED` — unassigned training maintainer; drafted, needs explicit merge approval | Done, pending merge |
| 4 | Merge PR #82 (corrects `Makefile:22`'s stale `demo-reset` comment) | `OWNER UNASSIGNED` — unassigned training maintainer; drafted, needs explicit merge approval | Done, pending merge |
| 5 | AL1/RXA HL7 mapping implementation (per `adr/0011`'s proposed schema) | `OWNER UNASSIGNED` — unassigned training maintainer; requires separate client authorization per `CLAUDE.md` | Not estimated — implementation, not documentation |
| 6 | Wire `libs/deid.scrub()` into the two named Bedrock call sites (Week 8 memo) | `OWNER UNASSIGNED` — unassigned training maintainer | Half a day (per `adr/0009` gate item 1) |
| 7 | Roster/role migration off deprecated `staff` role | `OWNER UNASSIGNED` — unassigned training maintainer; roster-gated, needs real job-function data per `CLAUDE.md` | Not estimated — blocked on client data |
| 8 | MFA rollout (backup codes, supervisor-verified reset, pilot clinic, cutover) | Client-directed; parked pending the client's own next-cycle scheduling | Not estimated — client scheduling decision |
| 9 | Production deployment mechanism, CI security scanning, backup/recovery | Out of scope for this training engagement — routed to `w8-planner-2` / a real production-readiness effort | Not estimated |

## 10. What this report does not claim

- Not a claim of production readiness or HIPAA compliance.
- Not a claim that `GraphRecursionError` is fixed or will not recur — it is
  reported as observed, real behavior of a bounded agent loop under a real
  model call, with its safety net (the fallback) working correctly.
- Not a claim that the eligibility chat's real, working behavior generalizes
  to every environment — it depends on this environment's configured
  `BEDROCK_MODEL_ID`/`AWS_REGION`, which are local `.env` values, not
  something this repository ships or guarantees.
- Not a claim that W10 is complete while this section still says otherwise
  — see §11.

## 11. Final merge commit

**Pending.** `6c7fc49` remains the evidence baseline until this PR (#81)
merges. This section is amended with the actual merge commit once that
happens; until then, W10 is NOT COMPLETE.
