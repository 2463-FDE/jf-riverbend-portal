"""W10 Final 2 Stage 1 — gateway-side authorization for Release of
Information routes.

Every ROI proxy route in app.py used to forward the caller's JSON body
straight to roi-service with only a role-permission check
(`require_permission("roi.write")`/`"disclosures.read"`) — no check that
the calling staff member holds any relationship to the SPECIFIC patient a
request/authorization concerns. A caller holding `roi.write` (every
`roi_clerk` and legacy `staff` account) could request, review, or fulfill a
release for a patient they have never been granted access to.

Mirrors visit_authorization.py's own reasoning exactly (same module
docstring rationale): this stays local to the gateway and reuses its
`has_active_grant`/`parse_user_id`, rather than pulling in
records-service's `libs.patient_view_agent`-coupled `AuthorizationPort`
machinery, which is more ceremony than a single boolean gate needs.

`request_id`/`authorization_id` arrive in these routes' URLs, not their
JSON bodies — resolving which patient each belongs to needs one extra
lookup against the gateway's own minimal read-only mirror of roi-service's
tables (see models.py's RoiRequest/RoiAuthorization) before the grant check
can run. A row that doesn't exist and a row that exists but isn't granted
must return the SAME denial — see has_active_grant's own no-existence-oracle
note in visit_authorization.py; the functions below return None for "no
patient to check against" in both cases, and app.py's callers treat that
identically to "not granted".
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from models import RoiAuthorization, RoiDisclosureRestriction, RoiRequest


def roi_request_patient_id(db: Session, *, request_id: int) -> int | None:
    """The patient_id a roi_requests row belongs to, or None if no such row
    exists — never distinguished from "exists but no grant" by any caller."""
    req = db.get(RoiRequest, request_id)
    return req.patient_id if req is not None else None


def roi_authorization_patient_id(db: Session, *, authorization_id: int) -> int | None:
    """The patient_id a roi_authorizations row belongs to, or None if no
    such row exists — same no-existence-oracle rule as roi_request_patient_id."""
    auth = db.get(RoiAuthorization, authorization_id)
    return auth.patient_id if auth is not None else None


def roi_restriction_patient_id(db: Session, *, restriction_id: int) -> int | None:
    """The patient_id a roi_disclosure_restrictions row belongs to, or None
    if no such row exists — review fix ROI-RESTRICT-GRANT, same
    no-existence-oracle rule as the two lookups above."""
    restriction = db.get(RoiDisclosureRestriction, restriction_id)
    return restriction.patient_id if restriction is not None else None
