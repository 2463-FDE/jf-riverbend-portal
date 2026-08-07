"""Week 4 catch-up — real, database-backed `AuthorizationPort`.

Replaces `StaffAccessGate` (authenticated-staff-only, explicitly NOT
patient-specific — see `libs/patient_view_agent/staff_access_gate.py`) with a
genuine per-(actor, patient) check against `patient_access_grants`
(migration 014): the patient-ownership/care-team-membership fact
docs/analysis/RIV-201-patient-records-IDOR.md §6 identified as missing
("users has no relationship to patients").

Mirrors `patient_view_repository.py::SqlChartRepository` in shape: the
framework-neutral `AuthorizationPort` ABC and `AuthorizedScope` live in
`libs.patient_view_agent` (SQLAlchemy-free by design — see that package's
module docstring); this concrete, SQLAlchemy-coupled adapter lives here in
records-service, same as the repository does, per ADR 0001 (no shared
service library). Constructing an `AuthorizedScope` requires the package's
module-private issuer token — `staff_access_gate.py` reaches it via a
relative import from inside the package; this file does the equivalent
absolute import from outside it, for the same legitimate reason: it is a
second real `AuthorizationPort` implementation, not a forgery attempt (see
`contracts.py`'s own docstring: "a strong tripwire ... not a cryptographic
guarantee").

Grant activity is evaluated in SQL against the database's own clock
(`now()`), not fetched then filtered in Python, so it stays consistent with
`granted_at`/`revoked_at`/`expires_at` all being database-set timestamps.

No patient-existence oracle: this file only ever queries
`patient_access_grants`, never `patients` — a denial here is
indistinguishable between "patient_id doesn't exist" and "patient_id exists
but this actor has no active grant for it." Route layers still do their own
404 check, but only AFTER authorization succeeds (see app.py's
get_patient_view for the existing pattern, and get_patient/
get_patient_records below for the newly-added equivalent).
"""
from __future__ import annotations

import uuid
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from libs.patient_view_agent import (
    Action,
    AuthorizationDenied,
    AuthorizationPort,
    AuthorizationRequest,
    Purpose,
)
from libs.patient_view_agent.contracts import (
    _SCOPE_ISSUER_TOKEN,
    AuthorizedScope,
    Denial,
    DenialReason,
)
from libs.safe_logging import get_safe_logger

from models import PatientAccessGrant, User

log = get_safe_logger(__name__)

_DEFAULT_ACTIONS = frozenset({Action.VIEW_PATIENT_CHART})
_DEFAULT_PURPOSES = frozenset({Purpose.TREATMENT, Purpose.PAYMENT, Purpose.OPERATIONS})


def parse_user_id(actor_id: str | None) -> int | None:
    """The actor identity is the stable users.id, forwarded by the gateway as
    the X-Actor-Id string (PR #23 review round 2 — never username). Anything
    non-numeric is not a valid principal and authorizes nothing."""
    try:
        return int(actor_id) if actor_id not in (None, "") else None
    except (ValueError, TypeError):
        return None


def _active_grant_filter(user_id: int, patient_id: int):
    return (
        PatientAccessGrant.user_id == user_id,
        PatientAccessGrant.patient_id == patient_id,
        PatientAccessGrant.revoked_at.is_(None),
        (PatientAccessGrant.expires_at.is_(None)) | (PatientAccessGrant.expires_at > func.now()),
    )


def active_patient_ids_query(user_id: int):
    """A SELECT of the patient_ids this user currently has an active,
    non-expired grant for, joined to a still-active user. Embed it in a
    list/search query (`Patient.id.in_(...)` / `Record.patient_id.in_(...)`) so
    unauthorized rows are never loaded — authorization happens IN the SQL, not
    after the record bodies come back (PR #23 review round 2, finding 4). A
    database error on the combined query then surfaces as the caller's 503,
    not a silently empty 200."""
    return (
        select(PatientAccessGrant.patient_id)
        .join(User, User.id == PatientAccessGrant.user_id)
        .where(
            PatientAccessGrant.user_id == user_id,
            PatientAccessGrant.revoked_at.is_(None),
            (PatientAccessGrant.expires_at.is_(None)) | (PatientAccessGrant.expires_at > func.now()),
            User.is_active.is_(True),
        )
    )


def authorized_patient_ids(db: Session, actor_id: str, patient_ids: Iterable[int]) -> set[int]:
    """Batch active-grant check for routes that authorize more than one
    candidate patient per request (e.g. records reconciliation checking several
    SSN-matched charts). Callers MUST silently drop ids not in the returned set
    and never render any placeholder for them — see this module's docstring on
    why no patient-existence oracle is created here. Fails closed (empty set) on
    a lookup error; list_patients/search_records instead embed
    active_patient_ids_query so a DB error surfaces as 503, not a silent empty
    result."""
    ids = {int(p) for p in patient_ids}
    user_id = parse_user_id(actor_id)
    if user_id is None or not ids:
        return set()
    try:
        rows = (
            db.execute(
                select(PatientAccessGrant.patient_id)
                .join(User, User.id == PatientAccessGrant.user_id)
                .where(
                    PatientAccessGrant.user_id == user_id,
                    PatientAccessGrant.patient_id.in_(ids),
                    PatientAccessGrant.revoked_at.is_(None),
                    (PatientAccessGrant.expires_at.is_(None))
                    | (PatientAccessGrant.expires_at > func.now()),
                    User.is_active.is_(True),
                )
            )
            .scalars()
            .all()
        )
    except SQLAlchemyError:
        log.exception("patient_access_gate: batch grant lookup failed")
        return set()  # fail closed: treat a lookup failure as "no candidate authorized"
    return set(rows)


class SqlPatientAccessGate(AuthorizationPort):
    """Per-request instance — construct with the request-scoped `db: Session`,
    exactly like `SqlChartRepository(db)`."""

    def __init__(
        self,
        db: Session,
        *,
        allowed_actions: Iterable[Action] = _DEFAULT_ACTIONS,
        allowed_purposes: Iterable[Purpose] = _DEFAULT_PURPOSES,
        id_factory=lambda: uuid.uuid4().hex,
    ):
        self._db = db
        self._allowed_actions = frozenset(allowed_actions)
        self._allowed_purposes = frozenset(allowed_purposes)
        self._id_factory = id_factory

    def authorize(self, request: AuthorizationRequest) -> AuthorizedScope:
        cid = request.correlation_id or self._id_factory()

        user_id = parse_user_id(request.actor_id)
        if user_id is None:
            self._deny(DenialReason.UNKNOWN_ACTOR, cid)
        if request.action not in self._allowed_actions:
            self._deny(DenialReason.ACTION_NOT_PERMITTED, cid)
        if request.purpose not in self._allowed_purposes:
            self._deny(DenialReason.PURPOSE_NOT_PERMITTED, cid)

        try:
            granted = self._db.execute(
                select(PatientAccessGrant.id)
                .join(User, User.id == PatientAccessGrant.user_id)
                .where(
                    *_active_grant_filter(user_id, request.patient_id),
                    User.is_active.is_(True),
                )
            ).first()
        except SQLAlchemyError:
            log.exception("patient_access_gate: grant lookup failed (correlation_id=%s)", cid)
            self._deny(DenialReason.POLICY_ERROR, cid)
            return  # unreachable — _deny always raises; keeps type-checkers honest

        if granted is None:
            self._deny(DenialReason.NOT_AUTHORIZED, cid)

        log.info(
            "patient_view authorize (outcome=allow, gate=patient_access, action=%s, purpose=%s, correlation_id=%s)",
            request.action.value,
            request.purpose.value,
            cid,
        )
        return AuthorizedScope(
            issuer_token=_SCOPE_ISSUER_TOKEN,
            actor_id=request.actor_id,
            patient_id=request.patient_id,
            action=request.action,
            purpose=request.purpose,
            correlation_id=cid,
        )

    def _deny(self, reason: DenialReason, correlation_id: str) -> None:
        log.warning(
            "patient_view authorize (outcome=deny, gate=patient_access, reason=%s, correlation_id=%s)",
            reason.value,
            correlation_id,
        )
        raise AuthorizationDenied(Denial(reason=reason, correlation_id=correlation_id))
