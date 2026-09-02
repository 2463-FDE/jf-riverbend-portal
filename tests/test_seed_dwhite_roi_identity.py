"""Demo-readiness slice: db/seed/generate_seed.py and db/seed/demo_reset.sql
seed and restore `dwhite` (Dana White), a least-privilege ROI demo identity
on the real `roi_clerk` role, scoped to exactly one active
patient_access_grants row (patient 1042) — without disturbing `roiclerk`
(the pre-existing legacy-`staff`-role account for the same demo person) or
any other legacy account.

Pure text/subprocess checks against generate_seed.py's own output and
demo_reset.sql's own text, matching test_seed_fixture_integrity.py's
established no-live-Postgres pattern.
"""
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GENERATOR = REPO_ROOT / "db" / "seed" / "generate_seed.py"
DEMO_RESET = REPO_ROOT / "db" / "seed" / "demo_reset.sql"


def _generated_sql() -> str:
    result = subprocess.run(
        [sys.executable, str(GENERATOR)], capture_output=True, text=True, check=True, cwd=REPO_ROOT
    )
    return result.stdout


def test_dwhite_seeded_with_the_real_roi_clerk_role():
    sql = _generated_sql()
    users_block = sql[sql.index("INSERT INTO users"):sql.index("INSERT INTO users") + 4000]
    match = re.search(r"\(\d+, 'dwhite', '[^']+', 'Dana White', '([a-z_]+)', now\(\)\)", users_block)
    assert match is not None, "dwhite must be seeded as a real users row"
    assert match.group(1) == "roi_clerk", (
        f"dwhite must carry the real 'roi_clerk' role, not the deprecated flat 'staff' role — got {match.group(1)!r}"
    )


def test_roiclerk_legacy_account_is_unchanged():
    sql = _generated_sql()
    users_block = sql[sql.index("INSERT INTO users"):sql.index("INSERT INTO users") + 4000]
    match = re.search(r"\(\d+, 'roiclerk', '[^']+', 'Dana White \(ROI Clerk\)', '([a-z_]+)', now\(\)\)", users_block)
    assert match is not None, "roiclerk must still be seeded exactly as before"
    assert match.group(1) == "staff", (
        "roiclerk must stay on the deprecated legacy 'staff' role — this PR adds dwhite, it does not migrate roiclerk"
    )


def test_dwhite_has_exactly_one_grant_scoped_to_1042():
    sql = _generated_sql()
    grants_block = sql[sql.index("INSERT INTO patient_access_grants"):]
    grants_block = grants_block[: grants_block.index(";") + 1]
    dwhite_user_id_match = re.search(r"\((\d+), 'dwhite', '[^']+', 'Dana White', 'roi_clerk', now\(\)\)", sql)
    assert dwhite_user_id_match is not None
    dwhite_user_id = dwhite_user_id_match.group(1)

    dwhite_grants = re.findall(rf"\({dwhite_user_id}, (\d+)\)", grants_block)
    assert dwhite_grants == ["1042"], (
        f"dwhite must hold exactly one grant, scoped to patient 1042 only — found grants for {dwhite_grants}"
    )


def test_demo_reset_restores_dwhites_scoped_grant():
    text = DEMO_RESET.read_text()
    assert text.count("('dwhite', 1042)") == 2, (
        "demo_reset.sql must list ('dwhite', 1042) in both the revoke-clearing and "
        "insert-missing patient_access_grants CTEs, the same restore mechanism every "
        "other fixture grant gets"
    )
    # Never a grant for dwhite outside 1042 — a typo here would silently widen the demo scope.
    assert not re.search(r"\('dwhite', (?!1042\))\d+\)", text)


def test_demo_reset_reactivates_dwhite_account():
    text = DEMO_RESET.read_text()
    active_restore = re.search(
        r"UPDATE users SET is_active = TRUE\s+"
        r"WHERE username IN \(([^)]*)\) AND is_active = FALSE;",
        text,
    )
    assert active_restore is not None
    assert "'dwhite'" in active_restore.group(1)
