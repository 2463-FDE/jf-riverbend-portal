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


def test_every_deny_outcome_deactivates_with_a_reason():
    findings = [
        _f(dry_run.DECIDE_NO_OWNER, "frontdesk"),
        _f(dry_run.UNMAPPED_FUNCTION, "someone"),
        _f(dry_run.UNKNOWN_STATUS, "typo_status"),
        _f(dry_run.DISABLE_DEPARTED, "departed"),
    ]

    m, d, u = migrate.plan(findings)

    assert not m and not u
    reasons = {f.subject: reason for f, reason in d}
    assert reasons["frontdesk"] == "role_migration_no_owner"
    # The client's copy is shown for any role_migration_* reason, so every one
    # of these prefixes must match what services/gateway/app.py checks.
    assert all(r.startswith("role_migration_") for r in reasons.values())


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
    assert "DEACTIVATE — cannot authenticate  [3]" in out
    # The three that are not people.
    for username in ("frontdesk", "labtech", "itadmin"):
        assert username in out
    # And the client's copy is quoted so an operator sees what the user will.
    assert "access is being updated, contact your supervisor" in out
