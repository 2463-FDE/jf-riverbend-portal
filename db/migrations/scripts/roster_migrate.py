#!/usr/bin/env python3
"""Apply a signed roster mapping report. Branch 9 part 2.

`roster_dry_run.py` produces the report the client signs. This applies exactly
that report and nothing else: it imports the same `build_report`, so the outcome
for every account is decided by the code the client reviewed, not by a second
implementation that could drift from it.

DRY RUN BY DEFAULT. Without `--apply` this changes nothing and prints what it
would do. `--apply` requires `--approved-by`, because an unattributed migration
of every staff account is not something this script should be able to do.

What each outcome does:

    migrate                            -> set users.role, keep the account active
    decide_no_roster_owner             -> LEFT ALONE, reported — the data cannot
                                           tell a shared login (frontdesk,
                                           labtech) from a departed owner's
                                           account (itadmin); a human resolves
                                           each individually, this script does not
    unmapped_function_deny_by_default  -> deactivate, reason role_migration_unmapped
    unknown_roster_status              -> deactivate, reason role_migration_unmapped
    disable_person_left                -> deactivate, reason role_migration_unmapped
    hold_on_leave_undecided            -> LEFT ALONE, reported
    needs_account / departure_checked  -> nothing to do (no account exists)
    role_disagreement_with_client      -> BLOCKS the run entirely

A disagreement between the report's derived role and the client's proposed role
aborts before any write. The report is meant to be signed with none outstanding;
if one appears, the signature covered something other than what would be
applied.

Deactivated accounts cannot authenticate at all, and an account carrying a
`role_migration_*` reason gets the client's specified copy at login rather than
"invalid username or password" — see services/gateway/app.py.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from roster_dry_run import (  # noqa: E402
    DECIDE_NO_OWNER,
    DEPARTED_CHECKED,
    DISABLE_DEPARTED,
    HOLD_ON_LEAVE,
    MIGRATE,
    NEEDS_ACCOUNT,
    ROLE_DISAGREEMENT,
    RolesConfigUnreadable,
    UNKNOWN_STATUS,
    UNMAPPED_FUNCTION,
    build_report,
    cross_check_client_roles,
    read_accounts_from_seed,
    read_roster,
)

# Outcome -> (deactivate?, disabled_reason). Absent = no account action.
#
# DECIDE_NO_OWNER is deliberately absent. roster_dry_run.py's own contract for
# this outcome (see its module docstring and test_an_account_nobody_owns_is_
# never_migrated) is that the data cannot tell a shared desk login (frontdesk,
# labtech — actively used by real staff, blocked on the client naming the
# fourth front-desk float staffer) apart from a departed contractor's orphaned
# account (itadmin) — both are "a name that isn't a person's." Auto-
# deactivating this bucket previously treated "goes to a human" as "deny by
# default," which would silently lock the demo's own shared front-desk login
# the moment this script's --apply ran. UNMAPPED_FUNCTION, UNKNOWN_STATUS and
# DISABLE_DEPARTED stay auto-applied: those are not ambiguous — a function
# nobody mapped, a status nobody defined, and a status the roster itself marks
# terminated are each already a specific, unambiguous fact, not a "which case
# is this" judgment call.
_DEACTIVATE = {
    UNMAPPED_FUNCTION: "role_migration_unmapped",
    UNKNOWN_STATUS: "role_migration_unmapped",
    DISABLE_DEPARTED: "role_migration_unmapped",
}
_NO_ACCOUNT = {NEEDS_ACCOUNT, DEPARTED_CHECKED}


def plan(findings):
    """(migrations, deactivations, untouched) — pure, so it is testable without a database."""
    migrations, deactivations, untouched = [], [], []
    for f in findings:
        if f.outcome == MIGRATE:
            migrations.append(f)
        elif f.outcome in _DEACTIVATE:
            deactivations.append((f, _DEACTIVATE[f.outcome]))
        elif f.outcome in _NO_ACCOUNT:
            continue
        else:
            # DECIDE_NO_OWNER, HOLD_ON_LEAVE, and anything added later:
            # reported, never guessed at.
            untouched.append(f)
    return migrations, deactivations, untouched


def format_plan(migrations, deactivations, untouched, applied):
    verb = "APPLIED" if applied else "WOULD APPLY (dry run — nothing changed)"
    out = [f"ROSTER MIGRATION — {verb}", ""]
    out.append(f"MIGRATE — role assigned  [{len(migrations)}]")
    out += [f"    {f.subject} -> {f.proposed_role}" for f in migrations] or ["    (none)"]
    out.append("")
    out.append(f"DEACTIVATE — cannot authenticate  [{len(deactivations)}]")
    out += [f"    {f.subject}  reason={reason}" for f, reason in deactivations] or ["    (none)"]
    out.append("")
    out.append(f"LEFT ALONE — needs a human decision  [{len(untouched)}]")
    out += [f"    {f.subject}  ({f.outcome})" for f in untouched] or ["    (none)"]
    out.append("")
    out.append("The deactivated accounts above are the list to return to the client.")
    out.append("Each will see: \"access is being updated, contact your supervisor\"")
    return "\n".join(out)


class AccountMissing(Exception):
    """A planned account does not exist in the target database.

    The plan is built from the committed seed, then written to whatever
    DATABASE_URL points at. Those can diverge — a username renamed, an account
    deleted, a database seeded from a different revision. Aborting the whole
    transaction is the only safe answer: a log row claiming an account was
    migrated when no row changed is worse than no migration at all.
    """


def _lock_and_read_role(cur, username: str) -> str:
    """Lock the target row and return its CURRENT role, or raise.

    Two review findings share this one fix (R1-MAJOR-001, R1-MAJOR-002). The
    previous version issued `UPDATE ... RETURNING role` and never fetched the
    result, so a zero-row match committed silently and still wrote a log row
    saying the account had been migrated. It also passed from_role=None, so the
    log recorded "to what" but not "from what" — losing the real distinction
    between `drkim` (already `clinician`) and the accounts on the deprecated
    flat `staff` role, which is exactly the question the table exists to answer.

    SELECT ... FOR UPDATE both proves the row exists and captures the prior
    role, under a lock held for the rest of the transaction.
    """
    cur.execute("SELECT role FROM users WHERE username = %s FOR UPDATE", (username,))
    row = cur.fetchone()
    if row is None:
        raise AccountMissing(
            f"account {username!r} is in the approved plan but does not exist in the "
            f"target database. The plan was built from db/seed/seed.sql; this database "
            f"has diverged from it. Nothing has been changed."
        )
    return row[0]


def _log(cur, username, from_role, to_role, finding, approved_by):
    cur.execute(
        "INSERT INTO role_migration_log "
        "(username, from_role, to_role, outcome, detail, roster_name, approved_by) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (username, from_role, to_role, finding.outcome, finding.detail,
         finding.roster_name, approved_by),
    )


def apply_plan(conn, migrations, deactivations, approved_by):
    """Single transaction. A partial migration leaves accounts in states nobody
    signed off on, so this either completes or leaves the database untouched.

    Every write is preceded by a locked read that proves the row exists and
    yields its prior role. Any missing account aborts everything.
    """
    cur = conn.cursor()
    try:
        for f in migrations:
            from_role = _lock_and_read_role(cur, f.subject)
            cur.execute(
                "UPDATE users SET role = %s, is_active = TRUE, disabled_reason = NULL "
                "WHERE username = %s",
                (f.proposed_role, f.subject),
            )
            if cur.rowcount != 1:
                raise AccountMissing(
                    f"expected to update exactly one row for {f.subject!r}, "
                    f"updated {cur.rowcount}. Nothing has been changed."
                )
            _log(cur, f.subject, from_role, f.proposed_role, f, approved_by)

        for f, reason in deactivations:
            from_role = _lock_and_read_role(cur, f.subject)
            cur.execute(
                "UPDATE users SET is_active = FALSE, disabled_reason = %s WHERE username = %s",
                (reason, f.subject),
            )
            if cur.rowcount != 1:
                raise AccountMissing(
                    f"expected to update exactly one row for {f.subject!r}, "
                    f"updated {cur.rowcount}. Nothing has been changed."
                )
            _log(cur, f.subject, from_role, None, f, approved_by)
    except Exception:
        conn.rollback()
        raise
    conn.commit()


def main(argv):
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--roster", default=os.path.join(here, "..", "..", "seed", "staff_roster_SYNTHETIC.csv"))
    ap.add_argument("--apply", action="store_true", help="write to the database (default: dry run)")
    ap.add_argument("--approved-by", help="who signed the report this run applies; required with --apply")
    args = ap.parse_args(argv[1:])

    if args.apply and not args.approved_by:
        print("--apply requires --approved-by: an unattributed role migration is not applied.", file=sys.stderr)
        return 2

    roster = read_roster(args.roster)
    accounts = read_accounts_from_seed(os.path.join(here, "..", "..", "seed", "seed.sql"))
    if not accounts:
        print("no accounts found — nothing to migrate.", file=sys.stderr)
        return 2

    try:
        findings = cross_check_client_roles(build_report(roster, accounts), roster)
    except RolesConfigUnreadable as exc:
        # P1 review (w8-planner-2): must abort before building any plan, not
        # only before applying one — a plan built against an empty role set
        # would misreport every function as unmapped even in dry-run output,
        # and printing that plan (even unapplied) is itself a wrong answer.
        print(f"REFUSING TO RUN — {exc}", file=sys.stderr)
        return 2

    disagreements = [f for f in findings if f.outcome == ROLE_DISAGREEMENT]
    if disagreements:
        print("REFUSING TO RUN — the report disagrees with the client's proposed roles:", file=sys.stderr)
        for f in disagreements:
            print(f"    {f.subject}: {f.detail}", file=sys.stderr)
        print("\nResolve every disagreement and re-sign the report first.", file=sys.stderr)
        return 3

    migrations, deactivations, untouched = plan(findings)

    if not args.apply:
        print(format_plan(migrations, deactivations, untouched, applied=False))
        print("\nRe-run with --apply --approved-by \"<name>\" to write these changes.")
        return 0

    try:
        import psycopg2
    except ImportError:
        print("psycopg2 is required to --apply.", file=sys.stderr)
        return 2
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL must be set to --apply.", file=sys.stderr)
        return 2
    conn = psycopg2.connect(dsn)
    try:
        apply_plan(conn, migrations, deactivations, args.approved_by)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    print(format_plan(migrations, deactivations, untouched, applied=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
