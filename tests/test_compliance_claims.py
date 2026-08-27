"""No file claims a protection the implementation does not provide.

`README.md:1` asserted "All PHI is encrypted and the system is fully HIPAA
compliant" from the initial scaffold until 2026-08-20 — the first line anyone
read, contradicted by `adr/0002`, by `ARCHITECTURE.md` §7, and by the plaintext
`ssn`/`dob`/`notes` columns three files away. `db/schema.sql:2` separately
claimed RDS volume encryption in a deployment that has no RDS.

Both survived a documentation-correction pass in Week 7 that was scoped to MFA
wording, and neither appeared on any deferred list. They fell between the two.
That is the specific failure this suite exists to prevent recurring: not a
missing control, a false statement about one.

Encryption at rest is an *addressable* specification (45 CFR 164.312(a)(2)(iv)),
so choosing not to implement it is defensible when documented — see adr/0008.
Claiming it is implemented is not defensible under any reading.
"""
import pathlib
import re
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]

# Affirmative claims only. Each was literally present in the repository.
_FORBIDDEN = (
    ("PHI-wide encryption claim", re.compile(r"All PHI is encrypted", re.I)),
    ("blanket compliance claim", re.compile(r"fully HIPAA[- ]compliant", re.I)),
    ("patient-data encryption claim", re.compile(r"All patient data is encrypted", re.I)),
    ("unevidenced RDS claim", re.compile(r"RDS volume encryption", re.I)),
    # Review AUD12-stale-architecture-claim: matching exact phrases missed
    # ARCHITECTURE.md's "Encryption is handled at the storage layer (volume
    # encryption) + TLS in transit", which told engineers the opposite of the
    # corrected README. These match the CLAIM SHAPE — a present-tense assertion
    # that the control exists — rather than one file's wording.
    ("storage-layer encryption claim",
     re.compile(r"(encryption\s+is\s+handled|encrypted\s+at\s+(the\s+)?(storage|disk|volume)"
                r"|(storage|disk|volume)[- ]level\s+encryption)", re.I)),
    ("TLS-in-transit claim",
     re.compile(r"TLS\s+in\s+transit|encrypted\s+in\s+transit", re.I)),
)

# ADRs are the historical record and must be free to quote what was wrong, or
# the decision loses its context. Everything else is current-tense description
# and is held to the current truth.
_EXEMPT_PREFIXES = ("adr/", "tests/test_compliance_claims.py", "docs/analysis/")

_TEXT_SUFFIXES = {".md", ".sql", ".py", ".ts", ".tsx", ".yaml", ".yml", ".txt", ".example"}


def _tracked_text_files():
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout
    files = [f for f in out.split("\0") if f]
    assert files, "git ls-files returned nothing; this suite would pass vacuously"
    return [
        f for f in files
        if pathlib.Path(f).suffix in _TEXT_SUFFIXES
        and not f.startswith(_EXEMPT_PREFIXES)
    ]


# A line that DENIES the control is the fix, not the defect — and the sentences
# doing the denying necessarily name the thing they deny ("Nothing is encrypted
# at the storage layer"). Without this the guard flags its own corrections,
# which is how a test gets deleted rather than fixed.
#
# The limit is worth stating: this is a heuristic over one line, so a claim on a
# line that happens to contain "not" elsewhere would slip through. It is a
# regression guard for a known, specific failure, not a proof of absence.
_NEGATION = re.compile(r"\b(no|not|never|nothing|none|false|without|un-?encrypted)\b", re.I)


@pytest.mark.parametrize("label,pattern", _FORBIDDEN, ids=[f[0] for f in _FORBIDDEN])
def test_no_file_claims_protection_that_does_not_exist(label, pattern):
    hits = []
    for rel in _tracked_text_files():
        try:
            text = (REPO / rel).read_text(errors="ignore")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line) and not _NEGATION.search(line):
                hits.append(f"{rel}:{i}")
    assert not hits, (
        f"{label} found at {hits}. PHI is not encrypted at rest and the system is not "
        f"HIPAA compliant — see adr/0008. State the actual posture instead."
    )


def test_the_readme_states_the_actual_posture():
    """A negative test alone would pass if the Compliance section were simply
    deleted. The claim has to be replaced by the truth, not removed.

    w8-planner-2 P2 (adr/0012), corrected again after review (PR #99 round
    2): "PHI columns are encrypted" overclaimed completeness — ssn/dob/notes
    and (adr/0012 follow-up) agent_draft_provenance.generated_text are
    application-layer encrypted, but most PHI is NOT, and the README must
    say so precisely — "selected PHI fields are application-encrypted",
    never a bare "PHI columns/is encrypted" that implies every column. What
    must still hold: the README names specific fields as encrypted, names
    at least one specific field that is NOT (records.title/body is the
    running example throughout this codebase's docs), still denies
    disk/volume-level encryption and KMS-backed custody (both genuinely
    absent), and still points to both the original risk decision (adr/0008)
    and the design that closed part of the gap (adr/0012)."""
    readme = (REPO / "README.md").read_text()
    assert "selected phi fields are application-encrypted" in readme.lower(), (
        "README must say 'selected PHI fields are application-encrypted', not a bare "
        "'PHI columns are encrypted' that implies every column"
    )
    assert "records.title" in readme, (
        "README must name at least one specific still-plaintext PHI surface (records.title/body)"
    )
    assert re.search(r"no\*?\*?\s+disk/volume-level encryption", readme), (
        "README must still deny disk/volume-level encryption — that part of the gap is real"
    )
    assert "no kms-backed key custody" in readme.lower(), (
        "README must still deny KMS-backed key custody for the fields that ARE encrypted"
    )
    assert "adr/0008" in readme, "README must point to the recorded risk decision"
    assert "adr/0012" in readme, "README must point to the design that closed part of it"


def test_the_guard_still_catches_an_affirmative_claim():
    """The negation allowance must not neuter the guard.

    Asserted directly because the allowance is the risky part: if it were too
    broad, every claim would slip through and this suite would be decoration.
    """
    affirmative = "Encryption is handled at the storage layer (volume encryption)."
    denial = "Nothing is encrypted at the storage layer; PHI is plain text."

    pattern = dict((label, p) for label, p in _FORBIDDEN)["storage-layer encryption claim"]

    assert pattern.search(affirmative) and not _NEGATION.search(affirmative)
    assert pattern.search(denial) and _NEGATION.search(denial)


def test_architecture_doc_states_the_actual_posture():
    # It is the doc engineers read, and it claimed storage-layer encryption and
    # TLS in transit until 2026-08-20 — telling them the opposite of the README.
    # w8-planner-2 P2 (adr/0012): "no encryption anywhere" stopped being true —
    # see test_the_readme_states_the_actual_posture's identical note.
    arch = (REPO / "ARCHITECTURE.md").read_text()

    assert "nothing is encrypted at the storage layer" in arch.lower()
    assert "adr/0008" in arch
    assert "adr/0012" in arch
