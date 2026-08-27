# ADR 0012 — PHI field encryption: shared crypto library, blind index, key management

**Date:** 2026-08-26
**Status:** Accepted
**Context:** w8-planner-2 P2, executing the remediation plan `adr/0008` left
blocked on a client decision.

## Context

`adr/0008` recorded, with a remediation plan, why `ssn`/`dob`/`notes` were
still plaintext and named the exact obstacle: `patients.ssn_digits` is a
Postgres `GENERATED ALWAYS AS (regexp_replace(ssn, '\D', '', 'g')) STORED`
column, indexed, and used by `services/records-service/reconciliation.py`'s
duplicate-detection query (`WHERE ssn_digits = :normalized`). Encrypting
`ssn` breaks that generated expression outright — Postgres cannot regex a
digit pattern out of AEAD ciphertext. `dob` and `notes` carry no equivalent
constraint: neither is ever compared in a SQL `WHERE` clause anywhere in
this codebase (verified by search, not assumed), so they can be encrypted
with no matching-index redesign.

adr/0008 also named the layer-1 choice — application-level encryption vs.
`pgcrypto` vs. a volume-level control once a deployment target exists — as
blocked on the client. That decision has now been made.

## Decision

**Application-level field encryption**, AES-256-GCM, via a new shared
package `libs/phi_crypto`. `ssn_digits` is replaced with an HMAC-SHA256
blind index computed under a key independent of the encryption key —
deterministic (so the equality lookup keeps working), but not reversible
without that second key, and not the same secret as the one that decrypts
the field it indexes.

### Why a shared library, not copy-paste (supersedes part of ADR 0001)

ADR 0001 established no shared Python library across services — each
service copy-pastes `config.py`/`db.py`/`models.py`/`schemas.py`/
`logging_config.py`/`app.py` deliberately. That decision **stands** for
everything it was made for: request handling, persistence, authorization,
config wiring. It does not extend well to AEAD crypto primitives and key
validation specifically — copy-pasted four times, the failure mode isn't
duplicated code, it's **silent incompatibility**: intake-service encrypts
under one envelope format or key-validation rule, records-service decrypts
expecting another, and the failure surfaces as a decrypt error on live PHI
instead of a code-review comment. `libs/deid` (P6, AI-egress scrubbing)
already established that `libs/` holds cross-cutting code outside ADR
0001's per-service rule when the alternative is drift on something
security-critical; this extends the same precedent, narrowly.

`libs/phi_crypto` owns only: AEAD encrypt/decrypt, versioned envelope
encode/decode, blind-index computation, SSN normalization (the one
canonical implementation — see below), the `KeyProvider` protocol, and
`EnvKeyProvider`. It has no FastAPI, SQLAlchemy, or config-class
dependency, and does not decide *when* to encrypt or *what* AAD to use —
every consuming service writes that explicitly:

```
normalize -> encrypt -> (compute blind index, if this field is looked
up by equality) -> persist envelope + key_version + blind index as
separate columns
```

**No SQLAlchemy `TypeDecorator`.** A transparent column type would hide
the AAD-binding-to-row-and-column, key-version selection, and blind-index
computation behind ordinary attribute assignment — exactly the kind of
implicit behavior that makes a crypto bug invisible in review. Explicit
service-level code is slightly more boilerplate and considerably easier to
audit.

### Envelope format

One TEXT column per encrypted field holds `base64(marker || nonce ||
ciphertext_with_tag)` — see `libs/phi_crypto/envelope.py`. `aad` is
required on every call and is built by the caller as
`b"<table>.<column>.<patient_id>"`, binding ciphertext to the exact row and
column it was written for. Copying ciphertext from one row/column into
another fails AEAD authentication instead of silently decrypting as if it
belonged there.

The envelope's format marker (`phc1`) versions the *encoding scheme*
(AEAD algorithm, nonce length) and is independent of the **key version**,
which each consuming service stores in a sibling `<field>_key_version`
column, not inside the envelope itself. The two rotate independently: a
key rotation bumps `PHI_ACTIVE_KEY_VERSION`; an algorithm change would
bump the format marker.

### Key management: environment-provided, not KMS-backed

No KMS or secrets-manager integration exists anywhere in this repository
(confirmed by search) — introducing one is out of scope for this change.
Keys are read from the process environment by `EnvKeyProvider`, following
the same fail-closed startup pattern `INTERNAL_SERVICE_TOKEN` already uses
in every service (`services/*/config.py` + an `@app.on_event("startup")`
hook that refuses to accept traffic on a missing/malformed key):

```
PHI_ACTIVE_KEY_VERSION=v1
PHI_ENCRYPTION_KEY_V1=<base64, decodes to exactly 32 bytes>
PHI_BLIND_INDEX_KEY_V1=<base64, decodes to exactly 32 bytes, != the above>
```

`EnvKeyProvider` validates every configured key-version pair — not only
the active one — at construction time: missing, non-base64, wrong-length,
or identical encryption/blind-index keys all raise before the owning
service accepts a request. Error messages name the offending env var, never
its value. Rotation keeps a superseded version's keys configured
alongside the active one so old ciphertext still decrypts; an unconfigured
version raises rather than silently falling back to another key.

Every caller depends on the `KeyProvider` **protocol**, not on
`EnvKeyProvider` or any env var name directly — this is deliberately
labeled environment-backed secret injection suitable for the current
deployment model, not KMS-backed key custody, and the protocol boundary is
what lets a future KMS-backed provider replace `EnvKeyProvider` without
touching intake-service or records-service's own code.

### Which services hold keys

Only `intake-service` (encrypts new patient PHI at registration, computes
the SSN blind index) and `records-service` (decrypts for authorized reads,
runs the duplicate-match lookup) import `libs/phi_crypto` or receive PHI
key material. `roi-service` does not decrypt fields directly — it releases
records by proxying through records-service's own read path, so it never
needed a key. The gateway, frontend, scheduling-service, interop-service,
and eligibility-service receive neither the dependency nor the keys; none
of them read or write the encrypted columns. Keys are never transmitted
over an internal HTTP call — each holder reads its own copy from its own
environment.

## Migration

`db/migrations/031_phi_field_encryption.sql`:

- `ALTER TABLE patients ALTER COLUMN ssn_digits DROP EXPRESSION` — converts
  the generated column to an ordinary one, preserving the existing index.
- Adds `ssn_key_version`, `dob_key_version`, `notes_key_version` (TEXT,
  nullable — NULL means "not yet migrated to ciphertext," see below).
- A one-time backfill script (`db/migrations/scripts/encrypt_existing_phi.py`)
  re-encrypts every existing plaintext row in place under the active key and
  repopulates `ssn_digits` as the HMAC blind index — run once, operator-
  invoked, not part of `apply.sh`'s automatic idempotent-DDL sweep, because
  it needs `PHI_ENCRYPTION_KEY_V1`/`PHI_BLIND_INDEX_KEY_V1` at hand and
  writes application-encrypted values the SQL migration itself cannot
  compute.
- `services/intake-service/app.py` and `services/records-service/app.py`
  read/write ciphertext unconditionally after this migration — there is no
  dual-read fallback for still-plaintext rows. Deploy order: run the SQL
  migration, run the backfill script, then deploy the new service code.
  Deploying the new code before the backfill runs would make records-
  service try to AEAD-decrypt plaintext and fail closed on every row.

## Consequences

- `ssn`, `dob`, and `notes` are ciphertext at rest; a database or backup
  reader without `PHI_ENCRYPTION_KEY_V1` cannot read them in the clear.
  This is the compensating control `adr/0008` listed as absent.
- `ssn_digits` no longer holds a reversible digit string — a database
  reader without `PHI_BLIND_INDEX_KEY_V1` cannot recover the SSN from it,
  only test equality against a blind index computed the same way.
- Key custody is environment-variable-based, not KMS-backed — a compromise
  of a service's own environment (not just its database) still exposes
  PHI. This is the explicitly accepted, documented posture, not an
  oversight; re-evaluate if/when a KMS or secrets-manager target exists.
- `dob`/`notes` gain no new query capability by design — they were never
  queried by value before, and this migration does not add a blind index
  for either (nothing in this codebase needs one).
- `libs/rag_corpus`'s embedding pipeline reads `records.title`/`body`, not
  `patients.ssn/dob/notes` — out of scope for this migration, but a
  reminder that `records` text is a separate, unencrypted PHI surface this
  ADR does not evaluate.

## Alternatives considered

**Deterministic encryption for `ssn` itself (e.g. AES-SIV), no separate
blind-index column.** Simpler schema — one ciphertext column serves both
storage and lookup. Rejected: deterministic ciphertext leaks equality
patterns (identical plaintext always produces identical ciphertext),
which is a real weakening for the single most sensitive field in this
schema; the blind-index split costs one extra column and keeps `ssn`
itself semantically-secure (randomized).

**Leave `ssn` plaintext, encrypt only `dob`/`notes`.** Narrower and
faster — no generated-column migration needed at all. Rejected: `ssn` is
adr/0002's and adr/0008's own headline example of the risk being
addressed; encrypting everything except the highest-sensitivity field
would leave the stated goal unmet.

**Copy `libs/phi_crypto` into each service instead of sharing it.**
Consistent with ADR 0001's letter. Rejected for the reason `libs/deid`
was already the exception to: drift in a copy-pasted crypto primitive is
a security bug, not a style inconsistency — see "Why a shared library"
above.
