"""Stage 2 (feature-readiness) — gateway-side authorization for the
eligibility chat's `POST /visits/{visit_id}/messages` route.

There is no `visits` table anywhere in this system. Before this stage,
`visit_id` was just a caller-supplied string — services/eligibility-service's
Redis-backed `VisitContext` is keyed on it with no server-side existence
check at all, and the same request body let the caller supply an arbitrary
`patient_id`/`insurance_id` that got written straight into that memory (see
`bind_visit_context` in services/eligibility-service/agent_wiring.py). An
authenticated-but-unauthorized staff member could bind ANY visit_id to ANY
patient_id and have the assistant discuss (or expose an eligibility check
for) a patient they have no grant for.

`visit_id` is now REQUIRED to be a real `appointments.id` (see app.py's
`proxy_visit_message`, which drops the request body's own `patient_id`/
`insurance_id` fields entirely — see also `docs/runbook.md`). This module
verifies the session's user_id holds an active, non-expired
`patient_access_grants` row for that appointment's patient before the
gateway will proxy anything downstream, and resolves the patient's insurance
member_id itself — nothing about WHICH patient or WHICH insurance a chat
turn is scoped to is ever taken from the request body.

Mirrors `services/records-service/patient_access_gate.py`'s grant-lookup
shape (same table, same active-grant filter, same active-user join, same
no-existence-oracle rule: a denial here never distinguishes "no such
appointment" from "appointment exists but this actor has no grant for its
patient") but stays local to the gateway rather than reusing that module's
`libs.patient_view_agent`-coupled `AuthorizationPort` machinery, which isn't
imported here (adr/0001 — no shared service library) and is more machinery
than a single boolean gate needs.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from models import Appointment, InsuranceCoverage, PatientAccessGrant, User


def parse_user_id(actor_id: Optional[str]) -> Optional[int]:
    """The session's `user_id` (a string — see security.py::create_session)
    is the authorization principal. Mirrors
    records-service/patient_access_gate.py::parse_user_id exactly: anything
    non-numeric authorizes nothing."""
    try:
        return int(actor_id) if actor_id not in (None, "") else None
    except (ValueError, TypeError):
        return None


def find_authorized_appointment(db: Session, *, user_id: int, appointment_id: int) -> Optional[Appointment]:
    """Returns the `Appointment` only if `user_id` currently holds an active,
    non-expired grant for its patient — otherwise `None`, with NO
    distinction between "no such appointment", "appointment exists but no
    grant", or "grant exists but the user is disabled" (same
    no-existence-oracle rule `SqlPatientAccessGate.authorize` follows). Lets
    `SQLAlchemyError` propagate so a grant-store outage surfaces as the
    caller's 503, not a silent, indistinguishable-from-real denial.
    """
    return (
        db.execute(
            select(Appointment)
            .join(PatientAccessGrant, PatientAccessGrant.patient_id == Appointment.patient_id)
            .join(User, User.id == PatientAccessGrant.user_id)
            .where(
                Appointment.id == appointment_id,
                PatientAccessGrant.user_id == user_id,
                PatientAccessGrant.revoked_at.is_(None),
                (PatientAccessGrant.expires_at.is_(None)) | (PatientAccessGrant.expires_at > func.now()),
                User.is_active.is_(True),
            )
        )
        .scalars()
        .first()
    )


def has_active_grant(db: Session, *, user_id: int, patient_id: int) -> bool:
    """Whether `user_id` currently holds an active, non-expired grant for
    `patient_id`. Same shape as `find_authorized_appointment` above (same
    table, same active-grant filter, same active-user join) but answers a
    plain boolean rather than resolving a row — used where the caller already
    has the patient_id and just needs to know they may act on it (e.g. issuing
    a portal invitation), not where it needs deriving from something else.
    Uses the module-level Appointment/PatientAccessGrant/User imports above,
    not a local re-import: a deferred `from models import ...` inside a
    function body resolves `models` from sys.modules at CALL time rather than
    at this module's own load time, which under this repo's test loader
    (conftest.load_module, no shared service package — adr/0001) can silently
    bind to a DIFFERENT service's same-named `models` module left cached from
    an earlier test file in the same pytest session."""
    return (
        db.execute(
            select(PatientAccessGrant.id)
            .join(User, User.id == PatientAccessGrant.user_id)
            .where(
                PatientAccessGrant.user_id == user_id,
                PatientAccessGrant.patient_id == patient_id,
                PatientAccessGrant.revoked_at.is_(None),
                (PatientAccessGrant.expires_at.is_(None)) | (PatientAccessGrant.expires_at > func.now()),
                User.is_active.is_(True),
            )
        )
        .scalars()
        .first()
    ) is not None


def latest_insurance_coverage(db: Session, *, patient_id: int) -> Optional[InsuranceCoverage]:
    """The patient's most recently created coverage row, or `None` — no
    member_id filter, unlike latest_insurance_member_id below. Backs the
    stored coverage-on-file snapshot the eligibility chat now also derives
    server-side (w-9-2-planner P1a, app.py::proxy_visit_message): a coverage
    row can carry a real payer/plan/status worth showing even with no
    member id on file (verification just can't run against it — mirrors the
    Coverage & Eligibility page's own has_member_id-gated display)."""
    return (
        db.execute(
            select(InsuranceCoverage)
            .where(InsuranceCoverage.patient_id == patient_id)
            .order_by(InsuranceCoverage.id.desc())
        )
        .scalars()
        .first()
    )


def latest_insurance_member_id(db: Session, *, patient_id: int) -> Optional[str]:
    """The patient's most recently recorded insurance member_id, or `None` if
    they have none on file. This — never a caller-supplied value — is the
    only source of `insurance_id` the visit-chat path uses; see
    `app.py::proxy_visit_message`."""
    coverage = (
        db.execute(
            select(InsuranceCoverage)
            .where(InsuranceCoverage.patient_id == patient_id, InsuranceCoverage.member_id.isnot(None))
            .order_by(InsuranceCoverage.id.desc())
        )
        .scalars()
        .first()
    )
    return coverage.member_id if coverage else None
