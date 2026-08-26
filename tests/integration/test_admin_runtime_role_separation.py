"""Integration test — requires a real Postgres (`make up`). P3 audit
integrity (w8-planner-2): closes AUD-B01, a code review finding on PR #84 —
`riverbend_app` currently OWNS every table it creates (it is this cluster's
original bootstrap role), and a table's OWNER can always `ALTER TABLE ...
DISABLE TRIGGER` on it regardless of any REVOKE, which made 026's
append-only guard on `audit_logs` bypassable by the very role that has real
database access in this system.

028_admin_runtime_role_separation.sql fixes this by transferring ownership
of application objects to a separate admin role and demoting the runtime
role to a non-owner with only the grants it actually needs. This file
proves that design's actual PRIVILEGE semantics using entirely DISPOSABLE
roles and a disposable schema/table — never the real `riverbend_app`/
`riverbend_admin` roles or the real `public.audit_logs` table, and never
literally running 028 against them (which the current session is explicitly
not authorized to do against the live shared database).

Roles are cluster-wide in Postgres (not schema-scoped), so this file's
setup/teardown creates and drops two uniquely named disposable roles per
test run, in addition to a disposable schema — bootstrapped using the
current `DB_USER` credential, which on today's real, not-yet-migrated
database is still the original bootstrap role and can create/drop roles
(exactly the fact AUD-B01 is about).

Round-3 review: the tests above prove the PRIVILEGE MODEL against a
hand-built table. The two tests near the bottom of this file
(`test_the_real_fresh_install_files_grant_a_non_default_runtime_role_correctly`,
`test_the_real_028_migration_transitions_a_non_default_legacy_role_correctly`)
instead execute the ACTUAL db/schema.sql / db/docker-init/*.sh /
db/migrations/028_*.sql files, using role names that are NOT this repo's
own defaults — proving no role name is hard-coded anywhere in them. Those
two run against a throwaway, fully isolated Docker container each (removed
in `finally` regardless of outcome), never the shared local Postgres this
module's other tests use — schema.sql always targets the `public` schema
by name with no schema-qualification parameter, so running it for real
against a schema inside the shared database would grant a throwaway role
broad access to every real table there. Skipped automatically if the
`docker` CLI is not on PATH.

Run with:  pytest -m integration tests/integration/test_admin_runtime_role_separation.py
Skipped by default in CI (`pytest -m "not integration"`).
"""
import contextlib
import os
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")

pytestmark = pytest.mark.integration


def _bare_connection(user=None, password=None):
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"), port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "riverbend"),
        user=user or os.getenv("DB_USER", "riverbend_app"),
        password=password if password is not None else os.getenv("DB_PASSWORD", "changeme"),
    )


@contextlib.contextmanager
def _disposable_roles_and_schema():
    """Creates two disposable roles (an admin-equivalent superuser and a
    demoted runtime role, mirroring 028's end state) plus a disposable
    schema containing an audit_logs-shaped table with the SAME append-only
    trigger 026 defines, owned by the disposable admin and granted to the
    disposable app role exactly the way 028 grants the real one — INSERT +
    SELECT only, no UPDATE/DELETE, no ownership.

    Yields (admin_conn, app_conn). Bootstrapped and torn down using the
    current DB_USER credential, which is still cluster-superuser-equivalent
    on today's real, not-yet-migrated database — never touching the real
    riverbend_app/riverbend_admin roles or the real public.audit_logs."""
    admin_role = f"test_admin_{uuid.uuid4().hex[:10]}"
    app_role = f"test_app_{uuid.uuid4().hex[:10]}"
    admin_password = uuid.uuid4().hex
    app_password = uuid.uuid4().hex
    schema = f"role_sep_test_{uuid.uuid4().hex[:10]}"

    boot = _bare_connection()
    boot.autocommit = True
    admin_conn = None
    app_conn = None
    try:
        with boot.cursor() as cur:
            cur.execute(
                f'CREATE ROLE "{admin_role}" WITH LOGIN PASSWORD %s SUPERUSER', (admin_password,)
            )
            cur.execute(
                f'CREATE ROLE "{app_role}" WITH LOGIN PASSWORD %s '
                "NOSUPERUSER NOCREATEDB NOCREATEROLE", (app_password,)
            )

        admin_conn = _bare_connection(user=admin_role, password=admin_password)
        admin_conn.autocommit = True
        with admin_conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA {schema} AUTHORIZATION "{admin_role}"')
            cur.execute(f"SET search_path TO {schema}, public")
            cur.execute(
                "CREATE TABLE IF NOT EXISTS audit_logs ("
                "  id SERIAL PRIMARY KEY,"
                "  actor TEXT,"
                "  message TEXT,"
                "  created_at TIMESTAMPTZ NOT NULL DEFAULT now()"
                ")"
            )
            # Same append-only trigger 026 defines on the real table — the
            # actual object whose DISABLE TRIGGER is what AUD-B01 is about.
            cur.execute(
                "CREATE OR REPLACE FUNCTION audit_logs_reject_mutation() RETURNS TRIGGER AS $$ "
                "BEGIN RAISE EXCEPTION 'audit_logs is append-only: % is not permitted', TG_OP; "
                "END; $$ LANGUAGE plpgsql"
            )
            cur.execute(
                "CREATE TRIGGER audit_logs_no_update BEFORE UPDATE ON audit_logs "
                "FOR EACH ROW EXECUTE FUNCTION audit_logs_reject_mutation()"
            )
            cur.execute(
                "CREATE TRIGGER audit_logs_no_delete BEFORE DELETE ON audit_logs "
                "FOR EACH ROW EXECUTE FUNCTION audit_logs_reject_mutation()"
            )
            # 028's exact runtime grant for audit_logs: INSERT + SELECT only,
            # plus sequence USAGE for the SERIAL id column (a separate grant
            # in Postgres — table INSERT alone does not imply it).
            cur.execute(f'GRANT USAGE ON SCHEMA {schema} TO "{app_role}"')
            cur.execute(f'GRANT SELECT, INSERT ON audit_logs TO "{app_role}"')
            cur.execute(f'GRANT USAGE, SELECT ON audit_logs_id_seq TO "{app_role}"')

        app_conn = _bare_connection(user=app_role, password=app_password)
        with app_conn.cursor() as cur:
            cur.execute(f"SET search_path TO {schema}, public")
        app_conn.commit()

        yield admin_conn, app_conn
    finally:
        if app_conn is not None:
            app_conn.close()
        cleanup = admin_conn if admin_conn is not None else boot
        cleanup.autocommit = True
        with cleanup.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        if admin_conn is not None:
            admin_conn.close()
        with boot.cursor() as cur:
            cur.execute(f'DROP ROLE IF EXISTS "{app_role}"')
            cur.execute(f'DROP ROLE IF EXISTS "{admin_role}"')
        boot.close()


def test_runtime_role_can_insert_and_select_an_audit_event():
    with _disposable_roles_and_schema() as (_admin_conn, app_conn):
        with app_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO audit_logs (actor, message) VALUES (%s, %s) RETURNING id",
                ("roletest", "runtime insert"),
            )
            row_id = cur.fetchone()[0]
        app_conn.commit()

        with app_conn.cursor() as cur:
            cur.execute("SELECT actor, message FROM audit_logs WHERE id = %s", (row_id,))
            assert cur.fetchone() == ("roletest", "runtime insert")


def test_runtime_role_update_and_delete_are_denied_at_the_grant_level():
    # Distinct from 026's trigger rejection (tested elsewhere): this proves
    # the GRANT itself doesn't include UPDATE/DELETE for the runtime role —
    # denial happens before any trigger even gets a chance to fire.
    with _disposable_roles_and_schema() as (_admin_conn, app_conn):
        with app_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO audit_logs (actor, message) VALUES (%s, %s) RETURNING id",
                ("roletest", "to be attacked"),
            )
            row_id = cur.fetchone()[0]
        app_conn.commit()

        with app_conn.cursor() as cur:
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                cur.execute("UPDATE audit_logs SET message = 'tampered' WHERE id = %s", (row_id,))
        app_conn.rollback()

        with app_conn.cursor() as cur:
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                cur.execute("DELETE FROM audit_logs WHERE id = %s", (row_id,))
        app_conn.rollback()


def test_runtime_role_cannot_disable_the_append_only_trigger():
    # The actual AUD-B01 proof: ALTER TABLE requires ownership, not a
    # grantable privilege, so a non-owner runtime role cannot disable
    # audit_logs's append-only trigger even though 026's trigger exists and
    # even though this exact role has INSERT/SELECT on the table.
    with _disposable_roles_and_schema() as (_admin_conn, app_conn):
        with app_conn.cursor() as cur:
            with pytest.raises(psycopg2.errors.InsufficientPrivilege, match="owner"):
                cur.execute("ALTER TABLE audit_logs DISABLE TRIGGER audit_logs_no_update")
        app_conn.rollback()

        with app_conn.cursor() as cur:
            with pytest.raises(psycopg2.errors.InsufficientPrivilege, match="owner"):
                cur.execute("ALTER TABLE audit_logs DISABLE TRIGGER audit_logs_no_delete")
        app_conn.rollback()


def test_admin_role_can_disable_and_reenable_the_trigger():
    # The owner retains full control — confirms the split is real
    # (admin vs. runtime), not just "nobody can touch it."
    with _disposable_roles_and_schema() as (admin_conn, _app_conn):
        with admin_conn.cursor() as cur:
            cur.execute("ALTER TABLE audit_logs DISABLE TRIGGER audit_logs_no_update")
            cur.execute("ALTER TABLE audit_logs ENABLE TRIGGER audit_logs_no_update")


def test_admin_role_can_apply_and_reapply_schema_migrations():
    # "Migration owner can apply/reapply migrations": re-running the same
    # guarded DDL a second time, as the admin role, must not error —
    # apply.sh's own reapply-safety contract, now specifically proven for
    # the role that actually runs migrations post-028.
    with _disposable_roles_and_schema() as (admin_conn, _app_conn):
        with admin_conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS audit_logs ("
                "  id SERIAL PRIMARY KEY,"
                "  actor TEXT,"
                "  message TEXT,"
                "  created_at TIMESTAMPTZ NOT NULL DEFAULT now()"
                ")"
            )  # no-op: already exists
            cur.execute(
                "CREATE OR REPLACE FUNCTION audit_logs_reject_mutation() RETURNS TRIGGER AS $$ "
                "BEGIN RAISE EXCEPTION 'audit_logs is append-only: % is not permitted', TG_OP; "
                "END; $$ LANGUAGE plpgsql"
            )
            cur.execute("DROP TRIGGER IF EXISTS audit_logs_no_update ON audit_logs")
            cur.execute(
                "CREATE TRIGGER audit_logs_no_update BEFORE UPDATE ON audit_logs "
                "FOR EACH ROW EXECUTE FUNCTION audit_logs_reject_mutation()"
            )  # must not raise a permission error against its own prior objects


def test_runtime_role_connects_and_is_not_the_owner():
    # "Services still connect using the runtime role": a plain login/query
    # succeeds, and the role genuinely is not audit_logs's owner (the
    # precondition every other test in this file depends on).
    with _disposable_roles_and_schema() as (_admin_conn, app_conn):
        with app_conn.cursor() as cur:
            cur.execute("SELECT current_user")
            (whoami,) = cur.fetchone()
            cur.execute(
                "SELECT pg_get_userbyid(c.relowner) FROM pg_class c "
                "WHERE c.oid = 'audit_logs'::regclass"
            )
            (owner,) = cur.fetchone()

        assert whoami != owner


def test_app_credentials_cannot_authenticate_as_the_admin_role():
    # Round 2 review requirement: distinct passwords must be a REAL
    # authentication boundary, not just a naming convention — knowing the
    # runtime role's password must not let you connect AS the admin role.
    admin_role = f"test_admin_{uuid.uuid4().hex[:10]}"
    app_role = f"test_app_{uuid.uuid4().hex[:10]}"
    admin_password = uuid.uuid4().hex
    app_password = uuid.uuid4().hex
    assert admin_password != app_password  # the fixture's own guarantee, made explicit

    boot = _bare_connection()
    boot.autocommit = True
    try:
        with boot.cursor() as cur:
            cur.execute(f'CREATE ROLE "{admin_role}" WITH LOGIN PASSWORD %s SUPERUSER', (admin_password,))
            cur.execute(f'CREATE ROLE "{app_role}" WITH LOGIN PASSWORD %s NOSUPERUSER', (app_password,))

        with pytest.raises(psycopg2.OperationalError, match="password authentication failed"):
            _bare_connection(user=admin_role, password=app_password)

        # Sanity: the admin role's OWN password still works, proving the
        # rejection above was about the wrong password, not a broken role.
        real_admin_conn = _bare_connection(user=admin_role, password=admin_password)
        real_admin_conn.close()
    finally:
        with boot.cursor() as cur:
            cur.execute(f'DROP ROLE IF EXISTS "{app_role}"')
            cur.execute(f'DROP ROLE IF EXISTS "{admin_role}"')
        boot.close()


def _run_equal_password_guard(cur, admin_password, app_password):
    # Mirrors db/migrations/scripts/create_admin_role.sql's own guard
    # exactly: SELECT (:'admin_password' = :'app_password') AS
    # passwords_equal \gset, then \if :passwords_equal raises. Its
    # \getenv/\gset/\if plumbing is psql-client-only, not executable via
    # psycopg2, so this exercises the identical decision logic the file
    # implements — the two-value equality check and the conditional RAISE —
    # through psycopg2's own %s parameter binding instead. The literal file
    # was verified by hand against a real container, both fresh-volume and
    # existing-volume paths — see this PR's own commit message.
    cur.execute("SELECT (%s = %s) AS passwords_equal", (admin_password, app_password))
    (passwords_equal,) = cur.fetchone()
    cur.execute(
        "DO $do_check$ BEGIN IF %s THEN "
        "RAISE EXCEPTION 'DB_ADMIN_PASSWORD must be distinct from DB_PASSWORD'; "
        "END IF; END $do_check$;",
        (passwords_equal,),
    )


def test_create_admin_role_sql_rejects_equal_passwords():
    conn = _bare_connection()
    conn.autocommit = True
    same = uuid.uuid4().hex
    with conn.cursor() as cur:
        with pytest.raises(psycopg2.errors.RaiseException, match="distinct"):
            _run_equal_password_guard(cur, same, same)
    conn.close()


def test_create_admin_role_sql_proceeds_with_distinct_passwords():
    conn = _bare_connection()
    conn.autocommit = True
    with conn.cursor() as cur:
        _run_equal_password_guard(cur, uuid.uuid4().hex, uuid.uuid4().hex)  # must not raise
    conn.close()


def test_public_audit_logs_and_real_roles_are_untouched():
    # Never applied to the live shared database — the real riverbend_app
    # still owns the real audit_logs, and the real table still carries
    # whatever row count it already had.
    conn = _bare_connection()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_get_userbyid(c.relowner) FROM pg_class c "
            "WHERE c.oid = 'public.audit_logs'::regclass"
        )
        (owner,) = cur.fetchone()
        cur.execute("SELECT count(*) FROM pg_roles WHERE rolname = 'riverbend_admin'")
        (admin_role_count,) = cur.fetchone()
    conn.close()

    assert owner == os.getenv("DB_USER", "riverbend_app")
    assert admin_role_count == 0  # 028 has not been run against this database


# --- round-3 review: the ACTUAL schema.sql/028 files, non-default role names ---
#
# Everything above proves the PRIVILEGE MODEL against a hand-built table and
# disposable roles/schema on the shared local Postgres. It cannot prove the
# real db/schema.sql / db/docker-init/*.sh / db/migrations/028_*.sql files
# themselves are free of hard-coded role names, because those files (a)
# contain \getenv/\gset/\if — psql client-side meta-commands psycopg2 cannot
# execute — and (b) schema.sql always targets the `public` schema by name,
# with no schema-qualification parameter, so running the LITERAL file
# against the shared database's own `public` would grant a throwaway role
# broad access to every real table there — exactly what this whole session
# has been careful never to do.
#
# The only way to run the real files as psql itself would, using role names
# that are NOT the repo's own defaults (proving nothing is hard-coded), is a
# throwaway, fully isolated Postgres container — not a schema inside the
# shared one. These two tests do that: spin up a real, disposable
# `pgvector/pgvector` container per test (removed in `finally` regardless of
# outcome), mount this checkout's actual init/migration files into it
# exactly as docker-compose.yml does, and drive it with `docker exec` (never
# touching the shared local Postgres this module's other tests use).
#
# Skipped automatically if the `docker` CLI is not on PATH.
import shutil
import subprocess
import time

_DOCKER = shutil.which("docker")
_REPO_ROOT_FOR_MOUNTS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _docker(*args, env=None, check=True, input_bytes=None):
    return subprocess.run(
        ["docker", *args], env=env, check=check, capture_output=True,
        input=input_bytes, timeout=30,
    )


def _wait_for_postgres(container, user, password, timeout=20):
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        result = _docker(
            "exec", "-e", f"PGPASSWORD={password}", container,
            "psql", "-U", user, "-d", "riverbend", "-c", "SELECT 1",
            check=False,
        )
        if result.returncode == 0:
            return
        last_err = result.stderr
        time.sleep(0.5)
    raise TimeoutError(f"postgres in {container} never became ready: {last_err}")


def _psql_exec(container, user, password, sql_path_in_container, extra_env=None):
    """Runs a file INSIDE the container via `docker exec ... psql -f`, so
    \\getenv reads from the CONTAINER's own environment — never a
    command-line argument — exactly as db/migrations/apply.sh and
    db/migrations/scripts/bootstrap_admin_role.sh do it for real."""
    env_args = []
    for k, v in (extra_env or {}).items():
        env_args += ["-e", f"{k}={v}"]
    return _docker(
        "exec", *env_args, "-e", f"PGPASSWORD={password}", container,
        "psql", "-v", "ON_ERROR_STOP=1", "-U", user, "-d", "riverbend",
        "-f", sql_path_in_container,
        check=False,
    )


@contextlib.contextmanager
def _disposable_container(name, postgres_user, postgres_password, extra_env, mounts):
    args = [
        "run", "-d", "--name", name,
        "-e", "POSTGRES_DB=riverbend",
        "-e", f"POSTGRES_USER={postgres_user}",
        "-e", f"POSTGRES_PASSWORD={postgres_password}",
    ]
    for k, v in extra_env.items():
        args += ["-e", f"{k}={v}"]
    for host_path, container_path in mounts.items():
        args += ["-v", f"{host_path}:{container_path}:ro"]
    args.append("pgvector/pgvector:0.8.0-pg15")
    try:
        _docker(*args)
        _wait_for_postgres(name, postgres_user, postgres_password)
        yield
    finally:
        _docker("rm", "-f", name, check=False)


@pytest.mark.skipif(_DOCKER is None, reason="docker CLI not available")
def test_the_real_fresh_install_files_grant_a_non_default_runtime_role_correctly():
    # Proves db/docker-init/00-create-app-role.sh + db/docker-init/
    # 01-run-schema.sh + db/schema.sql — mounted exactly as docker-compose.yml
    # mounts them — contain NO hard-coded role name, by using role names that
    # are NOT this repo's own defaults (riverbend_admin/riverbend_app) at all.
    name = f"role_sep_fresh_test_{uuid.uuid4().hex[:8]}"
    admin_role, admin_password = f"t_admin_{uuid.uuid4().hex[:6]}", uuid.uuid4().hex
    app_role, app_password = f"t_app_{uuid.uuid4().hex[:6]}", uuid.uuid4().hex

    mounts = {
        os.path.join(_REPO_ROOT_FOR_MOUNTS, "db", "docker-init", "00-create-app-role.sh"):
            "/docker-entrypoint-initdb.d/00-create-app-role.sh",
        os.path.join(_REPO_ROOT_FOR_MOUNTS, "db", "docker-init", "01-run-schema.sh"):
            "/docker-entrypoint-initdb.d/01-run-schema.sh",
        os.path.join(_REPO_ROOT_FOR_MOUNTS, "db", "schema.sql"):
            "/docker-entrypoint-initdb.d-src/schema.sql",
    }
    extra_env = {"DB_APP_USER": app_role, "DB_APP_PASSWORD": app_password}

    with _disposable_container(name, admin_role, admin_password, extra_env, mounts):
        owner = _docker(
            "exec", "-e", f"PGPASSWORD={admin_password}", name,
            "psql", "-U", admin_role, "-d", "riverbend", "-tA",
            "-c", "SELECT tableowner FROM pg_tables WHERE tablename='audit_logs'",
        ).stdout.decode().strip()
        assert owner == admin_role

        attrs = _docker(
            "exec", "-e", f"PGPASSWORD={admin_password}", name,
            "psql", "-U", admin_role, "-d", "riverbend", "-tA",
            "-c", f"SELECT rolsuper OR rolcreatedb OR rolcreaterole FROM pg_roles WHERE rolname='{app_role}'",
        ).stdout.decode().strip()
        assert attrs == "f"  # non-superuser, non-createdb, non-createrole

        insert = _docker(
            "exec", "-e", f"PGPASSWORD={app_password}", name,
            "psql", "-U", app_role, "-d", "riverbend",
            "-c", "INSERT INTO audit_logs (actor, message) VALUES ('t','ok')",
            check=False,
        )
        assert insert.returncode == 0, insert.stderr.decode()

        update = _docker(
            "exec", "-e", f"PGPASSWORD={app_password}", name,
            "psql", "-U", app_role, "-d", "riverbend",
            "-c", "UPDATE audit_logs SET message='tampered'",
            check=False,
        )
        assert update.returncode != 0
        assert b"permission denied" in update.stderr

        disable_trigger = _docker(
            "exec", "-e", f"PGPASSWORD={app_password}", name,
            "psql", "-U", app_role, "-d", "riverbend",
            "-c", "ALTER TABLE audit_logs DISABLE TRIGGER audit_logs_no_update",
            check=False,
        )
        assert disable_trigger.returncode != 0
        assert b"must be owner" in disable_trigger.stderr


@pytest.mark.skipif(_DOCKER is None, reason="docker CLI not available")
def test_the_real_028_migration_transitions_a_non_default_legacy_role_correctly():
    # Proves db/migrations/scripts/create_admin_role.sql + db/migrations/
    # 028_admin_runtime_role_separation.sql contain NO hard-coded role name,
    # by transitioning a LEGACY single-role volume whose original bootstrap
    # role is named something this repo's own scripts never default to.
    name = f"role_sep_legacy_test_{uuid.uuid4().hex[:8]}"
    legacy_role, legacy_password = f"t_legacy_{uuid.uuid4().hex[:6]}", uuid.uuid4().hex
    admin_role, admin_password = f"t_admin2_{uuid.uuid4().hex[:6]}", uuid.uuid4().hex

    old_schema_sql = os.path.join(_REPO_ROOT_FOR_MOUNTS, "db", "schema.sql")
    # This branch's own schema.sql already has the parameterized tail block
    # (\getenv), which the PRE-split-84 shape never had — for a legacy-volume
    # simulation we need the shape 026/027/028 actually run against: a bare
    # audit_logs with no chain columns and no grant tail. Build it minimally
    # rather than depending on git history from inside a test.
    minimal_legacy_schema = (
        "CREATE TABLE audit_logs (id SERIAL PRIMARY KEY, actor TEXT, message TEXT, "
        "created_at TIMESTAMPTZ NOT NULL DEFAULT now(), deleted_at TIMESTAMPTZ);\n"
        "CREATE TABLE patients (id SERIAL PRIMARY KEY, name TEXT);\n"
    )
    tmp_schema_path = f"/tmp/{name}_legacy_schema.sql"
    with open(tmp_schema_path, "w", encoding="utf-8") as f:
        f.write(minimal_legacy_schema)

    mounts = {tmp_schema_path: "/docker-entrypoint-initdb.d/01-schema.sql"}
    try:
        with _disposable_container(name, legacy_role, legacy_password, {}, mounts):
            # Sanity: legacy_role really is the bootstrap superuser here,
            # matching this repo's real not-yet-migrated state.
            attrs = _docker(
                "exec", "-e", f"PGPASSWORD={legacy_password}", name,
                "psql", "-U", legacy_role, "-d", "riverbend", "-tA",
                "-c", f"SELECT rolsuper FROM pg_roles WHERE rolname='{legacy_role}'",
            ).stdout.decode().strip()
            assert attrs == "t"

            # 026's own trigger — needed for the DISABLE-TRIGGER assertion
            # below; not this test's subject, so applied directly rather
            # than mounting the real file.
            create_trigger = (
                "CREATE OR REPLACE FUNCTION audit_logs_reject_mutation() RETURNS TRIGGER AS $$ "
                "BEGIN RAISE EXCEPTION 'audit_logs is append-only: % is not permitted', TG_OP; "
                "END; $$ LANGUAGE plpgsql;\n"
                "CREATE TRIGGER audit_logs_no_update BEFORE UPDATE ON audit_logs "
                "FOR EACH ROW EXECUTE FUNCTION audit_logs_reject_mutation();\n"
            )
            _docker(
                "exec", "-i", "-e", f"PGPASSWORD={legacy_password}", name,
                "psql", "-v", "ON_ERROR_STOP=1", "-U", legacy_role, "-d", "riverbend",
                input_bytes=create_trigger.encode(),
            )

            create_admin_sql = os.path.join(
                _REPO_ROOT_FOR_MOUNTS, "db", "migrations", "scripts", "create_admin_role.sql"
            )
            _docker("cp", create_admin_sql, f"{name}:/tmp/create_admin_role.sql")
            result = _psql_exec(
                name, legacy_role, legacy_password, "/tmp/create_admin_role.sql",
                extra_env={
                    "DB_ADMIN_USER": admin_role, "DB_ADMIN_PASSWORD": admin_password,
                    "DB_PASSWORD": legacy_password,
                },
            )
            assert result.returncode == 0, result.stderr.decode()

            migration_028 = os.path.join(
                _REPO_ROOT_FOR_MOUNTS, "db", "migrations", "028_admin_runtime_role_separation.sql"
            )
            _docker("cp", migration_028, f"{name}:/tmp/028.sql")
            result = _psql_exec(
                name, admin_role, admin_password, "/tmp/028.sql",
                extra_env={"DB_ADMIN_USER": admin_role, "DB_APP_USER": legacy_role},
            )
            assert result.returncode == 0, result.stderr.decode()

            owner = _docker(
                "exec", "-e", f"PGPASSWORD={admin_password}", name,
                "psql", "-U", admin_role, "-d", "riverbend", "-tA",
                "-c", "SELECT tableowner FROM pg_tables WHERE tablename='audit_logs'",
            ).stdout.decode().strip()
            assert owner == admin_role

            attrs = _docker(
                "exec", "-e", f"PGPASSWORD={admin_password}", name,
                "psql", "-U", admin_role, "-d", "riverbend", "-tA",
                "-c", f"SELECT rolsuper FROM pg_roles WHERE rolname='{legacy_role}'",
            ).stdout.decode().strip()
            assert attrs == "f"  # demoted off superuser

            insert = _docker(
                "exec", "-e", f"PGPASSWORD={legacy_password}", name,
                "psql", "-U", legacy_role, "-d", "riverbend",
                "-c", "INSERT INTO audit_logs (actor, message) VALUES ('t','ok')",
                check=False,
            )
            assert insert.returncode == 0, insert.stderr.decode()

            disable_trigger = _docker(
                "exec", "-e", f"PGPASSWORD={legacy_password}", name,
                "psql", "-U", legacy_role, "-d", "riverbend",
                "-c", "ALTER TABLE audit_logs DISABLE TRIGGER audit_logs_no_update",
                check=False,
            )
            assert disable_trigger.returncode != 0
            assert b"must be owner" in disable_trigger.stderr
    finally:
        os.remove(tmp_schema_path)
