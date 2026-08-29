"""Production-mode boot guard (W10 Final Stage 1, sub-slice 3).

Local/dev/test compose has always run with demo seed data, MFA off, and
simulated payer calls — all correct there. Nothing previously stopped the
same defaults from reaching a deployment with ENVIRONMENT=production set.
This is checked once at gateway startup (see app.py's lifespan) and refuses
to start rather than serve traffic on an unsafe posture; it reports what is
wrong and does not guess at a fix — no roster ownership is invented, and no
shared account is silently enrolled into MFA.

Only runs when settings.environment == "production" — every other mode
(the default, "development") is unaffected.
"""
import os

from sqlalchemy import select

import mfa_config
from models import User

# The deterministic salt format db/seed/generate_seed.py stamps into every
# demo account's password hash (`riverbend{NN}saltval0` / `riverbendp{id}saltval`).
# A hash still carrying it means a seeded demo credential, not a real one.
_DEMO_SALT_MARKER = "$riverbend"

_PLACEHOLDER_BEDROCK_MODEL_IDS = ("", "changeme")
_PLACEHOLDER_PAYER_HOSTS = ("edi.example.com",)


def _payer_mode_problem(settings) -> "str | None":
    mode = settings.payer_integration_mode
    if mode != "live":
        return f"PAYER_INTEGRATION_MODE is {mode!r}, not 'live'"
    if not settings.payer_api_key:
        return "PAYER_INTEGRATION_MODE=live but PAYER_API_KEY is not set"
    return None


def check(db, settings) -> list[str]:
    """Returns a list of human-readable problems; empty means safe to start."""
    problems: list[str] = []

    if mfa_config.effective_mode() == "off":
        problems.append("MFA rollout mode (config/mfa.yaml) is 'off'")

    payer_problem = _payer_mode_problem(settings)
    if payer_problem:
        problems.append(payer_problem)

    model_id = os.getenv("BEDROCK_MODEL_ID", "")
    if model_id in _PLACEHOLDER_BEDROCK_MODEL_IDS:
        problems.append(f"BEDROCK_MODEL_ID is unset or the placeholder value {model_id!r}")

    demo_accounts = db.execute(
        select(User.username).where(
            User.password_hash.like(f"%{_DEMO_SALT_MARKER}%"), User.is_active.is_(True)
        )
    ).scalars().all()
    if demo_accounts:
        problems.append(
            f"{len(demo_accounts)} account(s) still carry a known demo seed credential "
            f"(e.g. {demo_accounts[0]!r})"
        )

    legacy_staff = db.execute(
        select(User.username).where(User.role == "staff", User.is_active.is_(True))
    ).scalars().all()
    if legacy_staff:
        problems.append(
            f"{len(legacy_staff)} active account(s) are still on the deprecated 'staff' role "
            f"(e.g. {legacy_staff[0]!r}) — migrate via db/migrations/scripts/roster_migrate.py"
        )

    if mfa_config.effective_mode() != "off":
        unclassified_mfa = db.execute(
            select(User.username).where(
                User.is_active.is_(True), User.mfa_shared_account.is_(True)
            )
        ).scalars().all()
        if unclassified_mfa:
            problems.append(
                f"{len(unclassified_mfa)} active account(s) are still mfa_shared_account=TRUE "
                f"(e.g. {unclassified_mfa[0]!r}) — not individually classified for MFA"
            )

    return problems
