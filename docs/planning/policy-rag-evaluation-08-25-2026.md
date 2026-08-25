# Policy RAG evaluation — 2026-08-25

**Status:** working-tree evidence; not yet reviewed or merged  
**Repository base:** `465f744bd1dd670e6b2902cb93fded3aa1630118`  
**Data:** synthetic training policies only

## What this evaluates

This is the first evaluation of the adopted August 24 policy corpus. It is
separate from the historical Week 2 `libs/rag_eval` report, which used five
synthetic patient records and fake vectors. This run uses the current Markdown
manifest, real Amazon Titan embeddings, live PostgreSQL/pgvector retrieval,
application-derived role scope, and the 28 client-supplied evaluation cases.

It evaluates corpus freshness and retrieval. It does not claim that generated
answer wording, clinical escalation, or refusal prose has been evaluated.

## Manifest/database freshness

| Check | Result |
|---|---:|
| Active manifest documents | 15 |
| Active database documents | 15 |
| Expected deterministic chunks | 207 |
| Current embeddings | 207 |
| Provider | `bedrock` |
| Model | `amazon.titan-embed-text-v2:0` |
| Dimension | 1024 |
| Missing/extra documents or embeddings | 0 |
| Document/chunk/embedding hash mismatches | 0 |

An unchanged re-ingestion writes no chunks or embeddings. During adoption,
`LAB-REL-001` advanced from version 1.1 to 1.2 and the old version was
deactivated rather than deleted, preventing its removed clinician-only chunk
from remaining current retrieval evidence.

## Client-case classification

| Classification | Count | Meaning |
|---|---:|---|
| Runnable supported cases | 11 | Required approved evidence exists in the active corpus |
| Negative retrieval/safety cases | 13 | Retrieval filters and forbidden-source behavior can be checked; generated refusal wording is not scored |
| Specification conflicts | 3 | Client expectation contradicts the active corpus/access model |
| Deferred | 1 | Required source was deliberately excluded because it has citation metadata but no source text |

Case coverage for executable retrieval checks is 24/28 (85.71%). Deferred and
conflicting cases remain in the report and are not counted as passes.

## Real-vector retrieval results at top-k 5

| Metric | Vector retrieval | Keyword baseline |
|---|---:|---:|
| Required-source recall@5 | 100% | 100% |
| Citation-target case accuracy | 100% | 100% |
| Source-level precision@5 | 50.00% | 42.31% |
| Forbidden citation hits | 0 | 0 |
| Out-of-scope retrieval hits | 0 | 0 |

The precision improvement is the meaningful before/after comparison for this
corpus. Document and embedding counts prove inventory, not retrieval quality.

## Conflicts and deferred case

- `E09`, `E10`, `E25` — require
  `POL-A1C-MONITOR-STALE@2023-01-15`, which is intentionally superseded and
  retrieval-disabled. Enabling it merely to pass would violate the active
  manifest policy.
- `E19` — requires a citation-only ADA record with no source text. It cannot
  support quotations and remains deferred.

The first run exposed `E08`: the merged clinician early-release section was
retrievable in patient scope. The corpus was corrected by versioning the
general policy as `LAB-REL-001@1.2` and moving the procedure to the
clinician-only companion `LAB-REL-EXCEPTION-001@1.0`. The repeated real-vector
run retrieved the companion for clinician case `E07` and did not retrieve it
for patient case `E08`.

## Reproduce

With local credentials configured and Postgres running, execute from the
repository root without printing credential values:

```bash
docker compose run --rm --no-deps \
  -v "$PWD:/workspace:ro" -w /workspace -e DB_HOST=postgres \
  records-service python db/policy_corpus_ingest.py

docker compose run --rm --no-deps \
  -v "$PWD:/workspace:ro" -w /workspace -e DB_HOST=postgres \
  records-service python db/policy_corpus_evaluate.py --verify-only

docker compose run --rm --no-deps \
  -v "$PWD:/workspace:ro" -w /workspace -e DB_HOST=postgres \
  records-service python db/policy_corpus_evaluate.py --top-k 5
```

The evaluator emits evaluation IDs, source/version/citation metadata, counts,
and categorical reasons. It does not emit questions, document text, prompts,
model responses, patient identifiers, credentials, or raw provider errors.

## Verification

- Focused policy corpus/evaluation tests: 57 passed.
- Full non-integration suite: 1,463 passed, 132 deselected, 1 expected failure.
- Real manifest/database parity: passed.
- Real Titan/pgvector retrieval evaluation: completed.

## Remaining boundary

`agent_refusal_accuracy` is intentionally unset. Retrieval can prove that
forbidden or unauthorized evidence was not returned; it cannot prove that a
generated answer used the correct refusal or escalation language. That requires
a separate real-agent evaluation and must not be inferred from these metrics.
