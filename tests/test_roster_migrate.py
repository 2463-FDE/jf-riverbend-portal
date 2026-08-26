"""Applying the signed roster report — branch 9 part 2.

The properties that matter, in order:

1. **It applies the report the client signed, not a second opinion.** The script
   imports `build_report` from `roster_dry_run` rather than re-deriving outcomes,
   so the two cannot drift. A migration that assigned a role the report did not
   propose would be a change nobody approved.
2. **Nothing happens without `--apply` AND `--approved-by`.** An unattributed
   migration of every staff account is not something this script should be able
   to perform.
3. **An outcome it does not recognise is left alone and reported**, never
   defaulted. Guessing at an unfamiliar outcome is how an account ends up in a
   state nobody signed off on.
"""
import os
import sys

import pytest

from conftest import load_module

SCRIPTS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "db", "migrations", "scripts"
)
sys.path.insert(0, SCRIPTS)

dry_run = load_module("db/migrations/scripts/roster_dry_run.py", "roster_dry_run_for_migrate")
migrate = load_module("db/migrations/scripts/roster_migrate.py", "roster_migrate")

Finding = dry_run.Finding


def _f(outcome, subject, role=None):
    return Finding(outcome=outcome, subject=subject, detail="d", proposed_role=role)


def test_migrate_outcomes_become_role_assignments():
    m, d, u = migrate.plan([_f(dry_run.MIGRATE, "drpatel", "clinician")])

    assert [(f.subject, f.proposed_role) for f in m] == [("drpatel", "clinician")]
    assert not d and not u


def test_every_unambiguous_deny_outcome_deactivates_with_a_reason():
    findings = [
        _f(dry_run.UNMAPPED_FUNCTION, "someone"),
        _f(dry_run.UNKNOWN_STATUS, "typo_status"),
        _f(dry_run.DISABLE_DEPARTED, "departed"),
    ]

    m, d, u = migrate.plan(findings)

    assert not m and not u
    reasons = {f.subject: reason for f, reason in d}
    # The client's copy is shown for any role_migration_* reason, so every one
    # of these prefixes must match what services/gateway/app.py checks.
    assert all(r.startswith("role_migration_") for r in reasons.values())


def test_no_owner_is_left_alone_not_auto_deactivated():
    # frontdesk/labtech are shared logins actively used by real staff; itadmin
    # is a departed contractor's orphaned account. Both look identical in the
    # data ("a name that isn't a person's"), so this outcome must never be
    # auto-deactivated — deactivating a shared login the moment --apply runs
    # would lock out real, working staff with no way to tell them apart from
    # the genuinely-departed case. A human resolves each individually.
    findings = [
        _f(dry_run.DECIDE_NO_OWNER, "frontdesk"),
        _f(dry_run.DECIDE_NO_OWNER, "labtech"),
        _f(dry_run.DECIDE_NO_OWNER, "itadmin"),
    ]

    m, d, u = migrate.plan(findings)

    assert not m and not d
    assert {f.subject for f in u} == {"frontdesk", "labtech", "itadmin"}


def test_people_with_no_account_are_not_account_actions():
    # NEEDS_ACCOUNT and DEPARTED_CHECKED are roster-side findings. There is no
    # account to migrate or disable, and inventing one is not this script's job.
    m, d, u = migrate.plan([
        _f(dry_run.NEEDS_ACCOUNT, "Marisol Vega", "scheduler"),
        _f(dry_run.DEPARTED_CHECKED, "Marcus Hale MD"),
    ])

    assert not m and not d and not u


def test_on_leave_is_left_alone_and_reported():
    # The client set no rule for leave. Deactivating would lock out somebody
    # returning from leave; migrating would grant access nobody approved.
    m, d, u = migrate.plan([_f(dry_run.HOLD_ON_LEAVE, "onleave")])

    assert not m and not d
    assert [f.subject for f in u] == ["onleave"]


def test_an_unrecognised_outcome_is_left_alone_not_defaulted():
    """The forward-compatibility property. A new outcome added to the report
    must not be silently treated as safe by this script."""
    m, d, u = migrate.plan([_f("some_outcome_added_later", "mystery")])

    assert not m and not d
    assert [f.subject for f in u] == ["mystery"]


def test_an_unreadable_roles_config_aborts_before_any_plan_or_write(monkeypatch):
    # P1 review (w8-planner-2): defined_roles() previously swallowed a missing
    # PyYAML (or any unreadable roles.yaml) into an empty role set, which made
    # every mapped function read as UNMAPPED_FUNCTION -- and that outcome IS
    # auto-deactivated by design, so a missing dependency silently turned
    # "migrate ten staff" into "deactivate ten staff who cannot authenticate,"
    # with the same command and flags. Simulates that failure at the point
    # main() calls into it and proves the run aborts before building a plan,
    # let alone writing one.
    def _unreadable(roster, accounts, known_roles=None):
        # migrate.RolesConfigUnreadable, not dry_run.RolesConfigUnreadable:
        # this test file loads roster_dry_run.py under a different module
        # name than roster_migrate.py's own internal import of it, so the
        # two are distinct class objects despite being the same source —
        # main()'s except clause only matches its own import's class.
        raise migrate.RolesConfigUnreadable("simulated: PyYAML unavailable")

    monkeypatch.setattr(migrate, "build_report", _unreadable)

    def _must_not_be_called(*a, **k):
        raise AssertionError("apply_plan must never run when the roles config could not be read")

    monkeypatch.setattr(migrate, "apply_plan", _must_not_be_called)

    rc = migrate.main(["roster_migrate.py", "--apply", "--approved-by", "Jorge"])

    assert rc == 2


def test_apply_requires_an_approver():
    rc = migrate.main(["roster_migrate.py", "--apply"])

    assert rc == 2, "--apply without --approved-by must refuse"


def test_a_client_role_disagreement_blocks_the_whole_run(monkeypatch):
    """The report is signed with no disagreements outstanding. If one appears,
    the signature covered something other than what would be applied — so this
    aborts before any write rather than applying the majority of it."""
    monkeypatch.setattr(
        migrate, "cross_check_client_roles",
        lambda findings, roster: findings + [_f(dry_run.ROLE_DISAGREEMENT, "drpatel")],
    )

    rc = migrate.main(["roster_migrate.py", "--apply", "--approved-by", "Jorge"])

    assert rc == 3, "a disagreement must block the run"


def test_the_dry_run_is_the_default_and_writes_nothing(capsys):
    rc = migrate.main(["roster_migrate.py"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "dry run — nothing changed" in out
    assert "--apply" in out, "the dry run must say how to apply it"


def test_the_dry_run_reports_the_real_roster_plan(capsys):
    """End-to-end against the committed roster and seed: the plan the client
    receives. Pinned because these counts are what they sign."""
    migrate.main(["roster_migrate.py"])
    out = capsys.readouterr().out

    assert "MIGRATE — role assigned  [10]" in out
    assert "DEACTIVATE — cannot authenticate  [0]" in out
    assert "LEFT ALONE — needs a human decision  [3]" in out
    # The three that are not people — left alone, not auto-deactivated.
    for username in ("frontdesk", "labtech", "itadmin"):
        assert username in out
    # And the client's copy is quoted so an operator sees what the user will
    # see for whichever accounts a human later decides to deactivate.
    assert "access is being updated, contact your supervisor" in out


# --- apply_plan: the path review round 1 found untested ---------------------
#
# R1-MAJOR-001 and -002. The previous version issued `UPDATE ... RETURNING role`
# and never fetched the result, so a zero-row match committed silently AND still
# wrote a log row claiming the account had been migrated. It also logged
# from_role=None always. Both are exercised here with a fake cursor rather than a
# live database, so they run in CI without infrastructure.


class _FakeCursor:
    """Records statements and answers the locked read from a known account set."""

    def __init__(self, existing):
        self.existing = dict(existing)   # username -> current role
        self.statements = []
        self.logs = []
        self._last = None
        self.rowcount = 0

    def execute(self, sql, params=()):
        self.statements.append((sql, params))
        if sql.startswith("SELECT role FROM users"):
            username = params[0]
            self._last = (self.existing[username],) if username in self.existing else None
            self.rowcount = 1 if self._last else 0
        elif sql.startswith("UPDATE users"):
            username = params[-1]
            self.rowcount = 1 if username in self.existing else 0
        elif sql.startswith("INSERT INTO role_migration_log"):
            self.logs.append(params)
            self.rowcount = 1

    def fetchone(self):
        return self._last


class _FakeConn:
    def __init__(self, existing):
        self.cur = _FakeCursor(existing)
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return self.cur

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


def test_apply_records_the_role_it_migrated_FROM():
    """R1-MAJOR-002. `drkim` starts on `clinician` and the rest on the
    deprecated flat `staff`; the log has to preserve that distinction or it
    cannot answer "which accounts changed" at all."""
    conn = _FakeConn({"drkim": "clinician", "drpatel": "staff"})
    migrations = [
        _f(dry_run.MIGRATE, "drkim", "clinician"),
        _f(dry_run.MIGRATE, "drpatel", "clinician"),
    ]

    migrate.apply_plan(conn, migrations, [], "Jorge")

    logged = {row[0]: (row[1], row[2]) for row in conn.cur.logs}
    assert logged["drkim"] == ("clinician", "clinician")
    assert logged["drpatel"] == ("staff", "clinician")
    assert conn.committed


def test_a_missing_account_aborts_everything_and_logs_nothing():
    """R1-MAJOR-001. The plan comes from the committed seed and is written to
    whatever DATABASE_URL points at; those can diverge. A log row claiming a
    migration that did not happen is worse than no migration."""
    conn = _FakeConn({"drpatel": "staff"})          # drkim absent
    migrations = [
        _f(dry_run.MIGRATE, "drpatel", "clinician"),
        _f(dry_run.MIGRATE, "drkim", "clinician"),
    ]

    with pytest.raises(migrate.AccountMissing, match="drkim"):
        migrate.apply_plan(conn, migrations, [], "Jorge")

    assert conn.rolled_back and not conn.committed


def test_deactivations_also_record_the_prior_role():
    conn = _FakeConn({"frontdesk": "staff"})

    migrate.apply_plan(conn, [], [(_f(dry_run.DECIDE_NO_OWNER, "frontdesk"),
                                   "role_migration_no_owner")], "Jorge")

    row = conn.cur.logs[0]
    assert row[0] == "frontdesk"
    assert row[1] == "staff"      # from_role preserved
    assert row[2] is None         # to_role: nothing was assigned


def test_the_locked_read_precedes_every_write():
    """The lock has to be taken before the UPDATE or two concurrent runs can
    interleave on the same account."""
    conn = _FakeConn({"drpatel": "staff"})

    migrate.apply_plan(conn, [_f(dry_run.MIGRATE, "drpatel", "clinician")], [], "Jorge")

    kinds = [s.split()[0] + (" FOR UPDATE" if "FOR UPDATE" in s else "")
             for s, _ in conn.cur.statements]
    assert kinds[0] == "SELECT FOR UPDATE"
    assert kinds[1] == "UPDATE"
    assert kinds[2] == "INSERT"
