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
import shutil
import subprocess
import tempfile

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT = os.path.join(_REPO_ROOT, "db", "migrations", "scripts", "bootstrap_admin_role.sh")

_FAKE_DOCKER_MARKER = "FAKE_DOCKER_INVOKED"
_FAKE_DOCKER_EXIT_CODE = 7


def _run(env_overrides, fake_docker=False):
    # A minimal PATH (just enough for bash's own builtins/coreutils) — never
    # inherits the real shell's DB_ADMIN_PASSWORD/DB_PASSWORD. Where the real
    # `docker` binary lives is OS-dependent (confirmed: CI's ubuntu-latest
    # has it on /usr/bin, unlike this repo's own dev host) — a PATH that
    # merely omits docker's directory is not portable proof the script never
    # reached it. `fake_docker=True` instead prepends a directory containing
    # a deterministic stub `docker` script, so "did the real work start" is
    # provable regardless of what's actually installed.
    env = {"PATH": "/usr/bin:/bin"}
    tmpdir = None
    if fake_docker:
        tmpdir = tempfile.mkdtemp()
        fake_docker_path = os.path.join(tmpdir, "docker")
        with open(fake_docker_path, "w", encoding="utf-8") as f:
            f.write(f"#!/bin/sh\necho {_FAKE_DOCKER_MARKER} >&2\nexit {_FAKE_DOCKER_EXIT_CODE}\n")
        os.chmod(fake_docker_path, 0o755)
        env["PATH"] = f"{tmpdir}:{env['PATH']}"
    env.update(env_overrides)
    try:
        return subprocess.run(
            ["bash", _SCRIPT], env=env, capture_output=True, text=True, timeout=10
        )
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir)


def test_missing_db_admin_password_fails_closed_before_touching_docker():
    result = _run({"DB_PASSWORD": "some-app-secret"}, fake_docker=True)

    assert result.returncode != 0
    assert "DB_ADMIN_PASSWORD is not set" in result.stderr
    # Never claims to have created or applied anything, and never even
    # reaches the fake docker stub.
    assert "Creating the admin role" not in result.stdout
    assert _FAKE_DOCKER_MARKER not in result.stderr


def test_equal_admin_and_app_passwords_fail_closed_before_touching_docker():
    result = _run({"DB_PASSWORD": "same-secret", "DB_ADMIN_PASSWORD": "same-secret"}, fake_docker=True)

    assert result.returncode != 0
    assert "must be distinct from DB_PASSWORD" in result.stderr
    assert "Creating the admin role" not in result.stdout
    assert _FAKE_DOCKER_MARKER not in result.stderr


def test_distinct_passwords_pass_the_guard_and_reach_docker():
    # Doesn't need Docker/Postgres to actually be up — only that the script
    # gets PAST its own credential guard and attempts the real work. The
    # fake `docker` stub proves that deterministically: if the guard had
    # rejected this (legitimate, distinct-password) case, the stub would
    # never run at all.
    result = _run({"DB_PASSWORD": "app-secret", "DB_ADMIN_PASSWORD": "admin-secret"}, fake_docker=True)

    assert "DB_ADMIN_PASSWORD is not set" not in result.stderr
    assert "must be distinct from DB_PASSWORD" not in result.stderr
    assert "Creating the admin role" in result.stdout
    assert _FAKE_DOCKER_MARKER in result.stderr
    assert result.returncode == _FAKE_DOCKER_EXIT_CODE
