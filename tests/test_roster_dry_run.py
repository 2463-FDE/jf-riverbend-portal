"""Roster dry-run mapping — the report the client signs before migrating.

The property that matters most: **nothing is dropped.** Every account and
every active roster row must land in exactly one bucket, because a migration
that silently skips an account it couldn't parse is the failure this report
exists to prevent. That invariant is asserted directly, not implied.
"""
import os

import pytest

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

    # 20 rows: the CLIENT's roster (received 2026-08-19), which replaced the
    # one we invented. 18 current staff plus 2 departures they asked us to
    # check for live accounts. The shared logins and the orphaned itadmin are
    # deliberately absent — they are account facts, not people.
    assert len(roster) == 20
    assert all(r.name and r.function and r.status for r in roster)
    assert not any(r.name.startswith("#") for r in roster)


def test_roster_carries_the_client_columns_plus_the_cross_check():
    row = dry_run.read_roster(ROSTER_CSV)[0]

    # The client's five, plus proposed_role (their proposal, used only to
    # cross-check ours) and two fields derived at read time from their dated
    # status vocabulary. Pinned so a stray column cannot appear unnoticed.
    assert set(vars(row)) == {
        "name", "function", "department", "clinic", "status",
        "raw_status", "expires_on", "client_proposed_role",
    }


def test_the_clients_dated_statuses_are_normalised_but_never_guessed():
    # Their roster writes departed_2026-05 and temp_ends_2026-09-30; this
    # mapper's decision logic is built on three plain statuses. Normalising at
    # read time keeps the branches unchanged — but an unrecognised value must
    # pass through untouched so it still lands in UNKNOWN_STATUS. Coercing an
    # unparseable status to "active" would migrate somebody on a typo.
    assert dry_run.normalise_status("departed_2026-05") == ("terminated", None)
    assert dry_run.normalise_status("temp_ends_2026-09-30") == ("active", "2026-09-30")
    assert dry_run.normalise_status("secondment?") == ("secondment?", None)
    assert dry_run.normalise_status("") == ("", None)


def test_bare_credentials_are_stripped_so_clinicians_match_their_accounts():
    # The bug this fixes was the whole blocker: the client writes "Anil Patel
    # MD" while users.full_name holds "Dr. Anil Patel". Without stripping a
    # comma-less credential, five of six clinical accounts matched nobody and
    # the report recommended disabling four working clinicians.
    for account_name, roster_name in [
        ("Dr. Grace Kim", "Grace Kim MD"),
        ("Dr. Sandra Lee", "Sandra Lee MD"),
        ("Dr. Anil Patel", "Anil Patel MD"),
        ("Karen Cole, RN", "Karen Cole RN"),
    ]:
        assert dry_run.normalise_name(account_name) == dry_run.normalise_name(roster_name)


def test_stripping_credentials_does_not_eat_a_real_surname():
    # "Bright" and "Reyes" are not credentials. An unanchored "drop the last
    # word" rule would have merged unrelated people.
    assert dry_run.normalise_name("Thomas Bright") == "thomas bright"
    assert dry_run.normalise_name("Tom Reyes") == "tom reyes"
    assert dry_run.normalise_name("Renee Alvarez") == "renee alvarez"


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
    assert any("Daniel Okafor" in c for c in frontdesk.context)
    # Staff who already hold their own account are not offered as candidates.
    assert not any("Rosa Delgado" in c for c in frontdesk.context)
    assert not any("Maya Okonkwo" in c for c in frontdesk.context)


# The next three cases are driven by FIXTURES, not by the shipped roster.
#
# They used to read the real file, which worked only because the roster we
# invented was built to contain every case. The client's roster is realistic
# instead of comprehensive: nobody on it is on leave, and every function maps
# to a role. Coupling this logic to the data meant a roster update broke tests
# that were never about the data — so the rules are tested directly and the
# file is tested separately, above.


def test_a_terminated_persons_account_is_disabled_not_migrated():
    roster = [RosterRow("Ada Byron", "Physician", "Clinical", "Riverbend Main", "terminated")]
    accounts = [Account("abyron", "Dr. Ada Byron", "staff", True)]

    findings = dry_run.build_report(roster, accounts, known_roles={"clinician"})

    assert _subject_outcome(findings, "abyron") == dry_run.DISABLE_DEPARTED
    assert next(f for f in findings if f.subject == "abyron").proposed_role is None


def test_a_departure_the_client_asked_about_is_reported_not_dropped():
    # The client listed three departures and said why: "I do not know whether
    # these still have live accounts... I would rather they surface in your dry
    # run than in an audit." Until 2026-08-20 a departed person with no account
    # was skipped entirely, so the report answered nothing about them.
    roster = [RosterRow("Ada Byron", "Physician", "Clinical", "", "terminated",
                        raw_status="departed_2026-05")]

    findings = dry_run.build_report(roster, [Account("someone_else", "Other Person", "staff", True)],
                                    known_roles={"clinician"})
    ada = [f for f in findings if f.subject == "Ada Byron"]

    assert len(ada) == 1, "a departure must not vanish from the report"
    assert ada[0].outcome == dry_run.DEPARTED_CHECKED
    assert ada[0].proposed_role is None
    # The report quotes what the roster actually said, not the normalised form:
    # "departed_2026-05" tells an operator more than "terminated".
    assert "departed_2026-05" in ada[0].detail
    # And it admits the limit of a name-based check rather than implying
    # certainty the data cannot support.
    assert "username variant" in ada[0].detail
    assert not _outcomes(findings, dry_run.NEEDS_ACCOUNT)


def test_active_staff_without_an_account_are_reported():
    findings = _report()
    needs = {f.subject: f.proposed_role for f in _outcomes(findings, dry_run.NEEDS_ACCOUNT)}

    # No scheduler, it_admin or lab account exists anywhere today.
    assert needs["Marisol Vega"] == "scheduler"
    assert needs["Daniel Okafor"] == "it_admin"
    assert needs["Ben Osei"] == "lab"
    # People behind the shared logins surface here too — they need named ones.
    assert needs["Priya Raman"] == "lab"
    assert needs["Sofia Marin"] == "front_desk"


def test_a_temporary_placement_is_provisioned_with_an_expiry():
    # The client was explicit about this one: "Set an expiry on the account at
    # provisioning; do not rely on someone remembering." An agency placement
    # gets the same ROLE as a permanent registrar — weakening it would be a
    # policy invention — and the temporary part is carried by the expiry.
    sofia = next(f for f in _report() if f.subject == "Sofia Marin")

    assert sofia.outcome == dry_run.NEEDS_ACCOUNT
    assert sofia.proposed_role == "front_desk"
    assert "2026-09-30" in sofia.detail


def test_an_unmappable_function_denies_by_default_and_proposes_nothing():
    roster = [RosterRow("Ada Byron", "Volunteer Coordinator", "Outreach", "Riverbend Main", "active")]

    findings = dry_run.build_report(roster, [], known_roles={"clinician"})
    ada = next(f for f in findings if f.subject == "Ada Byron")

    assert ada.outcome == dry_run.UNMAPPED_FUNCTION
    assert ada.proposed_role is None


def test_staff_on_leave_are_surfaced_as_an_open_question_not_defaulted():
    # PR #32 review [medium] caught this: the previous version of this test only
    # asserted Yusuf Demir was not in MIGRATE, which passed happily while he
    # appeared in NO bucket at all — the report silently dropped him. Assert the
    # bucket he must be in, not merely one he must not be.
    roster = [RosterRow("Ada Byron", "Physician", "Clinical", "Riverbend Main", "leave")]

    findings = dry_run.build_report(roster, [], known_roles={"clinician"})
    ada = [f for f in findings if f.subject == "Ada Byron"]

    assert len(ada) == 1, "on-leave staff must not vanish from the report"
    assert ada[0].outcome == dry_run.HOLD_ON_LEAVE
    assert ada[0].proposed_role is None


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


# --- reading accounts without a live database ------------------------------
#
# This is a training simulation and the committed seed is the account set the
# exercise runs against, so the report must not require `make up`.


SEED_SQL = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "db", "seed", "seed.sql"
)


def test_reads_the_seeded_accounts_from_seed_sql():
    accounts = dry_run.read_accounts_from_seed(SEED_SQL)

    by_username = {a.username: a for a in accounts}
    # 14 since the S3 review queue added `drkim` (role='clinician') to the
    # seed — the queue is gated on a permission `staff` does not hold, so
    # without one clinical account the feature is unreachable by anyone —
    # plus the demo-readiness slice's `dwhite` (role='roi_clerk').
    assert len(accounts) == 14
    assert by_username["drpatel"].full_name == "Dr. Anil Patel"
    assert by_username["itadmin"].full_name == "Helix Support"
    assert by_username["dwhite"].full_name == "Dana White"
    # Every seeded account is on the legacy role — the thing the migration exists
    # to change. If this ever fails, the seed moved ahead of the migration.
    # Was {"staff"} alone, and that was the point: it pinned the pre-migration
    # reality where every account carries the deprecated flat role. That is
    # still true of the eleven original accounts, and the assertion keeps
    # saying so — but `drkim` and `drnguyen` (2026-08-22, promoted from
    # `staff`) are deliberately `clinician`, because the S3 review queue is
    # gated on a permission `staff` does not hold and a SINGLE clinical
    # account could only ever prove exclusive access, never that patient 1738's
    # deliberate two-reviewer overlap works. These are demo accounts with
    # obvious roles, not the roster-gated account migration. `dwhite` is a
    # third such deliberate exception: a real `roi_clerk` demo identity, not
    # a step toward the general migration either.
    assert {a.role for a in accounts} == {"staff", "clinician", "roi_clerk"}
    assert sum(1 for a in accounts if a.role == "clinician") == 2
    assert sum(1 for a in accounts if a.role == "roi_clerk") == 1
    assert all(a.is_active for a in accounts)


def test_the_seeded_accounts_produce_the_documented_split():
    # The end-to-end shape the report is meant to show, with no database.
    findings = dry_run.build_report(
        dry_run.read_roster(ROSTER_CSV), dry_run.read_accounts_from_seed(SEED_SQL)
    )

    # Against the CLIENT's roster: eleven of the fourteen seeded accounts map
    # cleanly, and the three that do not are exactly the three that are not
    # people. Nothing lands in DENY BY DEFAULT or UNRECOGNISED STATUS — before
    # the vocabulary fix, seven real staff sat in the former and three dated
    # statuses in the latter.
    #
    # Demo-readiness slice: `dwhite` (role='roi_clerk') is the eleventh —
    # normalise_name() strips parenthetical decoration, so 'Dana White (ROI
    # Clerk)' (roiclerk) and 'Dana White' (dwhite) key to the same roster
    # row; both are the same demo person and both legitimately migrate.
    assert len(_outcomes(findings, dry_run.MIGRATE)) == 11
    assert len(_outcomes(findings, dry_run.DECIDE_NO_OWNER)) == 3   # frontdesk, labtech, itadmin
    assert len(_outcomes(findings, dry_run.DISABLE_DEPARTED)) == 0  # no departure holds an account
    assert len(_outcomes(findings, dry_run.DEPARTED_CHECKED)) == 2  # Marcus Hale, Erin Castillo
    assert len(_outcomes(findings, dry_run.UNMAPPED_FUNCTION)) == 0
    assert len(_outcomes(findings, dry_run.UNKNOWN_STATUS)) == 0


def test_a_missing_users_insert_yields_no_accounts_rather_than_crashing(tmp_path):
    empty = tmp_path / "seed.sql"
    empty.write_text("-- no users here\n")

    assert dry_run.read_accounts_from_seed(str(empty)) == []


# --- PR #32 review fixes ---------------------------------------------------


def test_nothing_is_dropped_covers_roster_rows_too():
    # The invariant as originally claimed, now actually enforced: EVERY roster
    # row appears, whatever its status, not just the active ones.
    roster = dry_run.read_roster(ROSTER_CSV)
    findings = _report()
    subjects = {f.subject for f in findings}
    accounts_by_name = {dry_run.normalise_name(a.full_name) for a in SEEDED}

    for row in roster:
        matched_to_an_account = dry_run.normalise_name(row.name) in accounts_by_name
        assert matched_to_an_account or row.name in subjects, (
            f"{row.name} ({row.status}) appears in no bucket"
        )


def test_a_proposed_role_must_exist_in_the_live_grid():
    # [high]: this file's mapping table and config/roles.yaml can drift. A role
    # the grid does not define grants nothing, so proposing it would move staff
    # into a permissionless role while the report called them migratable.
    known = dry_run.defined_roles()
    assert known, "the grid should be readable"

    for f in _report():
        if f.proposed_role is not None:
            assert f.proposed_role in known, f"proposed undefined role {f.proposed_role}"


def test_a_function_mapping_to_an_undefined_role_is_not_proposed():
    assert dry_run.role_for_function("Physician", known_roles={"clinician"}) == "clinician"
    # Same function, a grid that does not define the role -> no proposal.
    assert dry_run.role_for_function("Physician", known_roles={"front_desk"}) is None


def test_an_unreadable_grid_raises_rather_than_reads_as_zero_roles():
    # P1 review (w8-planner-2): an unreadable grid (missing file, missing
    # PyYAML) must raise, not return set() — a caller cannot distinguish
    # "the grid genuinely defines nothing" from "the grid could not be read
    # at all," and roster_migrate.py auto-deactivates UNMAPPED_FUNCTION,
    # which is exactly what every function would become under an empty
    # known_roles set. A silently empty set previously turned a missing
    # dependency into a mass, unintended deactivation.
    with pytest.raises(dry_run.RolesConfigUnreadable):
        dry_run.defined_roles("/nonexistent/roles.yaml")


def test_role_for_function_with_an_explicit_empty_set_still_proposes_nothing():
    # Distinct from the case above: a CALLER-SUPPLIED empty set (e.g. a test
    # fixture, or a grid that genuinely defines no roles yet) is a normal,
    # valid "propose nothing" input — only defined_roles()'s own internal
    # failure-to-read path must raise.
    assert dry_run.role_for_function("Physician", known_roles=set()) is None


def test_an_unrecognised_status_is_reported_not_filtered_out():
    roster = [dry_run.RosterRow("Ada Lovelace", "Physician", "Medicine", "Main", "sabbatical")]
    findings = dry_run.build_report(roster, [], known_roles={"clinician"})

    assert len(findings) == 1
    assert findings[0].outcome == dry_run.UNKNOWN_STATUS
    assert "sabbatical" in findings[0].detail


def test_a_terminated_person_with_no_account_stays_out_of_needs_account():
    # The original intent still holds: somebody who has left must never be
    # reported as needing an account. What changed on 2026-08-20 is the other
    # half — this used to assert `findings == []`, i.e. that they vanished
    # entirely. That silence was the bug: the client listed departures
    # specifically so the report would say whether a live account exists.
    roster = [dry_run.RosterRow("Gone Person", "Physician", "Medicine", "Main", "terminated")]
    findings = dry_run.build_report(roster, [], known_roles={"clinician"})

    assert not _outcomes(findings, dry_run.NEEDS_ACCOUNT)
    assert not _outcomes(findings, dry_run.MIGRATE)
    assert [f.outcome for f in findings] == [dry_run.DEPARTED_CHECKED]


# --- the client cross-check (review R1-MAJOR-001) ---------------------------


def test_a_disagreement_on_a_MATCHED_account_is_reported():
    """The population the sign-off actually cares about.

    This shipped broken: `cross_check_client_roles` looked the roster person up
    by `finding.subject`, but a MIGRATE subject is a USERNAME, so every matched
    account was silently skipped and the report printed "zero disagreements"
    while comparing nothing. The finding now carries the matched roster name.
    """
    roster = [RosterRow("Ada Byron", "Physician", "Clinical", "Main", "active",
                        client_proposed_role="front_desk")]
    accounts = [Account("abyron", "Dr. Ada Byron", "staff", True)]

    findings = dry_run.cross_check_client_roles(
        dry_run.build_report(roster, accounts, known_roles={"clinician", "front_desk"}),
        roster,
    )
    disagreements = _outcomes(findings, dry_run.ROLE_DISAGREEMENT)

    assert len(disagreements) == 1, "a matched account's role must be cross-checked"
    assert disagreements[0].subject == "abyron"
    # Both sides are named, and neither is presented as the winner.
    assert "clinician" in disagreements[0].detail
    assert "front_desk" in disagreements[0].detail
    assert "must not pick a side" in disagreements[0].detail


def test_agreement_on_a_matched_account_reports_nothing():
    roster = [RosterRow("Ada Byron", "Physician", "Clinical", "Main", "active",
                        client_proposed_role="clinician")]
    accounts = [Account("abyron", "Dr. Ada Byron", "staff", True)]

    findings = dry_run.cross_check_client_roles(
        dry_run.build_report(roster, accounts, known_roles={"clinician"}), roster
    )

    assert not _outcomes(findings, dry_run.ROLE_DISAGREEMENT)


def test_a_roster_side_disagreement_is_also_reported():
    # Someone with no account: the finding's subject IS the roster name, so
    # this path exercises the fallback rather than the carried link.
    roster = [RosterRow("Ada Byron", "Physician", "Clinical", "Main", "active",
                        client_proposed_role="lab")]

    findings = dry_run.cross_check_client_roles(
        dry_run.build_report(roster, [], known_roles={"clinician", "lab"}), roster
    )

    assert len(_outcomes(findings, dry_run.ROLE_DISAGREEMENT)) == 1


def test_the_shipped_roster_genuinely_agrees_with_the_client():
    """Now that the comparison works, "zero disagreements" is a real signal.

    Asserted against the actual roster and the actual seed, because the claim
    that goes to the client for signature is exactly this one.
    """
    roster = dry_run.read_roster(ROSTER_CSV)
    findings = dry_run.cross_check_client_roles(
        dry_run.build_report(roster, dry_run.read_accounts_from_seed(SEED_SQL)), roster
    )

    assert not _outcomes(findings, dry_run.ROLE_DISAGREEMENT)
    # And it compared something: every migrated account carries the roster
    # person it matched, which is what the cross-check needs.
    migrated = _outcomes(findings, dry_run.MIGRATE)
    assert migrated and all(f.roster_name for f in migrated)


def test_an_unrecognised_status_on_a_MATCHED_account_is_never_migrated():
    """Review R2-MAJOR-001 — the gap the round-1 test could not see.

    The status guard existed only on the no-account path, so a status that was
    not exactly "terminated" or "leave" fell through to the role lookup and
    MIGRATED an account that already exists. That is the population the report
    exists to gate: a typo silently changed somebody's role.

    The pre-existing test at test_an_unrecognised_status_is_reported_not_filtered_out
    passes an EMPTY account list, which is why it never caught this.
    """
    roster = [RosterRow("Ada Byron", "Physician", "Clinical", "Main", "sabbatical",
                        raw_status="sabbatical")]
    accounts = [Account("abyron", "Ada Byron", "staff", True)]

    findings = dry_run.build_report(roster, accounts, known_roles={"clinician"})

    assert not _outcomes(findings, dry_run.MIGRATE), "an undefined status must never migrate"
    assert [f.outcome for f in findings] == [dry_run.UNKNOWN_STATUS]
    finding = findings[0]
    assert finding.subject == "abyron"
    assert finding.proposed_role is None
    # Quote what the roster said, so an operator can find and fix the row.
    assert "sabbatical" in finding.detail


def test_a_recognised_status_on_a_matched_account_still_migrates():
    # The guard must not be so broad that it blocks the normal path — that
    # would turn a silent-migration bug into a silent-nothing-happens bug.
    roster = [RosterRow("Ada Byron", "Physician", "Clinical", "Main", "active")]
    accounts = [Account("abyron", "Ada Byron", "staff", True)]

    findings = dry_run.build_report(roster, accounts, known_roles={"clinician"})

    assert [f.outcome for f in findings] == [dry_run.MIGRATE]
    assert findings[0].proposed_role == "clinician"


def test_the_shipped_roster_has_no_undefined_statuses_on_matched_accounts():
    # Belt and braces on the real data: the client's dated statuses normalise
    # cleanly, so nothing should be sitting in this bucket today.
    roster = dry_run.read_roster(ROSTER_CSV)
    findings = dry_run.build_report(roster, dry_run.read_accounts_from_seed(SEED_SQL))

    assert not _outcomes(findings, dry_run.UNKNOWN_STATUS)
