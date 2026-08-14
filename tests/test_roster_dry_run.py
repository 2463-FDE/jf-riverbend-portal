"""Roster dry-run mapping — the report the client signs before migrating.

The property that matters most: **nothing is dropped.** Every account and
every active roster row must land in exactly one bucket, because a migration
that silently skips an account it couldn't parse is the failure this report
exists to prevent. That invariant is asserted directly, not implied.
"""
import os

from conftest import load_module

dry_run = load_module("db/migrations/scripts/roster_dry_run.py", "roster_dry_run")

Account = dry_run.Account
RosterRow = dry_run.RosterRow

ROSTER_CSV = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "db", "seed", "staff_roster_SYNTHETIC.csv",
)


def _outcomes(findings, outcome):
    return [f for f in findings if f.outcome == outcome]


def _subject_outcome(findings, subject):
    return next((f.outcome for f in findings if f.subject == subject), None)


# --- name normalisation: what makes matching possible at all ---------------


def test_normalises_the_decoration_that_full_name_carries():
    # users.full_name has suffixes the roster does not. Without this, staff who
    # are plainly the same person would fail to match and land in the
    # decide-manually pile for no reason.
    assert dry_run.normalise_name("Maya Okonkwo (COO)") == "maya okonkwo"
    assert dry_run.normalise_name("Karen Cole, RN") == "karen cole"
    assert dry_run.normalise_name("Dr. Anil Patel") == "anil patel"
    assert dry_run.normalise_name("  Rosa   Delgado  ") == "rosa delgado"


def test_normalisation_does_not_collapse_different_people():
    assert dry_run.normalise_name("Anita Nguyen") != dry_run.normalise_name("Anil Patel")


def test_unknown_function_maps_to_no_role():
    # A function nobody has mapped must not inherit a role by accident.
    assert dry_run.role_for_function("Volunteer Coordinator") is None
    assert dry_run.role_for_function("") is None
    assert dry_run.role_for_function("Physician") == "clinician"


# --- the synthetic roster parses and covers its documented cases -----------


def test_reads_the_roster_skipping_comment_lines():
    roster = dry_run.read_roster(ROSTER_CSV)

    assert len(roster) == 17
    assert all(r.name and r.function and r.status for r in roster)
    assert not any(r.name.startswith("#") for r in roster)


def test_roster_carries_only_the_five_columns_the_client_specified():
    row = dry_run.read_roster(ROSTER_CSV)[0]

    assert set(vars(row)) == {"name", "function", "department", "clinic", "status"}


# --- the buckets, against the real seeded account set ----------------------

SEEDED = [
    Account("mokonkwo", "Maya Okonkwo (COO)", "staff", True),
    Account("frontdesk", "Front Desk (Riverbend Main)", "staff", True),
    Account("rdelgado", "Rosa Delgado (Registration)", "staff", True),
    Account("jpark", "Jin Park (Registration)", "staff", True),
    Account("drpatel", "Dr. Anil Patel", "staff", True),
    Account("drnguyen", "Dr. Anita Nguyen", "staff", True),
    Account("drlee", "Dr. Sandra Lee", "staff", True),
    Account("billing1", "Tom Reyes (Billing)", "staff", True),
    Account("roiclerk", "Dana White (ROI Clerk)", "staff", True),
    Account("labtech", "Lab Intake", "staff", True),
    Account("nurse_kc", "Karen Cole, RN", "staff", True),
    Account("itadmin", "Helix Support", "staff", True),
]


def _report():
    return dry_run.build_report(dry_run.read_roster(ROSTER_CSV), SEEDED)


def test_nothing_is_dropped():
    # The invariant. Every account appears exactly once, and every active
    # roster person is either matched to an account or reported as needing one.
    findings = _report()

    for acct in SEEDED:
        appearances = [f for f in findings if f.subject == acct.username]
        assert len(appearances) == 1, f"{acct.username} appeared {len(appearances)} times"


def test_clean_one_to_one_accounts_are_proposed_for_their_role():
    findings = _report()
    proposed = {f.subject: f.proposed_role for f in _outcomes(findings, dry_run.MIGRATE)}

    assert proposed["mokonkwo"] == "management"
    assert proposed["rdelgado"] == "front_desk"
    assert proposed["jpark"] == "front_desk"
    assert proposed["drpatel"] == "clinician"
    assert proposed["drnguyen"] == "clinician"
    assert proposed["nurse_kc"] == "nursing_ma"
    assert proposed["billing1"] == "billing"
    assert proposed["roiclerk"] == "roi_clerk"


def test_an_account_nobody_owns_is_never_migrated():
    # "Front Desk (Riverbend Main)", "Lab Intake" and "Helix Support" are not
    # people. Whether each is a shared desk login or a departed contractor's
    # account is not decidable from the data, so all three go to a human.
    findings = _report()

    for username in ("frontdesk", "labtech", "itadmin"):
        assert _subject_outcome(findings, username) == dry_run.DECIDE_NO_OWNER


def test_the_no_owner_finding_offers_context_without_guessing_a_role():
    findings = _report()
    frontdesk = next(f for f in findings if f.subject == "frontdesk")

    assert frontdesk.proposed_role is None  # never guesses
    assert "cannot tell" in frontdesk.detail

    # Context is a fact, not a guess about which desk this account belongs to:
    # exactly the active staff who have no account of their own, since those
    # are the only people a shared login could be split into.
    assert any("Priya Raman" in c for c in frontdesk.context)
    assert any("Owen Fitzgerald" in c for c in frontdesk.context)
    # Staff who already hold their own account are not offered as candidates.
    assert not any("Rosa Delgado" in c for c in frontdesk.context)
    assert not any("Maya Okonkwo" in c for c in frontdesk.context)


def test_a_terminated_persons_account_is_disabled_not_migrated():
    findings = _report()

    assert _subject_outcome(findings, "drlee") == dry_run.DISABLE_DEPARTED
    assert next(f for f in findings if f.subject == "drlee").proposed_role is None


def test_active_staff_without_an_account_are_reported():
    findings = _report()
    needs = {f.subject: f.proposed_role for f in _outcomes(findings, dry_run.NEEDS_ACCOUNT)}

    # No scheduler or it_admin account exists anywhere today.
    assert needs["Nadia Osei"] == "scheduler"
    assert needs["Owen Fitzgerald"] == "it_admin"
    # People behind the shared logins also surface here — they need named accounts.
    assert "Priya Raman" in needs and needs["Priya Raman"] == "front_desk"
    assert "Aisha Kone" in needs and needs["Aisha Kone"] == "lab"


def test_an_unmappable_function_denies_by_default_and_proposes_nothing():
    findings = _report()
    grace = next(f for f in findings if f.subject == "Grace Liang")

    assert grace.outcome == dry_run.UNMAPPED_FUNCTION
    assert grace.proposed_role is None


def test_staff_on_leave_are_surfaced_as_an_open_question_not_defaulted():
    # The client set rules for active and terminated staff and not for leave.
    # Yusuf Demir has no account, so he lands in the roster-side pass; the
    # point is that no rule silently decides for him.
    findings = _report()

    assert not any(
        f.subject == "Yusuf Demir" and f.outcome == dry_run.MIGRATE for f in findings
    )


def test_a_terminated_person_with_no_account_is_not_reported_as_needing_one():
    findings = _report()

    assert not any(f.subject == "Sandra Lee" and f.outcome == dry_run.NEEDS_ACCOUNT for f in findings)


# --- the report itself -----------------------------------------------------


def test_report_states_plainly_that_nothing_is_applied():
    text = dry_run.format_report(_report())

    assert "Nothing below is applied" in text
    assert "before any migration runs" in text


def test_report_counts_what_still_needs_a_decision():
    text = dry_run.format_report(_report())

    assert "need a decision" in text


def test_report_lists_every_bucket_even_when_empty():
    # An empty bucket must still be visible, so a reader can see it was
    # considered rather than wondering whether it was checked.
    text = dry_run.format_report(dry_run.build_report([], []))

    for title in ("MIGRATE", "DECIDE", "DISABLE", "DENY BY DEFAULT", "OPEN QUESTION", "NEEDS AN ACCOUNT"):
        assert title in text
