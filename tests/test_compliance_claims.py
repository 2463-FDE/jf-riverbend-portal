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


@pytest.mark.parametrize("label,pattern", _FORBIDDEN, ids=[f[0] for f in _FORBIDDEN])
def test_no_file_claims_protection_that_does_not_exist(label, pattern):
    hits = []
    for rel in _tracked_text_files():
        try:
            text = (REPO / rel).read_text(errors="ignore")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                hits.append(f"{rel}:{i}")
    assert not hits, (
        f"{label} found at {hits}. PHI is not encrypted at rest and the system is not "
        f"HIPAA compliant — see adr/0008. State the actual posture instead."
    )


def test_the_readme_states_the_actual_posture():
    """A negative test alone would pass if the Compliance section were simply
    deleted. The claim has to be replaced by the truth, not removed."""
    readme = (REPO / "README.md").read_text()
    assert "not** encrypted at rest" in readme or "not encrypted at rest" in readme, (
        "README must state plainly that PHI is not encrypted at rest"
    )
    assert "adr/0008" in readme, "README must point to the recorded risk decision"
