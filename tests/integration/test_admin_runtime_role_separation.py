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
