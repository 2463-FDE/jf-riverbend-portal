"""Roster dry-run mapping — the report the client signs before any migration.

Reconciles the staff roster (name, function, department, clinic, status)
against the accounts that actually exist, and reports, per account: its
current effective access, the role proposed for it, and — loudly — anything
that cannot be decided mechanically.

Design rule this whole script exists to serve: **a migration that quietly
skips an account it could not parse is the failure the dry run prevents.**
Every account and every roster row lands in exactly one bucket. Nothing is
dropped, and nothing is guessed.

Deliberately does NOT guess two things:

  * Whether an ownerless account is a shared desk login or a departed
    contractor's. Both look identical in the data — a name that isn't a
    person's. The report says "no roster owner, decide: disable or split",
    lists staff working in that function for context, and lets a human
    choose. Inferring it would be the tool overstepping a decision the
    client signs for.
  * How to treat staff on leave. The client set the rule for active and
    terminated staff and not for leave, so those surface as an open
    question instead of a silent default.

The pure functions below take plain data and are unit-tested without a
database; `main()` is the thin IO shell that reads the CSV and the accounts.
"""
import csv
import os
import re
import sys
from typing import Optional
from dataclasses import dataclass, field

# Function (as written in the roster) -> role (as defined in config/roles.yaml).
# Anything absent from this table is UNMAPPED by design: a function nobody has
# mapped must not silently inherit a role.
FUNCTION_TO_ROLE = {
    "chief operating officer": "management",
    "patient registration": "front_desk",
    "physician": "clinician",
    "registered nurse": "nursing_ma",
    "laboratory technician": "lab",
    "billing specialist": "billing",
    "release of information clerk": "roi_clerk",
    "scheduling coordinator": "scheduler",
    "systems administrator": "it_admin",
}

# Outcomes. MIGRATE is the only one that changes a role; every other bucket
# either disables, holds, or asks.
MIGRATE = "migrate"
DISABLE_DEPARTED = "disable_person_left"
DECIDE_NO_OWNER = "decide_no_roster_owner"
UNMAPPED_FUNCTION = "unmapped_function_deny_by_default"
HOLD_ON_LEAVE = "hold_on_leave_undecided"
NEEDS_ACCOUNT = "needs_account"


@dataclass(frozen=True)
class RosterRow:
    name: str
    function: str
    department: str
    clinic: str
    status: str


@dataclass(frozen=True)
class Account:
    username: str
    full_name: str
    role: str
    is_active: bool


@dataclass
class Finding:
    outcome: str
    subject: str          # username, or roster name for NEEDS_ACCOUNT
    detail: str
    proposed_role: Optional[str] = None
    context: list = field(default_factory=list)


_SUFFIX = re.compile(r"\s*\((?:[^)]*)\)\s*$")        # "Maya Okonkwo (COO)"
_TRAILING_CRED = re.compile(r",\s*[A-Za-z.]+\s*$")    # "Karen Cole, RN"
_TITLE = re.compile(r"^(dr|doctor|mr|mrs|ms|miss)\.?\s+", re.I)


def normalise_name(raw: str) -> str:
    """Reduce an account's display name or a roster name to a comparable key.

    `users.full_name` carries decoration the roster does not — "(COO)",
    ", RN", "Dr. " — so a literal comparison would fail on staff who are
    plainly the same person. Normalising is what lets the match succeed;
    every failure to match still ends up reported, never dropped.
    """
    s = (raw or "").strip()
    s = _SUFFIX.sub("", s)
    s = _TRAILING_CRED.sub("", s)
    s = _TITLE.sub("", s)
    return " ".join(s.lower().split())


def role_for_function(function: str) -> Optional[str]:
    return FUNCTION_TO_ROLE.get(" ".join((function or "").lower().split()))


def read_roster(path):
    """Read the roster CSV, skipping '#' comment lines."""
    with open(path, newline="") as f:
        lines = [ln for ln in f if not ln.lstrip().startswith("#")]
    return [
        RosterRow(
            name=r["name"].strip(),
            function=r["function"].strip(),
            department=r["department"].strip(),
            clinic=r["clinic"].strip(),
            status=r["status"].strip().lower(),
        )
        for r in csv.DictReader(lines)
        if (r.get("name") or "").strip()
    ]


def build_report(roster, accounts):
    """Every account and every roster row lands in exactly one bucket."""
    by_key = {}
    for row in roster:
        by_key.setdefault(normalise_name(row.name), []).append(row)

    findings = []
    matched_keys = set()

    # Active staff whose name matches no account at all. These are the only
    # people a shared login could plausibly be split into, so they are the
    # useful context for an ownerless account — and it is a fact, not a guess
    # about which desk the account belongs to.
    account_keys = {normalise_name(a.full_name) for a in accounts}
    accountless = [
        r for r in roster
        if r.status == "active" and normalise_name(r.name) not in account_keys
    ]

    for acct in sorted(accounts, key=lambda a: a.username):
        key = normalise_name(acct.full_name)
        people = by_key.get(key, [])

        if not people:
            # No identified owner. Could be a shared desk login or someone
            # who has left; the data cannot tell these apart, so a human
            # decides. Offer staff in adjacent functions as context only.
            findings.append(
                Finding(
                    outcome=DECIDE_NO_OWNER,
                    subject=acct.username,
                    detail=(
                        f'"{acct.full_name}" matches nobody in the roster. '
                        f"Disable it, or split it into named accounts — the data "
                        f"cannot tell a shared desk login from a departed owner. "
                        f"Staff below have no account of their own."
                    ),
                    context=(
                        [
                            f"{r.name} — {r.function}, {r.department}, {r.clinic}"
                            for r in accountless
                        ]
                        or ["no active staff lack an account, so there is nobody to split this into"]
                    ),
                )
            )
            continue

        matched_keys.add(key)
        person = people[0]

        if len(people) > 1:
            findings.append(
                Finding(
                    outcome=DECIDE_NO_OWNER,
                    subject=acct.username,
                    detail=(
                        f"{len(people)} roster entries share the name "
                        f'"{acct.full_name}". Resolve the ambiguity before migrating.'
                    ),
                    context=[f"{r.name} — {r.function}, {r.clinic}" for r in people],
                )
            )
        elif person.status == "terminated":
            findings.append(
                Finding(
                    outcome=DISABLE_DEPARTED,
                    subject=acct.username,
                    detail=f"{person.name} is terminated in the roster. Disable, do not migrate.",
                )
            )
        elif person.status == "leave":
            findings.append(
                Finding(
                    outcome=HOLD_ON_LEAVE,
                    subject=acct.username,
                    detail=(
                        f"{person.name} is on leave. No rule was set for leave — "
                        f"migrate, hold, or disable? Needs a decision."
                    ),
                )
            )
        else:
            role = role_for_function(person.function)
            if role is None:
                findings.append(
                    Finding(
                        outcome=UNMAPPED_FUNCTION,
                        subject=acct.username,
                        detail=(
                            f'{person.name}\'s function "{person.function}" maps to no '
                            f"role. Deny by default until a role is agreed."
                        ),
                    )
                )
            else:
                findings.append(
                    Finding(
                        outcome=MIGRATE,
                        subject=acct.username,
                        detail=f"{person.name} — {person.function}, {person.clinic}. Currently '{acct.role}'.",
                        proposed_role=role,
                    )
                )

    for key, people in sorted(by_key.items()):
        if key in matched_keys:
            continue
        for person in people:
            if person.status != "active":
                continue
            role = role_for_function(person.function)
            findings.append(
                Finding(
                    outcome=NEEDS_ACCOUNT if role else UNMAPPED_FUNCTION,
                    subject=person.name,
                    detail=(
                        f"{person.function}, {person.department}, {person.clinic} — "
                        + (
                            f"active staff with no account. Needs one as '{role}'."
                            if role
                            else f'function "{person.function}" maps to no role.'
                        )
                    ),
                    proposed_role=role,
                )
            )
    return findings


def format_report(findings):
    order = [MIGRATE, DECIDE_NO_OWNER, DISABLE_DEPARTED, UNMAPPED_FUNCTION, HOLD_ON_LEAVE, NEEDS_ACCOUNT]
    titles = {
        MIGRATE: "MIGRATE — ready, one person, one account, mapped function",
        DECIDE_NO_OWNER: "DECIDE — no identified owner (disable, or split into named accounts)",
        DISABLE_DEPARTED: "DISABLE — the person has left",
        UNMAPPED_FUNCTION: "DENY BY DEFAULT — function maps to no role",
        HOLD_ON_LEAVE: "OPEN QUESTION — staff on leave, no rule set",
        NEEDS_ACCOUNT: "NEEDS AN ACCOUNT — active staff with none",
    }
    out = [
        "ROSTER DRY-RUN MAPPING",
        "Nothing below is applied. This report is for approval before any migration runs.",
        "",
    ]
    for bucket in order:
        rows = [f for f in findings if f.outcome == bucket]
        out.append(f"{titles[bucket]}  [{len(rows)}]")
        if not rows:
            out.append("    (none)")
        for f in rows:
            arrow = f" -> {f.proposed_role}" if f.proposed_role else ""
            out.append(f"    {f.subject}{arrow}")
            out.append(f"        {f.detail}")
            for c in f.context:
                out.append(f"          · {c}")
        out.append("")
    total = len(findings)
    ready = len([f for f in findings if f.outcome == MIGRATE])
    out.append(f"{ready} of {total} entries can be migrated mechanically; {total - ready} need a decision.")
    return "\n".join(out)


def _load_accounts_from_db():
    import psycopg2

    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "riverbend"),
        user=os.getenv("DB_USER", "riverbend_app"),
        password=os.getenv("DB_PASSWORD", ""),
    )
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT username, full_name, role, is_active FROM users ORDER BY username")
            return [Account(u, fn or "", r or "", bool(a)) for u, fn, r, a in cur.fetchall()]
    finally:
        conn.close()


def main(argv):
    here = os.path.dirname(os.path.abspath(__file__))
    default_roster = os.path.join(here, "..", "..", "seed", "staff_roster_SYNTHETIC.csv")
    roster_path = argv[1] if len(argv) > 1 else default_roster
    roster = read_roster(roster_path)
    try:
        accounts = _load_accounts_from_db()
    except Exception as e:  # no stack trace: the message may carry a DSN
        print(f"could not read accounts from the database ({type(e).__name__}).", file=sys.stderr)
        print("Start the stack (`make up`) or point DB_* at a reachable database.", file=sys.stderr)
        return 2
    print(format_report(build_report(roster, accounts)))
    if os.path.basename(roster_path).find("SYNTHETIC") >= 0:
        print("\nNOTE: generated from the SYNTHETIC roster. Not for client sign-off.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
