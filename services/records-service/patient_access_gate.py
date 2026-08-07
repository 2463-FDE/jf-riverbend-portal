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

from models import PatientAccessGrant

log = get_safe_logger(__name__)

_DEFAULT_ACTIONS = frozenset({Action.VIEW_PATIENT_CHART})
_DEFAULT_PURPOSES = frozenset({Purpose.TREATMENT, Purpose.PAYMENT, Purpose.OPERATIONS})


def _active_grant_filter(username: str, patient_id: int):
    return (
        PatientAccessGrant.username == username,
        PatientAccessGrant.patient_id == patient_id,
        PatientAccessGrant.revoked_at.is_(None),
        (PatientAccessGrant.expires_at.is_(None)) | (PatientAccessGrant.expires_at > func.now()),
    )


def authorized_patient_ids(db: Session, username: str, patient_ids: Iterable[int]) -> set[int]:
    """Batch active-grant check for routes that authorize more than one
    candidate patient per request (e.g. records reconciliation checking
    several SSN-matched charts) — one query instead of one per candidate,
    using the same active-grant definition as SqlPatientAccessGate.authorize
    below. Callers MUST silently drop ids not in the returned set and never
    render any placeholder for them — see this module's docstring on why no
    patient-existence oracle is created here.

    Codex review (2026-08-07, PR #22 — medium): a DB failure here used to be
    swallowed into an empty set, so a real grant-store outage looked
    identical to "genuinely zero matches" — fail-closed for disclosure (no
    unauthorized data leaked) but fail-OPEN for correctness and
    observability (a clinician or monitor sees a normal "no candidates"
    result during a broken authorization dependency, not an error). Now
    propagates SQLAlchemyError instead of catching it, matching every other
    data read failure in this file and reconciliation.py — every existing
    caller (search_records, build_reconciliation_result) already wraps its
    call to this function in the same try/except SQLAlchemyError -> 503
    convention used everywhere else, so this requires no new exception type."""
    ids = {int(p) for p in patient_ids}
    if not username or not ids:
        return set()
    try:
        rows = (
            db.execute(
                select(PatientAccessGrant.patient_id).where(
                    PatientAccessGrant.username == username,
                    PatientAccessGrant.patient_id.in_(ids),
                    PatientAccessGrant.revoked_at.is_(None),
                    (PatientAccessGrant.expires_at.is_(None))
                    | (PatientAccessGrant.expires_at > func.now()),
                )
            )
            .scalars()
            .all()
        )
    except SQLAlchemyError:
        log.exception("patient_access_gate: batch grant lookup failed")
        raise
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

        if not request.actor_id:
            self._deny(DenialReason.UNKNOWN_ACTOR, cid)
        if request.action not in self._allowed_actions:
            self._deny(DenialReason.ACTION_NOT_PERMITTED, cid)
        if request.purpose not in self._allowed_purposes:
            self._deny(DenialReason.PURPOSE_NOT_PERMITTED, cid)

        try:
            granted = self._db.execute(
                select(PatientAccessGrant.id).where(
                    *_active_grant_filter(request.actor_id, request.patient_id)
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
