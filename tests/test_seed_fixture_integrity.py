"""Structural integrity of the four canonical demo fixtures (2026-08-22):
1042 (Maria Gonzalez), 1737 (Priya Khan), 1738 (Thomas Johnson), 1739 (Aisha
Taylor) — plus the "1330/1588 stay incomplete" and "generated matches
committed" invariants the rest of the fixture work depends on.

Pure text/subprocess checks against db/seed/generate_seed.py's OWN output — no
live Postgres required, so this runs in `make test`. Where correctness of a
record's *content* matters (quotable, computable, refusal-path), this drives
the real services/records-service/patient_summary.py parser against the
generated body text, not a regex guess at what that parser would do.
"""
import re
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import load_module

REPO_ROOT = Path(__file__).resolve().parents[1]
GENERATOR = REPO_ROOT / "db" / "seed" / "generate_seed.py"
COMMITTED_SEED = REPO_ROOT / "db" / "seed" / "seed.sql"

CANONICAL = (1042, 1737, 1738, 1739)


@pytest.fixture(scope="module")
def generated_sql() -> str:
    result = subprocess.run(
        [sys.executable, str(GENERATOR)], capture_output=True, text=True, check=True, cwd=REPO_ROOT
    )
    return result.stdout


def _block(sql: str, start_marker: str, end_marker: str) -> str:
    start = sql.index(start_marker)
    end = sql.index(end_marker, start)
    return sql[start:end]


# --- generated output matches the committed file ---------------------------- #


def test_generated_seed_matches_the_committed_file(generated_sql):
    """db/seed/seed.sql is GENERATED — hand-editing it drifts from the
    generator that supposedly produced it. This is that drift, caught."""
    committed = COMMITTED_SEED.read_text()
    assert generated_sql == committed, (
        "db/seed/seed.sql does not match `python3 db/seed/generate_seed.py` — "
        "regenerate with that command rather than hand-editing the SQL file"
    )


# --- each canonical patient has a real, curated row -------------------------- #


@pytest.mark.parametrize("patient_id,expected_name", [
    (1042, "Maria Gonzalez"),
    (1737, "Priya Khan"),
    (1738, "Thomas Johnson"),
    (1739, "Aisha Taylor"),
])
def test_each_canonical_patient_has_a_curated_demographics_row(generated_sql, patient_id, expected_name):
    pattern = re.compile(rf"^\s*\({patient_id}, '[^']+', '{re.escape(expected_name)}',", re.M)
    assert pattern.search(generated_sql), f"patient {patient_id} ({expected_name}) demographics row not found"


@pytest.mark.parametrize("patient_id", CANONICAL)
def test_each_canonical_patient_appears_exactly_once(generated_sql, patient_id):
    """A curated fixture id must never collide with a randomly-generated row —
    that would fail the actual INSERT outright, not merely look wrong."""
    block = _block(generated_sql, "INSERT INTO patients", "SELECT setval('patients_id_seq'")
    hits = re.findall(rf"^\s*\({patient_id}, '", block, re.M)
    assert len(hits) == 1, f"patient {patient_id} appears {len(hits)} times, expected exactly 1"


# 1042/1737 stay 'active' per their original requirement ("active insurance
# and eligibility data"). 1738/1739 were deliberately moved OFF 'active'
# (2026-08-23, W9.4): the Coverage & Eligibility workspace needs 'stale' and
# 'unknown' to be real, reachable states on a canonical patient rather than
# something only a random generated row happens to land on — see
# coverage-eligibility.md's essential test list. Each still has a member id
# on file, so "Request verification" is available from the seed's own
# starting state regardless of which status it starts on.
_EXPECTED_COVERAGE_STATUS = {1042: "active", 1737: "active", 1738: "stale", 1739: "unknown"}


@pytest.mark.parametrize("patient_id", CANONICAL)
def test_each_canonical_patient_has_the_expected_insurance_status(generated_sql, patient_id):
    block = _block(generated_sql, "INSERT INTO insurance_coverages", "\n\n")
    expected = _EXPECTED_COVERAGE_STATUS[patient_id]
    pattern = re.compile(rf"^\s*\({patient_id}, '[^']+', '[^']+', '[^']+', '[^']+', '{expected}',", re.M)
    assert pattern.search(block), f"patient {patient_id} has no {expected!r} insurance_coverages row"


@pytest.mark.parametrize("patient_id", CANONICAL)
def test_each_canonical_patient_has_at_least_three_encounters(generated_sql, patient_id):
    block = _block(generated_sql, "INSERT INTO encounters", "SELECT setval('encounters_id_seq'")
    hits = re.findall(rf"^\s*\(\d+, {patient_id}, ", block, re.M)
    assert len(hits) >= 3, f"patient {patient_id} has {len(hits)} encounters, need >= 3"


@pytest.mark.parametrize("patient_id", CANONICAL)
def test_each_canonical_patient_has_at_least_one_upcoming_and_one_completed_appointment(generated_sql, patient_id):
    block = _block(generated_sql, "INSERT INTO appointments", "\n\nINSERT INTO consents")
    rows = re.findall(rf"^\s*\({patient_id}, \d+, '[^']*', '[^']*', '[^']*', '[^']*', '(\w+)',", block, re.M)
    assert "completed" in rows, f"patient {patient_id} has no completed appointment: {rows}"
    assert "confirmed" in rows, f"patient {patient_id} has no upcoming (confirmed) appointment: {rows}"


@pytest.mark.parametrize("patient_id", CANONICAL)
def test_each_canonical_patient_has_both_required_consents(generated_sql, patient_id):
    block = _block(generated_sql, "INSERT INTO consents", "\n\n")
    assert re.search(rf"\({patient_id}, 'npp_ack'", block), f"patient {patient_id} missing npp_ack consent"
    assert re.search(rf"\({patient_id}, 'treatment_consent'", block), f"patient {patient_id} missing treatment_consent"


@pytest.mark.parametrize("patient_id", CANONICAL)
def test_each_canonical_patient_has_at_least_one_staff_or_clinician_grant(generated_sql, patient_id):
    block = _block(generated_sql, "INSERT INTO patient_access_grants", "\n\n")
    hits = re.findall(rf"\(\d+, {patient_id}\)", block)
    assert hits, f"patient {patient_id} has no patient_access_grants row at all"


# --- portal-account states, exactly as specified ----------------------------- #


def test_1042_and_1737_are_invite_ready_with_no_seeded_account(generated_sql):
    users_block = _block(generated_sql, "INSERT INTO users", "INSERT INTO patients")
    for pid in (1042, 1737):
        assert f"'patient-{pid}'" not in users_block, f"patient {pid} must have no pre-seeded portal account"


def test_1738_and_1739_have_active_pre_seeded_accounts_with_correct_full_name(generated_sql):
    tail = generated_sql[generated_sql.index("INSERT INTO patients"):]
    assert re.search(r"\(\d+, 'patient-1738', '[^']+', 'Thomas Johnson', 'patient', 1738, now\(\)\)", tail), (
        "patient-1738 account missing, or full_name is not 'Thomas Johnson'"
    )
    assert re.search(r"\(\d+, 'patient-1739', '[^']+', 'Aisha Taylor', 'patient', 1739, now\(\)\)", tail), (
        "patient-1739 account missing, or full_name is not 'Aisha Taylor'"
    )


def test_patient_demo_password_meets_the_twelve_character_activation_floor():
    module = load_module("db/seed/generate_seed.py", "generate_seed_pw_check")
    assert len(module.PATIENT_DEMO_PASSWORD) >= 12
    assert module.PATIENT_DEMO_PASSWORD != module.DEMO_PASSWORD, (
        "the patient demo password must be distinct from the staff one"
    )


# --- 1330/1588 stay incomplete, untouched by the curated-fixture work ------- #


def test_1330_and_1588_are_not_curated(generated_sql):
    module = load_module("db/seed/generate_seed.py", "generate_seed_curated_check")
    assert 1330 not in module.CURATED_IDS
    assert 1588 not in module.CURATED_IDS


def test_1330_and_1588_have_no_curated_trend_or_narrative_records(generated_sql):
    """They keep whatever the RANDOM generator gives them — this is the
    "intentionally incomplete duplicate candidate" property, not a gap to
    close. Specifically: neither carries one of the curated trend titles."""
    block = _block(generated_sql, "INSERT INTO records", "SELECT setval('records_id_seq'")
    for pid in (1330, 1588):
        for title in ("LDL", "A1c", "Systolic BP", "SpO2"):
            assert not re.search(rf"\(\d+, \d+, {pid}, 'lab_result', '{title}',", block), (
                f"patient {pid} unexpectedly carries a curated '{title}' trend"
            )


# --- quotable, computable, and refusal-path data, verified against the REAL
# patient_summary.py parser rather than a regex guess at its behaviour -------- #


@pytest.fixture(scope="module")
def patient_summary_module():
    return load_module("services/records-service/patient_summary.py", "patient_summary_seed_check")


CURATED_TREND_PAIRS = {
    1042: ("LDL 162 mg/dL.", "LDL 118 mg/dL."),
    1737: ("7.5%.", "6.2%."),
    1738: ("Systolic BP 158 mmHg.", "Systolic BP 132 mmHg."),
    1739: ("SpO2 90%.", "SpO2 96%."),
}


@pytest.mark.parametrize("patient_id,pair", CURATED_TREND_PAIRS.items())
def test_each_canonical_trend_is_quotable_and_computable(patient_summary_module, patient_id, pair):
    ps = patient_summary_module
    early, later = pair
    assert ps.classify(early) == ps.ResultShape.SINGLE_VALUE
    assert ps.classify(later) == ps.ResultShape.SINGLE_VALUE
    early_m = ps.parse_measurements(early)[0]
    later_m = ps.parse_measurements(later)[0]
    change = ps.compute_change(later_m, early_m, prior_record_id=1, prior_date="2026-01-01")
    assert change is not None, f"patient {patient_id}'s trend did not compute a change: {pair}"
    assert change.unit in ps._UNITS_SAFE_FOR_ARITHMETIC


@pytest.mark.parametrize("patient_id", CANONICAL)
def test_each_canonical_patient_has_a_refusal_path_note(generated_sql, patient_summary_module, patient_id):
    """A kind='note' record is UNQUOTABLE by construction — never in
    _QUOTABLE_KINDS — which is what routes it to the clinician review queue."""
    block = _block(generated_sql, "INSERT INTO records", "SELECT setval('records_id_seq'")
    notes = re.findall(rf"\(\d+, \d+, {patient_id}, 'note', 'Visit note', '([^']+)',", block)
    assert notes, f"patient {patient_id} has no narrative/note record"
    for body in notes:
        assert patient_summary_module.classify(body, kind="note") == patient_summary_module.ResultShape.UNQUOTABLE
