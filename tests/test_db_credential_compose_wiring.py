"""W10 Final 2 Stage 4 review fix — docker-compose.yml wiring for the
runtime database credential (DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD).

Root cause of the full-stack-verification CI failure this fixes: gateway,
intake-service, records-service, scheduling-service, and roi-service all
connect to Postgres directly (see each service's own config.py), but none
of them had their own DB_* entries in docker-compose.yml — they relied
entirely on `env_file: .env` for these. A clean checkout with no `.env`
(any fresh CI runner) left DB_PASSWORD empty in every one of those five
containers while postgres's own required POSTGRES_PASSWORD
(${DB_PASSWORD:?...}) set the REAL runtime-role password — a password
mismatch, not a startup race, that fails every real query with a
persistent OperationalError.

Runs real `docker compose config` invocations with a fully-isolated
environment (`--env-file /dev/null` plus an explicit env dict), mirroring
tests/test_phi_compose_wiring.py's own established pattern — never the
ambient test-runner environment or this worktree's own `.env`.
"""
import json
import os
import pathlib
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]

_DB_CLIENT_SERVICES = {"gateway", "intake-service", "records-service", "scheduling-service", "roi-service"}
_NON_DB_SERVICES = {"eligibility-service", "interop-service", "frontend"}

_VALID_ENV = {
    "INTERNAL_SERVICE_TOKEN": "test-internal-token-well-over-the-32-char-floor",
    "DB_PASSWORD": "test-db-password",
    "DB_ADMIN_PASSWORD": "test-db-admin-password",
    "PHI_ACTIVE_KEY_VERSION": "v1",
    "PHI_ENCRYPTION_KEY_V1": "MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTIzNDU2Nzg5MDE=",
    "PHI_BLIND_INDEX_KEY_V1": "OTg3NjU0MzIxMDk4NzY1NDMyMTA5ODc2NTQzMjEwOTg=",
}


def _docker_available():
    try:
        subprocess.run(["docker", "--version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


pytestmark = pytest.mark.skipif(not _docker_available(), reason="docker CLI not available")


def _resolved_config(env_overrides=None):
    env = dict(_VALID_ENV)
    if env_overrides:
        for key, value in env_overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
    full_env = {"PATH": os.environ.get("PATH", "")}
    full_env.update(env)
    result = subprocess.run(
        ["docker", "compose", "--env-file", "/dev/null", "config", "--format", "json"],
        cwd=REPO, env=full_env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_every_db_client_service_receives_the_runtime_db_password():
    config = _resolved_config()
    for name in _DB_CLIENT_SERVICES:
        env = config["services"][name].get("environment", {})
        assert env.get("DB_PASSWORD") == "test-db-password", (
            f"{name} connects to Postgres directly but did not receive the resolved DB_PASSWORD "
            f"(would connect with an empty/wrong password on a clean checkout with no .env)"
        )
        assert env.get("DB_HOST") == "postgres"
        assert env.get("DB_PORT") == "5432"
        assert env.get("DB_NAME") == "riverbend"
        assert env.get("DB_USER") == "riverbend_app"


def test_missing_db_password_fails_compose_for_every_db_client_service():
    for name in _DB_CLIENT_SERVICES:
        env = dict(_VALID_ENV)
        del env["DB_PASSWORD"]
        full_env = {"PATH": os.environ.get("PATH", "")}
        full_env.update(env)
        result = subprocess.run(
            ["docker", "compose", "--env-file", "/dev/null", "config", "-q"],
            cwd=REPO, env=full_env, capture_output=True, text=True,
        )
        assert result.returncode != 0, f"expected a missing DB_PASSWORD to fail compose config for {name}"
        assert "DB_PASSWORD" in result.stderr


def _raw_service_environment_keys(service_name):
    """The keys THIS service's own `environment:` mapping declares in the
    source docker-compose.yml, before any merge with an env_file — that
    merge is a separate, pre-existing mechanism (every service using
    `env_file: .env` inherits whatever a real .env on disk happens to
    contain, whether or not that service's own environment: block needs
    it) and is not what these two tests are checking. What matters here is
    only this repo's own explicit wiring choice for each service."""
    yaml = pytest.importorskip("yaml")
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text())
    env = compose["services"][service_name].get("environment", {}) or {}
    return set(env)


def test_non_database_services_do_not_receive_db_password():
    for name in _NON_DB_SERVICES:
        keys = _raw_service_environment_keys(name)
        assert "DB_PASSWORD" not in keys, (
            f"{name} does not connect to Postgres and must not be explicitly wired with DB_PASSWORD"
        )


def test_no_application_service_receives_the_admin_password():
    """DB_ADMIN_PASSWORD is Postgres bootstrap/administration only — every
    application service must connect as the least-privilege runtime role."""
    yaml = pytest.importorskip("yaml")
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text())
    for name in compose["services"]:
        if name == "postgres":
            continue
        keys = _raw_service_environment_keys(name)
        assert "DB_ADMIN_PASSWORD" not in keys, f"{name} must never be explicitly wired with DB_ADMIN_PASSWORD"


def test_postgres_itself_still_receives_both_credentials():
    config = _resolved_config()
    env = config["services"]["postgres"].get("environment", {})
    assert env.get("DB_ADMIN_PASSWORD") == "test-db-admin-password"
    assert env.get("DB_APP_PASSWORD") == "test-db-password"
