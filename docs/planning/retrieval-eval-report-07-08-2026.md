# Retrieval-Eval Report — 2026-07-08

- **Purpose:** First run of the retrieval-eval harness (`libs/rag_eval`)
  against the Commit 2 corpus/embedding-cache pipeline (`libs/rag_corpus`)
  and the contractor's gold set (`db/seed/goldset.json`). This is a
  measurement artifact, not a validation of the gold set or of any
  retrieval approach — see `docs/planning/gold-set-risk-log-07-08-2026.md`
  (`RISK-GS-01`) for why the gold set itself cannot be trusted yet.
- **How to reproduce:** `python3 -m libs.rag_eval.harness` from the repo
  root (uses `EMBEDDING_PROVIDER=fake` by default — no network call, no
  credentials). Config: `RAG_EVAL_TOP_K` (default `1`),
  `RAG_CORPUS_MAX_RECORDS` (default `200`).

## 1. Run parameters

- **Embedding provider:** `fake` (deterministic, content-hash-derived
  vectors — see `libs/embedding_client/providers/fake_provider.py`)
- **top_k:** 1
- **Corpus:** 5 records, built from `db/seed/patients.csv` +
  `db/seed/encounters.csv` (all 5 rows fit under the default 200-record cap,
  so no truncation occurred this run)
- **Gold-set cases:** 3 (`db/seed/goldset.json`)

## 2. Metrics

| Metric | Value |
|---|---|
| recall@1 | 0.0% |
| precision@1 | 0.0% |
| duplicate-rate | 33.3% |
| fragment-coverage gap | 66.7% |

## 3. Duplicate clusters (identity proxy: normalized SSN match)

- patient_ids `[1042, 1330, 1588]` — the "Maria Gonzalez" fixture: same SSN
  across all three rows, but three different name spellings
  ("Maria Gonzalez" / "Maria Gonzales" / "M. Gonzalez") and, on one row, a
  differing DOB (see `db/seed/generate_seed.py`).

## 4. Per-case detail

| Query | Expected patient | Expected record | Retrieved record (top-1) | recall hit | fragment gap |
|---|---|---|---|---|---|
| show me Maria Gonzalez's allergies | 1042 | seed-enc-0001 | seed-enc-0005 | no | **yes** |
| what medications is James O'Brien on? | 1043 | seed-enc-0004 | seed-enc-0005 | no | no |
| latest lab results for M. Gonzalez | 1588 | seed-enc-0003 | seed-enc-0005 | no | **yes** |

## 5. What this run does and does not show

**Does not show:** real retrieval quality. The `fake` provider's vectors are
derived from each text's SHA-256 hash, not from any semantic model — recall@1
and precision@1 being 0% here is an artifact of that (all three queries
happened to hash-rank the same unrelated record, `seed-enc-0005`, highest),
not evidence that a real embedding model would fail. A sanity check at
`top_k=5` (the entire 5-record corpus) confirms the retrieval mechanism
itself is correct: recall@5 = 100%, precision@5 = 20% (1 relevant of 5
returned, matching the metric definition), and fragment-coverage gap drops
to 0% (trivial at k = corpus size, since every fragment is always
"retrieved"). Re-running with `EMBEDDING_PROVIDER=ollama` against a real
local model would be required for a real retrieval-quality signal; that is
not done in this deliverable (no live Ollama server was available in this
environment, and running it is not required for what this commit measures).

**Does show, independent of embedding quality:**

- **duplicate-rate (33.3%)** — of the 3 distinct people in this tiny corpus,
  1 (Maria Gonzalez) is split across 3 patient rows. This number comes
  entirely from `db/seed/patients.csv`'s SSN field, not from retrieval, so it
  is unaffected by which embedding provider is used.
- **fragment-coverage gap (66.7% at top_k=1)** — this is the metric that
  matters most for this deliverable. In both cases involving the fragmented
  "Maria Gonzalez" identity, the harness flags a gap: the query resolved to
  one fragment (e.g. patient 1042, whose own chart row says "no known
  allergies"), while a *different* fragment for the same real person
  (patient 1330) holds the clinically relevant content (a penicillin
  allergy) that the top-1 result never surfaced. This reproduces, in
  miniature and without touching production code, the exact mechanism
  described in `AUD-04`/`AUD-09` and manifested in the real incident
  `RIV-160` (`docs/analysis/system-audit-07-01-2026.md`): a clinician can
  get a confident, fully-cited, "no known allergies" answer that is wrong by
  omission, from a system with no way to indicate anything is missing. The
  James O'Brien case (no duplicate patient row) correctly shows no gap,
  confirming the metric doesn't fire on non-fragmented patients.

This is the concrete demonstration behind
`docs/planning/gold-set-risk-log-07-08-2026.md`'s `RISK-GS-01` claim: a
retrieval approach can look acceptable on recall/precision alone while still
missing a patient's other fragments, and a gold set authored without
correcting for `AUD-09` (as this one was — `cites_records` for the Maria
Gonzalez cases points at a single fragment each, never both) will not by
itself catch this.

## 6. Known limitations of this harness

- **Record-id mapping is positional, not a real foreign key.**
  `db/seed/encounters.csv` (what Commit 2's corpus builder reads) has no
  native record-id column. This harness maps `goldset.json`'s
  `cites_records: [N]` to corpus record `seed-enc-{N:04d}` on the assumption
  that CSV row order N matches the original `records` table id N in
  `db/seed/generate_seed.py` — true today because both were generated from
  the same ordered fixture list, but not a structural guarantee. If either
  file's row order changes independently, this mapping breaks silently.
- **The identity-cluster proxy (normalized SSN match) is a measurement
  convenience, not a match-key algorithm.** It works here only because the
  fixture data happens to keep SSN identical across the Maria Gonzalez rows.
  Real intake data cannot be assumed to have a reliable, non-blank SSN for
  every patient. See `libs/rag_eval/identity_proxy.py` for the same caveat
  in code, and `adr/0004-master-patient-index-match-key.md` for the actual
  match-key approach proposed (not implemented) for production.
- **Corpus and gold set are both tiny (5 records / 3 queries).** These
  percentages are illustrative of the mechanism, not statistically
  meaningful measurements of a production-scale system.
- **This report does not validate the gold set.** It demonstrates a specific
  known failure mode (`RISK-GS-01` / `AUD-09`) using the harness against the
  gold set as given; it does not audit the other two gold-set cases' label
  quality or coverage more broadly.

## 7. Non-goals

This report does not implement a retrieval helper, does not fix `AUD-09`,
and does not change any production route, schema, or data. See
`adr/0004-master-patient-index-match-key.md` for the proposed (not
implemented) production fix.
