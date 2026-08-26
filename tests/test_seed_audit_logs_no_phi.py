"""AUD-M01 (code review, P3 w8-planner-2, 2026-08-26): db/seed/seed.sql's
audit_logs INSERT must never carry a raw patient name, DOB, or SSN, or a raw
request body — the exact shape the pre-DEBT-D1 teaching fixture used to
have before db/seed/generate_seed.py was corrected to log the same
metadata-only shape services/intake-service/app.py's real
_INTAKE_LOG_SUMMARY_KEYS allowlist does. Static content check, no database
needed — the matching real-Postgres proof (that migration 026 also scrubs
an already-seeded legacy row, and the resulting chain still verifies) is
tests/integration/test_audit_logs_append_only.py::
test_migration_026_scrubs_the_known_legacy_phi_row_before_027_backfills.
"""
import os
import re

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SEED_SQL = os.path.join(_REPO_ROOT, "db", "seed", "seed.sql")

_SSN_PATTERN = re.compile(r"\d{3}-\d{2}-\d{4}")
_PHI_MARKERS = ("Maria Gonzalez", '"dob"', '"ssn"', "body={")


def _audit_logs_insert_block():
    with open(_SEED_SQL, encoding="utf-8") as f:
        content = f.read()
    start = content.index("INSERT INTO audit_logs")
    end = content.index(";", start)
    return content[start:end]


def test_seed_audit_logs_has_no_ssn_shaped_content():
    assert not _SSN_PATTERN.search(_audit_logs_insert_block())


def test_seed_audit_logs_has_no_known_phi_markers():
    block = _audit_logs_insert_block()
    for marker in _PHI_MARKERS:
        assert marker not in block, f"found {marker!r} in the audit_logs seed data"


def test_seed_audit_logs_matches_the_real_allowlist_shape():
    # services/intake-service/app.py's _INTAKE_LOG_SUMMARY_KEYS is exactly
    # {"correlation_id", "created_via"} — the seed row should look like
    # something that allowlist could actually have produced.
    block = _audit_logs_insert_block()
    assert "correlation_id=" in block
    assert "created_via=" in block
