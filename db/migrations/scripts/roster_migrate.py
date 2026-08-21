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
    decide_no_roster_owner             -> deactivate, reason role_migration_no_owner
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
    UNKNOWN_STATUS,
    UNMAPPED_FUNCTION,
    build_report,
    cross_check_client_roles,
    read_accounts_from_seed,
    read_roster,
)

# Outcome -> (deactivate?, disabled_reason). Absent = no account action.
_DEACTIVATE = {
    DECIDE_NO_OWNER: "role_migration_no_owner",
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
            # HOLD_ON_LEAVE and anything added later: reported, never guessed at.
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


def apply_plan(conn, migrations, deactivations, approved_by):
    """Single transaction. A partial migration leaves accounts in states nobody
    signed off on, so this either completes or leaves the database untouched."""
    cur = conn.cursor()
    for f in migrations:
        cur.execute(
            "UPDATE users SET role = %s, is_active = TRUE, disabled_reason = NULL "
            "WHERE username = %s RETURNING role",
            (f.proposed_role, f.subject),
        )
        cur.execute(
            "INSERT INTO role_migration_log "
            "(username, from_role, to_role, outcome, detail, roster_name, approved_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (f.subject, None, f.proposed_role, f.outcome, f.detail, f.roster_name, approved_by),
        )
    for f, reason in deactivations:
        cur.execute(
            "UPDATE users SET is_active = FALSE, disabled_reason = %s WHERE username = %s",
            (reason, f.subject),
        )
        cur.execute(
            "INSERT INTO role_migration_log "
            "(username, from_role, to_role, outcome, detail, roster_name, approved_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (f.subject, None, None, f.outcome, f.detail, f.roster_name, approved_by),
        )
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

    findings = cross_check_client_roles(build_report(roster, accounts), roster)

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
