"""No secret-bearing file is tracked, and no tracked file carries a secret.

`.env` was tracked until 2026-08-20 with a real DB_PASSWORD, a `pyr_live_`-
prefixed PAYER_API_KEY and a SESSION_SECRET in it. Untracking it is not the
whole fix and this suite is deliberately narrow about what it proves: the
values remain in git history, because the decision was to ROTATE rather than
rewrite history. What these tests prevent is a REGRESSION — a future change
re-adding `.env`, or putting a live-looking secret in a tracked file.

They are cheap, they run without infrastructure, and they would have caught the
original mistake. That is the whole argument for them.
"""
import pathlib
import re
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]


def _tracked_files():
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout
    return [f for f in out.split("\0") if f]


def test_git_is_available_so_the_other_tests_mean_something():
    # Without this, a git failure would make every assertion below vacuously
    # pass — the suite would go green precisely when it cannot check anything.
    assert _tracked_files(), "git ls-files returned nothing; the checks below prove nothing"


def test_dotenv_is_not_tracked():
    tracked = _tracked_files()
    offenders = [f for f in tracked if f == ".env" or f.startswith(".env.")]
    offenders = [f for f in offenders if f != ".env.example"]
    assert not offenders, (
        f"{offenders} is tracked. Real env files carry secrets; only .env.example "
        f"belongs in git. Untrack with `git rm --cached` and keep the .gitignore rule."
    )


def test_gitignore_covers_real_env_files():
    """Parse the rules, don't substring-match the file.

    Review finding C2-GITIGNORE-TEST-WEAK: a substring check passes on the
    explanatory COMMENT above the rules, so deleting the actual `.env` line
    would leave this test green. The comment exists precisely because the rule
    is non-obvious, which makes that failure mode likely rather than theoretical.
    """
    rules = {
        line.strip()
        for line in (REPO / ".gitignore").read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    }

    assert ".env" in rules, ".gitignore must exclude .env or it will be re-added"
    assert ".env.*" in rules, "variant env files (.env.local, .env.production) too"
    assert "!.env.example" in rules, (
        "the .env.example template must stay tracked — it is the only documentation "
        "of which variables a deployment needs"
    )


def test_every_env_file_reference_is_optional():
    """A clean clone has no `.env`, and a required `env_file` is a HARD compose
    error that fires BEFORE any `${VAR:?}` guard — so the operator gets
    "env file not found" instead of the message naming the variable to set.

    Review finding C2-CI-MISSING-ENVFILE, confirmed by running
    `docker compose config` in a fresh worktree: it failed on all seven
    services. Marking each optional keeps the ${VAR:?} guards as the real
    fail-fast, and `.env` still supplies interpolation when it exists.
    """
    compose = (REPO / "docker-compose.yml").read_text()

    assert "env_file: .env" not in compose, (
        "short-form `env_file: .env` is required-by-default and breaks a clean "
        "clone. Use the long form with `required: false`."
    )
    # One `- path: .env` / `required: false` pair per service that reads .env.
    assert compose.count("- path: .env") == compose.count("required: false"), (
        "every env_file path must carry `required: false`"
    )


# Shapes that indicate a real credential rather than a placeholder or a name.
# Deliberately narrow: broad entropy matching on a repo full of password HASHES
# and base64 test fixtures produces noise, and a noisy guard gets deleted.
_SECRET_PATTERNS = (
    ("live payer API key", re.compile(r"pyr_live_[A-Za-z0-9]{16,}")),
    ("AWS access key id", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("GitHub token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}")),
)

# The seeded demo password and the local test DSN password are intentional,
# published, and worthless — they exist so the demo and the integration suite
# can run. Excluding them by name keeps the guard honest about what it covers.
_ALLOWED = {"db/seed/seed.sql", "db/seed/generate_seed.py", ".env.example"}


@pytest.mark.parametrize("label,pattern", _SECRET_PATTERNS, ids=[p[0] for p in _SECRET_PATTERNS])
def test_no_tracked_file_carries_a_live_secret(label, pattern):
    hits = []
    for rel in _tracked_files():
        if rel in _ALLOWED:
            continue
        path = REPO / rel
        try:
            text = path.read_text(errors="ignore")
        except (OSError, IsADirectoryError):
            continue
        if pattern.search(text):
            hits.append(rel)
    assert not hits, (
        f"{label} found in tracked file(s): {hits}. Rotate the value, remove it from "
        f"the file, and read it from the environment instead."
    )


def test_compose_has_no_guessable_password_default():
    """`${DB_PASSWORD:-changeme}` boots a working stack on a predictable
    credential and reports nothing. A missing value must stop compose instead,
    the same way INTERNAL_SERVICE_TOKEN already does."""
    # Comment lines are stripped first: the comment explaining WHY the default
    # was removed necessarily names it, and a guard a comment can break is a
    # guard that gets deleted rather than fixed.
    lines = [
        ln for ln in (REPO / "docker-compose.yml").read_text().splitlines()
        if not ln.lstrip().startswith("#")
    ]
    compose = "\n".join(lines)
    assert ":-changeme" not in compose, (
        "a guessable default password is worse than a failed start — use the "
        "${VAR:?message} form so a missing value stops compose"
    )


def test_ci_supplies_every_variable_compose_requires():
    """A `${VAR:?}` in compose is a build-time dependency, and CI must satisfy it.

    This is the test that would have caught the C2 regression. `.env` was
    committed, so compose interpolation quietly resolved DB_PASSWORD from the
    tracked file and CI never had to supply it. Untracking `.env` broke
    docker-build immediately — correct behaviour, discovered late. Any future
    `${VAR:?}` added to compose now has to be wired into CI in the same change.
    """
    compose = (REPO / "docker-compose.yml").read_text()
    required = set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*):\?", compose))
    required.discard("VAR")  # the placeholder used in explanatory comments

    workflow = (REPO / ".github" / "workflows" / "ci.yml").read_text()
    missing = sorted(v for v in required if v not in workflow)

    assert not missing, (
        f"docker-compose.yml requires {missing} but .github/workflows/ci.yml never "
        f"mentions them, so docker-build will fail on interpolation. Add each to the "
        f"build step's env and its throwaway-value fallback."
    )
