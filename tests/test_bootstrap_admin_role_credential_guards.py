"""P3 (w8-planner-2, AUD-B01 round 2 review): db/migrations/scripts/
bootstrap_admin_role.sh must fail closed BEFORE ever touching Postgres when
DB_ADMIN_PASSWORD is missing or equal to DB_PASSWORD — both checks run at
the very top of the script, ahead of any `docker compose exec` call, so
these are plain subprocess tests: no database, no Docker, no `-m
integration` needed. The matching real-Postgres behavior — the admin role
actually being created (or refused) — is exercised directly in
tests/integration/test_admin_runtime_role_separation.py against
db/migrations/scripts/create_admin_role.sql's own equal-password guard.
"""
import os
import subprocess

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT = os.path.join(_REPO_ROOT, "db", "migrations", "scripts", "bootstrap_admin_role.sh")


def _run(env_overrides):
    # A minimal, deliberately DOCKER-LESS PATH (just enough for bash's own
    # builtins/coreutils) — never inherits the real shell's
    # DB_ADMIN_PASSWORD/DB_PASSWORD, and guarantees a run that gets past the
    # credential guard fails at "docker: command not found" rather than
    # actually invoking `docker compose exec` against whatever project
    # happens to be live in the test-running host's current directory.
    env = {"PATH": "/usr/bin:/bin"}
    env.update(env_overrides)
    return subprocess.run(
        ["bash", _SCRIPT], env=env, capture_output=True, text=True, timeout=10
    )


def test_missing_db_admin_password_fails_closed_before_touching_docker():
    result = _run({"DB_PASSWORD": "some-app-secret"})

    assert result.returncode != 0
    assert "DB_ADMIN_PASSWORD is not set" in result.stderr
    # Never claims to have created or applied anything.
    assert "Creating the admin role" not in result.stdout


def test_equal_admin_and_app_passwords_fail_closed_before_touching_docker():
    result = _run({"DB_PASSWORD": "same-secret", "DB_ADMIN_PASSWORD": "same-secret"})

    assert result.returncode != 0
    assert "must be distinct from DB_PASSWORD" in result.stderr
    assert "Creating the admin role" not in result.stdout


def test_distinct_passwords_pass_the_guard_and_reach_docker():
    # Doesn't need Docker/Postgres to actually be up — only that the script
    # gets PAST its own credential guard and attempts the real work. It then
    # fails for an unrelated, deliberately engineered reason (`docker` is
    # not on this test's minimal PATH), proving the guard itself let a
    # legitimate distinct-password case through rather than actually
    # exercising `docker compose exec` against whatever project happens to
    # be live in the test-running host's current directory.
    result = _run({"DB_PASSWORD": "app-secret", "DB_ADMIN_PASSWORD": "admin-secret"})

    assert "DB_ADMIN_PASSWORD is not set" not in result.stderr
    assert "must be distinct from DB_PASSWORD" not in result.stderr
    assert "Creating the admin role" in result.stdout
    assert result.returncode != 0
    assert "docker" in result.stderr.lower()  # "command not found", not a credential rejection
