-- 032_agent_draft_text_encryption.sql — adr/0012 follow-up: AEAD-encrypts
-- agent_draft_provenance.generated_text (migration 020's "THE CLINICAL
-- ARTIFACT" column), which migration 031 (patients.ssn/dob/notes) did not
-- cover. See adr/0010's updated section and adr/0012 for the shared
-- design (libs/phi_crypto, environment-provided keys — same posture, same
-- caveats, not repeated here).
--
-- generated_text_key_version NULL alongside a non-NULL generated_text means
-- the row predates this migration and is still plaintext, awaiting
-- db/migrations/scripts/encrypt_agent_draft_text.py's backfill — same
-- "not yet migrated" contract migration 031 established for
-- patients.*_key_version. Every row created by current application code
-- (services/records-service/agent_drafts.py::create_draft) always sets
-- both generated_text and generated_text_key_version together; there is
-- no code path that writes a plaintext row going forward.
--
-- WHY THE GUARD TRIGGER NEEDS TO CHANGE, NOT JUST THE TABLE.
-- agent_draft_provenance_guard_trigger (020) freezes generated_text
-- unconditionally on UPDATE — exactly right for blocking a silent content
-- edit, exactly wrong for the backfill (and any future key rotation),
-- both of which MUST update generated_text (plaintext -> ciphertext, or
-- ciphertext-under-v1 -> ciphertext-under-v2) without that being a content
-- edit. The fix is not to carve out an exception for "the backfill
-- script" (a trigger cannot know who is connecting, and even if it could,
-- that is not the actual property that matters) — it is to freeze the
-- right INVARIANT: generated_text may change ONLY together with
-- generated_text_key_version. A same-key-version change to generated_text
-- is still unconditionally rejected (that IS a content edit); a change
-- that also changes the key version is a re-encryption, not an edit, and
-- is allowed regardless of direction (NULL->v1 for the initial backfill,
-- v1->v2 for a future rotation) — the trigger cannot verify the recovered
-- PLAINTEXT stayed identical across a re-encryption (it has no key), so
-- that guarantee is the responsibility of whatever re-encrypts (the
-- backfill script here; a future rotation tool later), not this trigger.
-- generated_text_key_version is deliberately NOT added to the
-- unconditionally-frozen field list below — it is the one field in this
-- pair a re-encryption is allowed to change, by design.

ALTER TABLE agent_draft_provenance
    ADD COLUMN IF NOT EXISTS generated_text_key_version TEXT;

COMMENT ON COLUMN agent_draft_provenance.generated_text IS
    'THE CLINICAL ARTIFACT. AEAD-encrypted (libs/phi_crypto) once generated_text_key_version is set on this row; immutable together with that column — see agent_draft_provenance_guard_trigger. This is what the clinician reviews and what the patient is shown; it is never regenerated at display time.';
COMMENT ON COLUMN agent_draft_provenance.generated_text_key_version IS
    'Which PHI_ENCRYPTION_KEY_V<n> encrypted generated_text on this row. NULL = row predates this migration, generated_text is still plaintext, awaiting db/migrations/scripts/encrypt_agent_draft_text.py. The one field in this table''s immutable-identity set that a re-encryption (backfill or future rotation) is allowed to change — see agent_draft_provenance_guard_trigger, which requires generated_text to change in lockstep with this column, never independently.';

CREATE OR REPLACE FUNCTION agent_draft_provenance_guard()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' THEN
            RAISE EXCEPTION
                'agent_draft_provenance rows are never deleted once they leave '
                '''draft'' status (draft id=%, status=%). Deletion policy beyond '
                'this refusal (e.g. a hash-chained audit trail) is w8-planner-2 B3; '
                'until then, a decided draft is simply never removable, regardless '
                'of whether it has citations.', OLD.id, OLD.status;
        END IF;
        RETURN OLD;
    END IF;

    -- TG_OP = 'UPDATE' from here on.
    IF NEW.patient_id IS DISTINCT FROM OLD.patient_id
       OR NEW.version IS DISTINCT FROM OLD.version
       OR NEW.provenance_label IS DISTINCT FROM OLD.provenance_label
       OR NEW.correlation_id IS DISTINCT FROM OLD.correlation_id
       OR NEW.model_id IS DISTINCT FROM OLD.model_id
       OR NEW.prompt_version IS DISTINCT FROM OLD.prompt_version
    THEN
        RAISE EXCEPTION
            'agent_draft_provenance identity/evidence is immutable once written '
            '(draft id=%, version=%): patient_id, version, provenance_label, '
            'correlation_id, model_id and prompt_version never change after '
            'insert. A corrected or regenerated draft is a NEW version.',
            OLD.id, OLD.version;
    END IF;

    -- 032: generated_text is frozen UNLESS generated_text_key_version also
    -- changes in the same statement — that pairing is a re-encryption
    -- (initial backfill, NULL->v1, or a future rotation, v1->v2), not a
    -- content edit. A change to generated_text with the SAME key version
    -- is exactly the silent-content-edit case this guard exists to block.
    IF NEW.generated_text_key_version IS NOT DISTINCT FROM OLD.generated_text_key_version
       AND NEW.generated_text IS DISTINCT FROM OLD.generated_text
    THEN
        RAISE EXCEPTION
            'agent_draft_provenance.generated_text cannot change without '
            'generated_text_key_version also changing (draft id=%, version=%) '
            '— that would be a silent content edit, not a re-encryption.',
            OLD.id, OLD.version;
    END IF;

    IF NEW.validation_code IS DISTINCT FROM OLD.validation_code
       AND OLD.status <> 'draft'
    THEN
        RAISE EXCEPTION
            'agent_draft_provenance.validation_code may only be set once, when '
            'leaving draft status (draft id=%, current status=%)', OLD.id, OLD.status;
    END IF;

    IF NEW.status IS DISTINCT FROM OLD.status THEN
        IF NOT (
            (OLD.status = 'draft'        AND NEW.status IN ('validated', 'refused'))
            OR (OLD.status = 'validated' AND NEW.status IN ('approved', 'rejected'))
            OR (OLD.status = 'approved'  AND NEW.status = 'superseded')
        ) THEN
            RAISE EXCEPTION
                'invalid agent_draft_provenance status transition % -> % (draft id=%). '
                'Allowed: draft->validated|refused, validated->approved|rejected, '
                'approved->superseded. refused/rejected/superseded are terminal.',
                OLD.status, NEW.status, OLD.id;
        END IF;
    END IF;

    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- The trigger binding itself is unchanged (still fires on the same
-- function name) — no DROP TRIGGER/CREATE TRIGGER needed, CREATE OR
-- REPLACE FUNCTION above is sufficient and takes effect immediately for
-- the existing trigger.
