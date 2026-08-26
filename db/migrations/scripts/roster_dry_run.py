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
import pathlib
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
    # --- the client's own wording, added 2026-08-20 with their roster --------
    # Their functions are more specific than ours were, and every one of these
    # previously fell through to UNMAPPED — which reported seven real staff as
    # "deny by default" in the safe column. A specialty is not a role: all three
    # physician variants are `clinician`, exactly as the signed matrix has it.
    "physician family medicine": "clinician",
    "physician internal medicine": "clinician",
    "physician pediatrics": "clinician",
    "medical assistant": "nursing_ma",   # same note area as clinicians, per the matrix
    "scheduler": "scheduler",
    "it administrator": "it_admin",
    "practice manager": "management",    # reporting only; asks a clinician for a chart
    # Agency placements do the same job as the permanent role. The temporary
    # part of their status is handled by status, not by a different role —
    # giving an agency registrar a weaker role than a permanent one would be a
    # policy invention nobody asked for.
    "patient registration agency": "front_desk",
    "front desk agency": "front_desk",
}

# Outcomes. MIGRATE is the only one that changes a role; every other bucket
# either disables, holds, or asks.
MIGRATE = "migrate"
DISABLE_DEPARTED = "disable_person_left"
DECIDE_NO_OWNER = "decide_no_roster_owner"
UNMAPPED_FUNCTION = "unmapped_function_deny_by_default"
HOLD_ON_LEAVE = "hold_on_leave_undecided"
NEEDS_ACCOUNT = "needs_account"
UNKNOWN_STATUS = "unknown_roster_status"
ROLE_DISAGREEMENT = "role_disagreement_with_client"
DEPARTED_CHECKED = "departure_checked_no_account"

# The client writes dated statuses; this mapper's logic is built on three plain
# ones. Normalising at READ time keeps the decision branches unchanged and
# keeps the raw value for the report, rather than scattering date parsing
# through the outcome logic.
#
#   departed_2026-05      -> terminated  (they have left; disable if found)
#   temp_ends_2026-09-30  -> active      (working now; the end date is an
#                                         expiry to set at provisioning, NOT a
#                                         reason to treat them as inactive)
_DEPARTED_RE = re.compile(r"^departed[_-](\d{4}-\d{2}(?:-\d{2})?)$")
_TEMP_RE = re.compile(r"^temp[_-]ends[_-](\d{4}-\d{2}-\d{2})$")


def normalise_status(raw: str):
    """Return (status, expires_on). An unrecognised value passes through
    untouched so it still lands in UNKNOWN_STATUS — never silently coerced to
    `active`, which would migrate somebody on the strength of a typo."""
    value = (raw or "").strip().lower()
    m = _DEPARTED_RE.match(value)
    if m:
        return "terminated", None
    m = _TEMP_RE.match(value)
    if m:
        return "active", m.group(1)
    return value, None


@dataclass(frozen=True)
class RosterRow:
    name: str
    function: str
    department: str
    clinic: str
    status: str
    # What the roster literally said, kept so the report can quote it. A
    # normalised "terminated" tells an operator less than "departed_2026-05".
    raw_status: str = ""
    # Set only for a temp/agency placement. An account for one of these must be
    # created WITH an expiry — the client's instruction was explicit: "do not
    # rely on someone remembering".
    expires_on: Optional[str] = None
    # The client's own proposal. Cross-check only; the mapping still derives
    # its own and the report flags disagreement.
    client_proposed_role: Optional[str] = None


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
    # The roster person this finding was matched to, when there is one. Set so
    # the client cross-check can find them: a MIGRATE finding's `subject` is a
    # USERNAME, and re-deriving the person from it would mean repeating the
    # name-matching logic — which is exactly how the cross-check came to
    # silently skip every migrated account (review R1-MAJOR-001).
    roster_name: Optional[str] = None


_SUFFIX = re.compile(r"\s*\((?:[^)]*)\)\s*$")        # "Maya Okonkwo (COO)"
_TRAILING_CRED = re.compile(r",\s*[A-Za-z.]+\s*$")    # "Karen Cole, RN"
_TITLE = re.compile(r"^(dr|doctor|mr|mrs|ms|miss)\.?\s+", re.I)
# "Anil Patel MD" — a credential with no comma before it. The client's roster
# writes them this way while `users.full_name` writes "Dr. Anil Patel", so
# without this five of the six clinical accounts (drkim, drpatel, drnguyen,
# drlee, nurse_kc) failed to match and were reported as having no owner —
# recommending that four working clinicians be disabled. Anchored and
# enumerated rather than "any trailing word", so a real surname like "Bright"
# or "Reyes" is never eaten.
_BARE_CRED = re.compile(
    r"\s+(md|do|rn|lpn|np|pa|ma|rd|phd|dds|pharmd|mph|msn|bsn)\.?$", re.I
)


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
    s = _BARE_CRED.sub("", s)
    s = _TITLE.sub("", s)
    return " ".join(s.lower().split())


class RolesConfigUnreadable(RuntimeError):
    """The roles grid itself could not be read or parsed — missing file,
    missing PyYAML, malformed YAML. This is a fatal setup problem, not "the
    grid defines zero roles": defined_roles() must never collapse the two,
    because an empty-but-successfully-read set and a failed read produce the
    identical membership test (`role in known_roles` is False either way).
    P1 review (w8-planner-2): the previous behavior — return set() on any
    exception — meant a missing PyYAML dependency made every function read
    as unmapped, and roster_migrate.py's UNMAPPED_FUNCTION outcome is
    auto-deactivated by design (it IS supposed to be unambiguous). A missing
    dependency in a minimal checkout therefore silently turned "migrate ten
    staff to their real roles" into "deactivate ten staff who cannot
    authenticate" — the exact opposite of what --apply's caller approved,
    with the same command, same flags, same exit code shape. Callers must
    let this propagate and abort before planning or applying anything; see
    roster_migrate.py's main().
    """


def defined_roles(roles_yaml_path=None):
    """Role names the live config actually defines.

    PR #32 review [high]: FUNCTION_TO_ROLE is a table in this file, and
    config/roles.yaml is the thing that grants permissions. If they drift, this
    report can propose a role that does not exist — and because the enforcement
    layer fails closed on an unknown role, a signed migration would move valid
    staff into a role with NO permissions while the report called them
    mechanically migratable. Access loss, presented as the safe column.

    So the mapping is validated against the grid rather than trusted.

    Raises RolesConfigUnreadable if the grid cannot be read at all — see that
    class's docstring for why this must never fall back to an empty set.
    """
    if roles_yaml_path is None:
        here = os.path.dirname(os.path.abspath(__file__))
        roles_yaml_path = os.path.join(here, "..", "..", "..", "config", "roles.yaml")
    try:
        import yaml

        with open(roles_yaml_path) as f:
            return set((yaml.safe_load(f) or {}).get("roles", {}))
    except Exception as exc:
        raise RolesConfigUnreadable(
            f"could not read the roles grid at {roles_yaml_path!r} "
            f"({type(exc).__name__}) — refusing to propose or apply anything "
            f"against an unvalidated role set."
        ) from exc


def role_for_function(function: str, known_roles=None) -> Optional[str]:
    role = FUNCTION_TO_ROLE.get(" ".join((function or "").lower().split()))
    if role is None:
        return None
    if known_roles is None:
        known_roles = defined_roles()
    # A role this file maps to but the grid does not define is not a proposal.
    return role if role in known_roles else None


def _row(r) -> RosterRow:
    raw = (r.get("status") or "").strip()
    status, expires_on = normalise_status(raw)
    proposed = (r.get("proposed_role") or "").strip() or None
    return RosterRow(
        name=r["name"].strip(),
        function=r["function"].strip(),
        department=r["department"].strip(),
        clinic=r["clinic"].strip(),
        status=status,
        raw_status=raw,
        expires_on=expires_on,
        client_proposed_role=proposed,
    )


def read_roster(path):
    """Read the roster CSV, skipping '#' comment lines.

    `proposed_role` is optional: a roster without it still reads, and the
    cross-check simply has nothing to compare against."""
    with open(path, newline="") as f:
        lines = [ln for ln in f if not ln.lstrip().startswith("#")]
    return [
        _row(r)
        for r in csv.DictReader(lines)
        if (r.get("name") or "").strip()
    ]


def build_report(roster, accounts, known_roles=None):
    """Every account and every roster row lands in exactly one bucket."""
    if known_roles is None:
        known_roles = defined_roles()
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
        elif person.status != "active":
            # Review R2-MAJOR-001. This branch did not exist, so any status
            # that was not exactly "terminated" or "leave" fell through to the
            # role lookup below and was MIGRATED — on an account that already
            # exists, which is the population this whole report exists to gate.
            # A typo like "sabbatical" silently changed somebody's role.
            #
            # The no-account path had this guard from the start; the matched
            # path did not, and the round-1 test only exercised the former,
            # which is why it survived a review. Never infer "active" from the
            # absence of a recognised status.
            findings.append(
                Finding(
                    outcome=UNKNOWN_STATUS,
                    subject=acct.username,
                    detail=(
                        f'{person.name} has roster status "{person.raw_status or person.status}", '
                        f"which has no defined handling. Not migrated — define the status or "
                        f"correct the roster first."
                    ),
                    roster_name=person.name,
                )
            )
        else:
            role = role_for_function(person.function, known_roles)
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
                        roster_name=person.name,
                    )
                )

    for key, people in sorted(by_key.items()):
        if key in matched_keys:
            continue
        for person in people:
            if person.status == "terminated":
                # Reporting them as "needs an account" would be actively wrong —
                # but dropping them silently is worse, and was the behaviour
                # until 2026-08-20. The client listed three departures and said
                # exactly why: "I do not know whether these still have live
                # accounts... I would rather they surface in your dry run than
                # in an audit." A report that answers nothing about them does
                # not answer the question they asked.
                findings.append(
                    Finding(
                        outcome=DEPARTED_CHECKED,
                        subject=person.name,
                        detail=(
                            f"{person.function or 'role not stated'} — roster says "
                            f"{person.raw_status or person.status}. Checked against every "
                            f"account: no live account found under this name. Nothing to "
                            f"disable. If they held an account under a username variant "
                            f"this name-based check would miss it."
                        ),
                    )
                )
                continue
            if person.status == "leave":
                # PR #32 review [medium]: previously skipped entirely, so an
                # on-leave person with no account appeared in NO bucket — which
                # broke this report's own "nothing is dropped" guarantee and
                # contradicted what the client was told about leave surfacing
                # as an open question.
                findings.append(
                    Finding(
                        outcome=HOLD_ON_LEAVE,
                        subject=person.name,
                        detail=(
                            f"{person.function}, {person.department}, {person.clinic} — "
                            f"on leave with no account. No rule was set for leave: "
                            f"provision on return, or hold? Needs a decision."
                        ),
                    )
                )
                continue
            if person.status != "active":
                # An unrecognised status is reported, never filtered out — the
                # whole point is that nothing disappears silently.
                findings.append(
                    Finding(
                        outcome=UNKNOWN_STATUS,
                        subject=person.name,
                        detail=(
                            f'unrecognised roster status "{person.status}" — '
                            f"cannot be actioned until it is defined."
                        ),
                    )
                )
                continue
            role = role_for_function(person.function, known_roles)
            expiry = (
                f" Provision WITH an expiry of {person.expires_on} — the roster marks this "
                f"a temporary placement and the client asked not to rely on someone "
                f"remembering."
                if person.expires_on
                else ""
            )
            findings.append(
                Finding(
                    outcome=NEEDS_ACCOUNT if role else UNMAPPED_FUNCTION,
                    subject=person.name,
                    detail=(
                        f"{person.function}, {person.department}, {person.clinic} — "
                        + (
                            f"active staff with no account. Needs one as '{role}'.{expiry}"
                            if role
                            else f'function "{person.function}" maps to no role.'
                        )
                    ),
                    proposed_role=role,
                    roster_name=person.name,
                )
            )
    return findings


def cross_check_client_roles(findings, roster):
    """Flag where the mapping and the CLIENT disagree about a role.

    The client's roster carries their own `proposed_role` per person. It is
    deliberately NOT used as the answer — the mapping derives its own from
    function, validated against config/roles.yaml. Comparing the two catches
    both failure directions in one pass: a FUNCTION_TO_ROLE entry that has
    drifted from what the client actually wants, and a typo or stale row on
    the client's side.

    Silence here is meaningful: it means every role this report proposes is one
    the client already wrote down, which is most of what a sign-off is for.
    """
    by_name = {normalise_name(r.name): r for r in roster if r.client_proposed_role}
    extra = []
    for f in findings:
        if not f.proposed_role:
            continue
        # Prefer the roster person the match already established. Falling back
        # to `subject` covers roster-side findings, whose subject IS the name.
        person = by_name.get(normalise_name(f.roster_name or f.subject))
        if person is None:
            continue
        if person.client_proposed_role != f.proposed_role:
            extra.append(
                Finding(
                    outcome=ROLE_DISAGREEMENT,
                    subject=f.subject,
                    detail=(
                        f"this report proposes '{f.proposed_role}' from the function "
                        f'"{person.function}", but the client\'s roster proposes '
                        f"'{person.client_proposed_role}'. Resolve before sign-off — "
                        f"one of the two is wrong and the report must not pick a side."
                    ),
                )
            )
    return findings + extra


def format_report(findings):
    order = [MIGRATE, ROLE_DISAGREEMENT, DECIDE_NO_OWNER, DISABLE_DEPARTED,
             DEPARTED_CHECKED, UNMAPPED_FUNCTION, HOLD_ON_LEAVE, NEEDS_ACCOUNT,
             UNKNOWN_STATUS]
    titles = {
        MIGRATE: "MIGRATE — ready, one person, one account, mapped function",
        DECIDE_NO_OWNER: "DECIDE — no identified owner (disable, or split into named accounts)",
        DISABLE_DEPARTED: "DISABLE — the person has left",
        UNMAPPED_FUNCTION: "DENY BY DEFAULT — function maps to no role",
        HOLD_ON_LEAVE: "OPEN QUESTION — staff on leave, no rule set",
        NEEDS_ACCOUNT: "NEEDS AN ACCOUNT — active staff with none",
        UNKNOWN_STATUS: "UNRECOGNISED STATUS — cannot be actioned",
        ROLE_DISAGREEMENT: "DISAGREEMENT — the client proposed a different role",
        DEPARTED_CHECKED: "DEPARTURES CHECKED — left the network, no account found",
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


_SEED_USER_ROW = re.compile(
    r"\(\s*\d+\s*,\s*'([^']*)'\s*,\s*'[^']*'\s*,\s*'([^']*)'\s*,\s*'([^']*)'"
)


def read_accounts_from_seed(path):
    """Read accounts from db/seed/seed.sql instead of a live database.

    This is a training simulation and the committed seed is the account set the
    exercise runs against, so the report must not require `make up` to produce.
    Parses the users INSERT only: (id, username, password_hash, full_name, role).
    is_active is not in that INSERT — it defaults TRUE in the schema, so seeded
    accounts are active.
    """
    text_ = pathlib.Path(path).read_text()
    start = text_.find("INSERT INTO users")
    if start < 0:
        return []
    block = text_[start : text_.find(";", start)]
    return [
        Account(username=u, full_name=fn, role=role, is_active=True)
        for u, fn, role in _SEED_USER_ROW.findall(block)
    ]


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
    seed_path = os.path.join(here, "..", "..", "seed", "seed.sql")
    source = "the live database"
    try:
        accounts = _load_accounts_from_db()
    except Exception as e:  # never print the exception: it can carry a DSN
        print(
            f"database unreachable ({type(e).__name__}) — reading the committed seed instead.",
            file=sys.stderr,
        )
        accounts = read_accounts_from_seed(seed_path)
        source = "db/seed/seed.sql (no live database)"
    if not accounts:
        print("no accounts found — nothing to map.", file=sys.stderr)
        return 2
    try:
        report = format_report(cross_check_client_roles(build_report(roster, accounts), roster))
    except RolesConfigUnreadable as exc:
        print(f"REFUSING TO REPORT — {exc}", file=sys.stderr)
        return 2
    print(report)
    print(f"\nRoster: {os.path.relpath(roster_path)}   Accounts: {source}")
    print(
        "Training-simulation dataset — the people are fictional by design. "
        "This is the intended basis for review and sign-off."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
