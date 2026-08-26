# Riverbend Breach Response Policy (v1, contractor draft)

1. **Notification clock.** If a breach of unsecured PHI is discovered, Riverbend
   will notify affected individuals without unreasonable delay and no later than
   **60 calendar days** after discovery (per 45 CFR 164.404).
2. **Media / Secretary.** Breaches affecting 500+ individuals will be reported to
   the media and HHS Secretary.
3. **Discovery.** *(TODO — no mechanism currently exists to detect an
   impermissible access; there is no breach-detection alerting, so
   "discovery" is still undefined in practice. Updated 2026-08-26
   (w8-planner-2 P3): `audit_logs` is no longer mutable — it is append-only
   and hash-chained against a compromised runtime/application role, and
   `db/migrations/scripts/verify_audit_chain.py` can prove no logged row was
   altered. That is not the same thing as detection: it protects the
   integrity of a record after something is logged to it, not whether an
   impermissible access gets logged or flagged in the first place. This TODO
   remains open.)*
