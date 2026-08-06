"""Stage 3 — authenticated-staff access gate for the live `/patients/{id}/view`
route (services/records-service/app.py), replacing the Week 4 fixture
`FakePolicyAuthorization` (a hardcoded actor->patient grant table meant only
for tests/demo) for that one real route.

This is deliberately NOT patient-specific authorization. `config/roles.yaml`
gives every account the same flat `staff` role, and `users` has no
relationship to `patients` anywhere in the schema (no care-team table, no
per-patient assignment) — see docs/analysis/RIV-201-patient-records-IDOR.md
§6, which names exactly this gap: "a patient-ownership or care-team-
membership fact that a session can be checked against ... does not exist
today." Building that fact is out of scope for Stage 3; it was not asked for
and is not approved.

What this gate DOES provide, in place of the fixture:

  - A real, fail-closed check: an unknown/missing actor (no session reached
    this far) is DENIED, never silently allowed — `authorize()` still only
    ever returns via `AuthorizationPort`'s contract (ALLOW -> AuthorizedScope,
    DENY -> raise), so a denied request still performs zero repository/graph
    reads, exactly like `FakePolicyAuthorization`.
  - ALLOW for any other authenticated actor, for an allowed action/purpose —
    i.e. it enforces "you must be an authenticated staff member," which is
    the actual, current access model (config/roles.yaml), not a stronger
    claim than that.
  - A real correlation-id-bearing ALLOW/DENY log line and (via the /view
    route) a real audit_logs row — this route's accesses are now recorded,
    unlike the legacy endpoints below it in records-service/app.py.

This does NOT close RIV-201. `services/gateway/app.py`'s `proxy_records`/
`proxy_patient` and `services/records-service/app.py`'s
`get_patient_records`/`get_patient` remain exactly as IDOR-exploitable as
documented in docs/analysis/RIV-201-patient-records-IDOR.md — this gate only
guards the new `/patients/{id}/view` route.
"""
from __future__ import annotations

import uuid
from typing import Iterable

from .authorization import AuthorizationDenied, AuthorizationPort
from .contracts import (
    _SCOPE_ISSUER_TOKEN,
    Action,
    AuthorizationRequest,
    AuthorizedScope,
    Denial,
    DenialReason,
    Purpose,
)
from libs.safe_logging import get_safe_logger

log = get_safe_logger(__name__)

_DEFAULT_ACTIONS = frozenset({Action.VIEW_PATIENT_CHART})
_DEFAULT_PURPOSES = frozenset({Purpose.TREATMENT, Purpose.PAYMENT, Purpose.OPERATIONS})


class StaffAccessGate(AuthorizationPort):
    def __init__(
        self,
        *,
        allowed_actions: Iterable[Action] = _DEFAULT_ACTIONS,
        allowed_purposes: Iterable[Purpose] = _DEFAULT_PURPOSES,
        id_factory=lambda: uuid.uuid4().hex,
    ):
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

        log.info(
            "patient_view authorize (outcome=allow, gate=staff_access, action=%s, purpose=%s, correlation_id=%s)",
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
            "patient_view authorize (outcome=deny, gate=staff_access, reason=%s, correlation_id=%s)",
            reason.value,
            correlation_id,
        )
        raise AuthorizationDenied(Denial(reason=reason, correlation_id=correlation_id))
