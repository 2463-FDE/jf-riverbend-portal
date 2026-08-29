"""adr/0012 follow-up — agent_draft_provenance.generated_text encryption.

SQLite-level coverage of the crypto/wiring properties themselves: migration
020's TRIGGER and CHECK constraints are Postgres-only and covered instead
by tests/integration/test_agent_draft_text_encryption_contract.py (real
Postgres) and the manual verification recorded in that file's docstring.
Approval/status-transition behavior with encryption wired in is already
covered end to end by test_agent_draft_write_path.py and
test_agent_portal_path.py (both updated for the ciphertext-at-rest shape,
not duplicated here); this file is specifically about the properties the
review comment asked for that nothing else already proves.
"""
import base64
import logging
import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from conftest import load_module
from libs.phi_crypto import DecryptionError, EnvKeyProvider, UnknownKeyVersionError

drafts = load_module("services/records-service/agent_drafts.py", "agent_drafts_mod")

TEXT = "Your A1c is 6.2%, down from 7.5% in March."
CORR = "corr-encryption-test"

_ENC_KEY = base64.b64encode(os.urandom(32)).decode()
_IDX_KEY = base64.b64encode(os.urandom(32)).decode()
_TEST_PHI_PROVIDER = EnvKeyProvider(
    {"PHI_ACTIVE_KEY_VERSION": "v1", "PHI_ENCRYPTION_KEY_V1": _ENC_KEY, "PHI_BLIND_INDEX_KEY_V1": _IDX_KEY}
)


@pytest.fixture(autouse=True)
def _configured_phi_key_provider(monkeypatch):
    # Per-test, via monkeypatch — not a bare module-level assignment. This
    # module's own phi._key_provider slot is shared with every OTHER test
    # file that also loads services/records-service/agent_drafts.py or
    # app.py (conftest.load_module reuses an already-correct sys.modules
    # entry rather than re-evicting it) — a bare assignment here would be
    # in exactly the same "last collected write wins" race that broke
    # tests/test_records_reconciliation_route.py until it was fixed the
    # same way. Auto-reverted after each test by monkeypatch.
    monkeypatch.setattr(drafts.phi, "_key_provider", _TEST_PHI_PROVIDER)


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    drafts.AgentDraftProvenance.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _create(db, text=TEXT, patient_id=1042, correlation_id=CORR):
    return drafts.create_draft(
        db, patient_id=patient_id, generated_text=text, correlation_id=correlation_id,
        provenance_label=drafts.LABEL_REAL, model_id="model-x", prompt_version="v1",
    )


# --- 1. the database stores no plaintext ------------------------------------ #


def test_the_stored_row_is_not_plaintext(db):
    draft = _create(db)

    assert draft.generated_text != TEXT
    assert TEXT not in draft.generated_text
    assert draft.generated_text_key_version == "v1"


def test_the_same_text_encrypted_twice_produces_different_ciphertext(db):
    # Two different drafts (different versions -> different AAD), same
    # text — proves the encryption is not a deterministic/lookup-style
    # scheme that would leak "these two drafts say the same thing".
    d1 = _create(db, patient_id=1042)
    # v2, same patient, same text — its own distinct, server-generated
    # lifecycle id (migration 036, review fix ALC-CORR-COLLISION).
    d2 = _create(db, patient_id=1042, correlation_id="corr-agent-2")

    assert d1.generated_text != d2.generated_text


# --- 2. authorized readers receive the exact original text ------------------ #


def test_the_stored_text_decrypts_back_to_the_original(db):
    draft = _create(db)

    recovered = drafts.phi.decrypt_draft_text(
        draft.patient_id, draft.version, draft.generated_text, draft.generated_text_key_version
    )
    assert recovered == TEXT


# --- 3 & AAD binding: cannot decrypt under the wrong identity --------------- #


def test_decrypting_under_a_different_patient_id_fails_closed(db):
    draft = _create(db, patient_id=1042)

    with pytest.raises(DecryptionError):
        drafts.phi.decrypt_draft_text(
            9999, draft.version, draft.generated_text, draft.generated_text_key_version
        )


def test_decrypting_under_a_different_version_fails_closed(db):
    draft = _create(db, patient_id=1042)

    with pytest.raises(DecryptionError):
        drafts.phi.decrypt_draft_text(
            draft.patient_id, draft.version + 1, draft.generated_text, draft.generated_text_key_version
        )


def test_decrypting_without_the_right_key_fails_closed(db):
    draft = _create(db)
    wrong_provider = EnvKeyProvider(
        {
            "PHI_ACTIVE_KEY_VERSION": "v1",
            "PHI_ENCRYPTION_KEY_V1": base64.b64encode(os.urandom(32)).decode(),
            "PHI_BLIND_INDEX_KEY_V1": base64.b64encode(os.urandom(32)).decode(),
        }
    )
    from libs.phi_crypto import decrypt

    with pytest.raises(DecryptionError):
        decrypt(
            draft.generated_text,
            wrong_provider.encryption_key("v1"),
            f"agent_draft_provenance.generated_text.{draft.patient_id}.{draft.version}".encode(),
        )


# --- 4. ciphertext tampering fails ------------------------------------------ #


def test_tampered_ciphertext_fails_closed(db):
    draft = _create(db)
    raw = bytearray(base64.b64decode(draft.generated_text))
    raw[-1] ^= 0xFF
    tampered = base64.b64encode(bytes(raw)).decode()

    with pytest.raises(DecryptionError):
        drafts.phi.decrypt_draft_text(draft.patient_id, draft.version, tampered, draft.generated_text_key_version)


# --- 5. unknown key version fails closed ------------------------------------ #


def test_an_unconfigured_key_version_fails_closed_not_silently(db):
    draft = _create(db)

    with pytest.raises(UnknownKeyVersionError):
        drafts.phi.decrypt_draft_text(draft.patient_id, draft.version, draft.generated_text, "v9")


def test_a_row_with_no_key_version_is_treated_as_pre_migration_plaintext(db):
    # Contract for db/migrations/scripts/encrypt_agent_draft_text.py's
    # target rows: NULL key_version means "not yet backfilled", and the
    # stored value IS the plaintext — returned as-is, not "fails closed"
    # (there is nothing to decrypt).
    plain = "A legacy plaintext row, pre-migration-032."
    assert drafts.phi.decrypt_draft_text(1042, 1, plain, None) == plain


# --- 10. no plaintext ever reaches a log line ------------------------------- #


def test_create_draft_never_logs_the_plaintext(db, caplog):
    with caplog.at_level(logging.INFO):
        _create(db, text="A very specific unique marker sentence 8f3c1a.")

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "A very specific unique marker sentence 8f3c1a" not in logged


def test_decrypt_draft_text_never_logs_the_plaintext(db, caplog):
    draft = _create(db, text="Another unique marker 92dd4e.")

    with caplog.at_level(logging.INFO):
        recovered = drafts.phi.decrypt_draft_text(
            draft.patient_id, draft.version, draft.generated_text, draft.generated_text_key_version
        )

    assert recovered == "Another unique marker 92dd4e."
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "Another unique marker 92dd4e." not in logged
