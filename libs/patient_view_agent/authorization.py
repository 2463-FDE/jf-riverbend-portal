"""Deterministic, fail-closed authorization boundary for the patient-view graph.

This is the first operation in the whole flow. `authorize()` either returns an
`AuthorizedScope` (the only key that unlocks retrieval) or RAISES
`AuthorizationDenied` — it never returns a partial/None result that a caller
could accidentally proceed past. Raising is what guarantees the plan's
invariant "unauthorized requests execute zero repository/graph reads": the
repository and graph reader are only ever constructed *after* a scope exists.

`FakePolicyAuthorization` is a deterministic fixture policy for tests/demo. It
is explicitly NOT the real authorization model:

- It denies by default (an unknown actor, or any actor/patient pair not in an
  explicit grant, is refused).
- It does NOT infer access from the current flat `staff` role (which has no
  per-patient concept) and does NOT read a `session.patient_id` (which does not
  exist in this codebase — see docs/analysis/RIV-201-patient-records-IDOR.md).
- It never consults model output.

PHI-safe logging: only the correlation id, outcome, and coarse
action/purpose/reason enums are logged — never the actor id, patient id, name,
or any clinical content. A real per-patient *access trail* (Week 10) would
record actor+patient in a secured, append-only store; that is a separate
concern from these operational logs and is intentionally not done here.
"""
from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from typing import Iterable, Mapping, Optional

from libs.safe_logging import get_safe_logger

from .contracts import (
    Action,
    AuthorizationRequest,
    AuthorizedScope,
    Denial,
    DenialReason,
    Purpose,
)

log = get_safe_logger(__name__)

_DEFAULT_ACTIONS = frozenset({Action.VIEW_PATIENT_CHART})
_DEFAULT_PURPOSES = frozenset({Purpose.TREATMENT})


class AuthorizationDenied(Exception):
    """Raised on any DENY decision. The message contains only the coarse reason
    enum — no actor id, patient id, name, or free text."""

    def __init__(self, denial: Denial):
        self.denial = denial
        super().__init__(f"authorization denied: {denial.reason.value}")


class AuthorizationPort(ABC):
    @abstractmethod
    def authorize(self, request: AuthorizationRequest) -> AuthorizedScope:
        """Return an AuthorizedScope on ALLOW, or raise AuthorizationDenied.

        Must be deterministic and must never perform or trigger any retrieval —
        it decides access, it does not read data.
        """
        raise NotImplementedError


class FakePolicyAuthorization(AuthorizationPort):
    """Deny-by-default fixture policy driven by an explicit grant table.

    `grants` maps an actor id to the set of patient ids that actor may access.
    An actor absent from the table, or a patient absent from that actor's set,
    is denied. Action/purpose must also be in the allow-lists.
    """

    def __init__(
        self,
        grants: Mapping[str, Iterable[int]],
        *,
        allowed_actions: Iterable[Action] = _DEFAULT_ACTIONS,
        allowed_purposes: Iterable[Purpose] = _DEFAULT_PURPOSES,
        id_factory=lambda: uuid.uuid4().hex,
    ):
        # Copy into immutable-ish frozensets so a caller can't mutate policy
        # after construction.
        self._grants = {actor: frozenset(pids) for actor, pids in grants.items()}
        self._allowed_actions = frozenset(allowed_actions)
        self._allowed_purposes = frozenset(allowed_purposes)
        self._id_factory = id_factory

    def authorize(self, request: AuthorizationRequest) -> AuthorizedScope:
        cid = request.correlation_id or self._id_factory()

        allowed_patients: Optional[frozenset] = self._grants.get(request.actor_id)
        if allowed_patients is None:
            self._deny(DenialReason.UNKNOWN_ACTOR, cid)
        if request.action not in self._allowed_actions:
            self._deny(DenialReason.ACTION_NOT_PERMITTED, cid)
        if request.purpose not in self._allowed_purposes:
            self._deny(DenialReason.PURPOSE_NOT_PERMITTED, cid)
        if request.patient_id not in allowed_patients:
            self._deny(DenialReason.NOT_AUTHORIZED, cid)

        log.info(
            "patient_view authorize (outcome=allow, action=%s, purpose=%s, correlation_id=%s)",
            request.action.value,
            request.purpose.value,
            cid,
        )
        return AuthorizedScope(
            actor_id=request.actor_id,
            patient_id=request.patient_id,
            action=request.action,
            purpose=request.purpose,
            correlation_id=cid,
        )

    def _deny(self, reason: DenialReason, correlation_id: str) -> None:
        log.warning(
            "patient_view authorize (outcome=deny, reason=%s, correlation_id=%s)",
            reason.value,
            correlation_id,
        )
        raise AuthorizationDenied(Denial(reason=reason, correlation_id=correlation_id))
