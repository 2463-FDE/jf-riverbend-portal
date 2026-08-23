-- 022_message_threads — secure patient-clinician messaging (W9.2).
--
-- A small, durable relational model, deliberately not the eligibility chat's
-- transient in-memory conversation (that endpoint has no human transcript at
-- all — see services/eligibility-service's visit chat). A thread here is a
-- real, persisted conversation between one patient and their authorized care
-- team; every message carries one immutable sender and timestamp.
--
-- Authorization is the SAME mechanism as chart access, not a new one:
-- patient_access_grants (migration 014) already ties an actor to a patient
-- for both a patient's own account and every staff grant. A thread's
-- patient_id is what gets checked against that table — see
-- services/records-service/app.py's messaging routes, which reuse
-- _authorize_or_deny exactly as the agent-draft and summary routes do.

CREATE TABLE IF NOT EXISTS message_threads (
    id          SERIAL PRIMARY KEY,
    patient_id  INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    subject     TEXT NOT NULL CHECK (char_length(subject) BETWEEN 1 AND 200),
    -- open | closed. Closing is a staff action that stops new replies; it
    -- does not delete or hide history — see thread_messages below, which has
    -- no delete route at all.
    status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    created_by  INTEGER NOT NULL REFERENCES users(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Bumped by the write path on every new message (app.py), not a
    -- trigger — there is exactly one code path that creates a message, so
    -- the invariant does not need database enforcement to hold.
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS message_threads_patient_idx
    ON message_threads (patient_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS thread_messages (
    id              SERIAL PRIMARY KEY,
    thread_id       INTEGER NOT NULL REFERENCES message_threads(id) ON DELETE CASCADE,
    -- The sender's identity and the message body are both immutable once
    -- written — no UPDATE route exists on this table at the application
    -- layer, and there must not be one: a message read by its recipient
    -- cannot un-say itself, and rewriting who sent it would falsify the
    -- audit trail every message reply relies on.
    sender_user_id  INTEGER NOT NULL REFERENCES users(id),
    body            TEXT NOT NULL CHECK (char_length(body) BETWEEN 1 AND 4000),
    -- One client-supplied key per (sender, thread). A retried send with the
    -- same key returns the original message instead of creating a second
    -- one. Scoped to thread_id, not just sender — round-1 review (MSG-002):
    -- without it, a key reused by the same sender across TWO DIFFERENT
    -- threads returned the FIRST thread's message as if it had just been
    -- posted to the second, a false "success" for a reply that was never
    -- recorded where the caller asked for it.
    idempotency_key TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS thread_messages_sender_thread_idem_key
    ON thread_messages (sender_user_id, thread_id, idempotency_key);

-- Migration-compatibility fix for the MSG-002 correction above: this file
-- ran against real deployments BEFORE this correction existed, and
-- apply.sh re-applies every file unconditionally against a database at any
-- prior point (see that script's own docstring) — it does not track "this
-- one already ran". A database from before this fix already has the OLD,
-- sender+key-only index, and leaving it in place is not merely redundant:
-- it still REJECTS the very insert the new (sender, thread, key) index and
-- the application code now correctly allow (the same idempotency_key reused
-- by one sender across two different threads), turning the fix into a
-- database-level IntegrityError regardless of what the code now permits.
-- The new index is created FIRST, so uniqueness is never unenforced even
-- for the instant between these two statements; the old one is then
-- dropped only if it is actually still there.
DROP INDEX IF EXISTS thread_messages_sender_idem_key;

CREATE INDEX IF NOT EXISTS thread_messages_thread_idx
    ON thread_messages (thread_id, created_at);

-- Per-user read position, not a per-message flag: unread count for a user in
-- a thread is "messages newer than this, excluding the reader's own", so one
-- row per (user, thread) is enough regardless of how many messages exist.
CREATE TABLE IF NOT EXISTS thread_read_state (
    thread_id             INTEGER NOT NULL REFERENCES message_threads(id) ON DELETE CASCADE,
    user_id               INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    last_read_message_id  INTEGER REFERENCES thread_messages(id),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (thread_id, user_id)
);
