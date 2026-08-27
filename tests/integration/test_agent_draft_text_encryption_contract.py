"""Integration tests for migration 032's agent_draft_provenance.generated_text
encryption contract — the Postgres-level guarantees SQLite unit tests
cannot exercise (the trigger's re-encryption invariant) plus the
backfill script's actual behavior against a live database (resumability,
key rotation).

Connects to Postgres directly, same pattern as
test_agent_draft_provenance_contract.py, and invokes
db/migrations/scripts/encrypt_agent_draft_text.py as a real subprocess
(the same way `make phi-backfill` does via `docker compose exec`, just
against the host-published port instead) — this is testing the script's
actual behavior, not a reimplementation of it.

Requires the full stack up (`make up`) AND PHI_ACTIVE_KEY_VERSION /
PHI_ENCRYPTION_KEY_V1 / PHI_BLIND_INDEX_KEY_V1 set in the environment this
test runs in (the same keys the live stack's .env uses — see
docs/runbook.md's "Required one-time setup" section).

Run with:  pytest -m integration tests/integration/test_agent_draft_text_encryption_contract.py
Skipped by default in CI (`pytest -m "not integration"`).
"""
import base64
import os
import subprocess
import sys
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")
import psycopg2.errors  # noqa: E402

pytestmark = pytest.mark.integration

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BACKFILL_SCRIPT = os.path.join(REPO_ROOT, "db", "migrations", "scripts", "encrypt_agent_draft_text.py")

_DB_HOST = os.getenv("DB_HOST", "localhost")
_DB_PORT = os.getenv("DB_PORT", "5432")
_DB_NAME = os.getenv("DB_NAME", "riverbend")
_DB_USER = os.getenv("DB_USER", "riverbend_app")
_DB_PASSWORD = os.getenv("DB_PASSWORD", "changeme")


def _connect():
    conn = psycopg2.connect(
        host=_DB_HOST, port=_DB_PORT, dbname=_DB_NAME, user=_DB_USER, password=_DB_PASSWORD,
    )
    conn.autocommit = False
    return conn


def _backfill_env(**overrides):
    env = dict(os.environ)
    env.setdefault("DB_HOST", _DB_HOST)
    env.setdefault("DB_PORT", _DB_PORT)
    env.setdefault("DB_NAME", _DB_NAME)
    env.setdefault("DB_USER", _DB_USER)
    env.setdefault("DB_PASSWORD", _DB_PASSWORD)
    env.update(overrides)
    return env


def _run_backfill(*extra_args, env_overrides=None):
    return subprocess.run(
        [sys.executable, BACKFILL_SCRIPT, *extra_args],
        env=_backfill_env(**(env_overrides or {})),
        capture_output=True, text=True,
    )


def _require_phi_env():
    missing = [
        v for v in ("PHI_ACTIVE_KEY_VERSION", "PHI_ENCRYPTION_KEY_V1", "PHI_BLIND_INDEX_KEY_V1")
        if not os.getenv(v)
    ]
    if missing:
        pytest.skip(f"requires {missing} in the environment — see docs/runbook.md")


@pytest.fixture()
def conn():
    c = _connect()
    yield c
    c.rollback()
    c.close()


@pytest.fixture()
def patient_id(conn):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO patients (name, dob, created_via) VALUES (%s, %s, %s) RETURNING id",
            (f"Encryption Contract Test {uuid.uuid4().hex[:8]}", "1990-01-01", "front_desk"),
        )
        pid = cur.fetchone()[0]
    conn.commit()
    return pid


def _insert_draft(conn, *, patient_id, version, generated_text="plaintext draft body",
                   generated_text_key_version=None, correlation_id=None):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_draft_provenance "
            "(patient_id, version, status, provenance_label, correlation_id, model_id, "
            " validation_code, generated_text, generated_text_key_version, prompt_version) "
            "VALUES (%s, %s, 'draft', 'real', %s, 'model-x', NULL, %s, %s, 'prompt-1') "
            "RETURNING id",
            (patient_id, version, correlation_id or f"corr-{uuid.uuid4().hex[:8]}",
             generated_text, generated_text_key_version),
        )
        draft_id = cur.fetchone()[0]
    conn.commit()
    return draft_id


# --- 6. immutable draft ciphertext cannot be updated ------------------------ #
#
# These three tests are purely about the TRIGGER's invariant — the content
# of generated_text/generated_text_key_version is never real ciphertext or
# a real key version here, deliberately: this table has no per-test
# isolation (rows persist across the whole live database, same as every
# other integration test in this repo), and db/migrations/scripts/
# encrypt_agent_draft_text.py's own tests below run REAL backfill/rotation
# queries scoped by key_version across the WHOLE table, not per-patient.
# Tagging these placeholder rows with a real-looking version like "v1"
# would make them collide with that scan and fail it with an
# EnvelopeFormatError on a string that was never real ciphertext to begin
# with — "trigger-test-*" can never collide with a real
# PHI_ACTIVE_KEY_VERSION.


def test_a_same_key_edit_to_generated_text_is_rejected(conn, patient_id):
    draft_id = _insert_draft(conn, patient_id=patient_id, version=1,
                             generated_text="CIPHERTEXT_ORIGINAL",
                             generated_text_key_version="trigger-test-key")

    with pytest.raises(psycopg2.errors.RaiseException, match="cannot change without"):
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE agent_draft_provenance SET generated_text = %s WHERE id = %s",
                ("CIPHERTEXT_TAMPERED", draft_id),
            )


def test_a_paired_reencryption_is_allowed(conn, patient_id):
    # The exact invariant migration 032 encodes: generated_text may change
    # ONLY together with generated_text_key_version — this is what makes
    # the backfill (and any future rotation) possible at all without
    # weakening the "no silent content edit" guarantee.
    draft_id = _insert_draft(conn, patient_id=patient_id, version=1,
                             generated_text="CIPHERTEXT_UNDER_KEY_A",
                             generated_text_key_version="trigger-test-key-a")

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_draft_provenance SET generated_text = %s, generated_text_key_version = %s "
            "WHERE id = %s",
            ("CIPHERTEXT_UNDER_KEY_B", "trigger-test-key-b", draft_id),
        )
        cur.execute(
            "SELECT generated_text, generated_text_key_version FROM agent_draft_provenance WHERE id = %s",
            (draft_id,),
        )
        text, key_version = cur.fetchone()
    conn.commit()

    assert (text, key_version) == ("CIPHERTEXT_UNDER_KEY_B", "trigger-test-key-b")


def test_other_identity_fields_are_still_frozen_regardless_of_key_version_changes(conn, patient_id):
    # 032 narrowed the freeze condition for generated_text specifically —
    # confirms it did NOT accidentally loosen the other fields.
    draft_id = _insert_draft(conn, patient_id=patient_id, version=1,
                             generated_text_key_version="trigger-test-key")

    with pytest.raises(psycopg2.errors.RaiseException, match="immutable once written"):
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE agent_draft_provenance SET model_id = 'a-different-model' WHERE id = %s",
                (draft_id,),
            )


# --- 8. interrupted backfill resumes safely --------------------------------- #


def test_backfill_resumes_after_a_simulated_interruption(conn, patient_id):
    _require_phi_env()
    # Three plaintext rows; the "interruption" is simulated by manually
    # completing one of them (as if a prior run committed it before
    # crashing) before invoking the script — proving a resumed run only
    # touches what's still actually plaintext, not a full re-run from
    # scratch.
    d1 = _insert_draft(conn, patient_id=patient_id, version=1, generated_text="First draft, plaintext.")
    d2 = _insert_draft(conn, patient_id=patient_id, version=2, generated_text="Second draft, plaintext.")
    d3 = _insert_draft(conn, patient_id=patient_id, version=3, generated_text="Third draft, plaintext.")

    active_version = os.environ["PHI_ACTIVE_KEY_VERSION"]
    # Deliberately NOT tagged with the real active_version: this row only
    # needs a NON-NULL key_version (the backfill script's own WHERE clause
    # is `key_version IS NULL`, so any non-NULL value is correctly
    # skipped) — tagging it with the real "v1"/"v2" identifier would make
    # it collide with test_mixed_key_versions_all_decrypt_correctly_after_a_rotation's
    # broader `WHERE key_version = '<real version>'` scan later, on a
    # string that was never real ciphertext.
    already_done_marker_version = "trigger-test-already-done"
    with conn.cursor() as cur:
        # Simulate: an earlier, interrupted run already finished d1.
        cur.execute(
            "UPDATE agent_draft_provenance SET generated_text = %s, generated_text_key_version = %s "
            "WHERE id = %s",
            ("ALREADY_ENCRYPTED_BY_A_PRIOR_RUN", already_done_marker_version, d1),
        )
    conn.commit()

    result = _run_backfill()
    assert result.returncode == 0, result.stderr
    assert "ALREADY_ENCRYPTED_BY_A_PRIOR_RUN" not in result.stdout  # never prints ciphertext/plaintext

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, generated_text, generated_text_key_version FROM agent_draft_provenance "
            "WHERE id IN (%s, %s, %s) ORDER BY id",
            (d1, d2, d3),
        )
        rows = {r[0]: (r[1], r[2]) for r in cur.fetchall()}

    # d1 untouched by this run (it was already done) — same ciphertext-shaped marker.
    assert rows[d1] == ("ALREADY_ENCRYPTED_BY_A_PRIOR_RUN", already_done_marker_version)
    # d2/d3 backfilled by this run.
    assert rows[d2][1] == active_version
    assert rows[d2][0] != "Second draft, plaintext."
    assert rows[d3][1] == active_version
    assert rows[d3][0] != "Third draft, plaintext."

    # Re-running again is a clean no-op — resumability, not "runs once".
    result2 = _run_backfill()
    assert result2.returncode == 0, result2.stderr
    assert "nothing to backfill" in result2.stdout


# --- 9. mixed key versions work during rotation ----------------------------- #


def test_mixed_key_versions_all_decrypt_correctly_after_a_rotation(conn, patient_id):
    _require_phi_env()
    v1_enc = os.environ["PHI_ENCRYPTION_KEY_V1"]
    v1_idx = os.environ["PHI_BLIND_INDEX_KEY_V1"]
    # A second key version, generated fresh for this test — simulates a
    # rotation without requiring the real deployment to have one
    # configured. Independent of PHI_ENCRYPTION_KEY_V1 (never derived
    # from it).
    v2_enc = base64.b64encode(os.urandom(32)).decode()
    v2_idx = base64.b64encode(os.urandom(32)).decode()

    # "older" is written and backfilled BEFORE the rotation — ends up
    # under v1 and, per this scenario, is never explicitly rotated
    # afterward (a real rotation need not migrate every historical row
    # atomically — mixed versions coexisting IS the normal in-between
    # state, not an error case). "newer" arrives AFTER the active version
    # has moved to v2, so it is encrypted directly under v2 — no `--key-
    # version` rotation pass involved for either row, this is what "mixed
    # key versions" looks like from ordinary day-to-day writes.
    older = _insert_draft(conn, patient_id=patient_id, version=1, generated_text="Older draft, plaintext.")
    result = _run_backfill(env_overrides={
        "PHI_ACTIVE_KEY_VERSION": "v1", "PHI_ENCRYPTION_KEY_V1": v1_enc, "PHI_BLIND_INDEX_KEY_V1": v1_idx,
    })
    assert result.returncode == 0, result.stderr

    newer = _insert_draft(conn, patient_id=patient_id, version=2, generated_text="Newer draft, plaintext.")
    result = _run_backfill(env_overrides={
        "PHI_ACTIVE_KEY_VERSION": "v2",
        "PHI_ENCRYPTION_KEY_V1": v1_enc, "PHI_BLIND_INDEX_KEY_V1": v1_idx,
        "PHI_ENCRYPTION_KEY_V2": v2_enc, "PHI_BLIND_INDEX_KEY_V2": v2_idx,
    })
    assert result.returncode == 0, result.stderr

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, patient_id, version, generated_text, generated_text_key_version "
            "FROM agent_draft_provenance WHERE id IN (%s, %s) ORDER BY id",
            (older, newer),
        )
        rows = cur.fetchall()

    sys.path.insert(0, REPO_ROOT)
    from libs.phi_crypto import EnvKeyProvider, decrypt

    kp = EnvKeyProvider({
        "PHI_ACTIVE_KEY_VERSION": "v2",
        "PHI_ENCRYPTION_KEY_V1": v1_enc, "PHI_BLIND_INDEX_KEY_V1": v1_idx,
        "PHI_ENCRYPTION_KEY_V2": v2_enc, "PHI_BLIND_INDEX_KEY_V2": v2_idx,
    })

    decoded = {}
    for row_id, pid, version, ciphertext, key_version in rows:
        aad = f"agent_draft_provenance.generated_text.{pid}.{version}".encode()
        decoded[row_id] = (decrypt(ciphertext, kp.encryption_key(key_version), aad), key_version)

    assert decoded[older] == ("Older draft, plaintext.", "v1")  # still under the version it was written with
    assert decoded[newer] == ("Newer draft, plaintext.", "v2")  # written directly under the new active version

    # Now perform an EXPLICIT rotation of the v1 stragglers — proves
    # --key-version can sweep up exactly the still-v1 rows without
    # touching the already-v2 one.
    result = _run_backfill(
        "--key-version", "v1",
        env_overrides={
            "PHI_ACTIVE_KEY_VERSION": "v2",
            "PHI_ENCRYPTION_KEY_V1": v1_enc, "PHI_BLIND_INDEX_KEY_V1": v1_idx,
            "PHI_ENCRYPTION_KEY_V2": v2_enc, "PHI_BLIND_INDEX_KEY_V2": v2_idx,
        },
    )
    assert result.returncode == 0, result.stderr

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, patient_id, version, generated_text, generated_text_key_version "
            "FROM agent_draft_provenance WHERE id IN (%s, %s) ORDER BY id",
            (older, newer),
        )
        rows_after_rotation = cur.fetchall()

    decoded_after = {}
    for row_id, pid, version, ciphertext, key_version in rows_after_rotation:
        aad = f"agent_draft_provenance.generated_text.{pid}.{version}".encode()
        decoded_after[row_id] = (decrypt(ciphertext, kp.encryption_key(key_version), aad), key_version)

    assert decoded_after[older] == ("Older draft, plaintext.", "v2")  # rotated, plaintext unchanged
    assert decoded_after[newer] == ("Newer draft, plaintext.", "v2")  # untouched (was already v2)
