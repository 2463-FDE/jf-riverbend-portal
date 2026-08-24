"""Role-derived retrieval scope (w-9-2-planner P3) — the application, never
the model, fixes audiences/workflows before the navigator's tool is ever
callable (vector-rag.md). Derived from `config/roles.yaml`'s real,
client-signed role names; an unrecognized role gets an empty scope, which
`PolicyRetriever.retrieve()` already turns into zero results rather than an
error — fail closed, not fail open, exactly like `roles_config.permissions_for`
does for the signed permission matrix itself.

This is a fixed lookup table, not a second permission system: it decides
which SYNTHETIC POLICY TEXT a role may see, never patient data, and adding a
new manifest audience/workflow here does not grant any application
permission — that stays roles.yaml's job alone.
"""
from libs.policy_corpus import RetrievalScope

# role name -> (audiences, workflows). Role names match config/roles.yaml;
# most audiences match those names directly (docs/RagDocs/manifest.json).
_ROLE_SCOPES = {
    "patient": (("patient",), ("patient_summary", "records_access", "secure_messaging", "intake_consent")),
    "clinician": (
        ("clinician",),
        ("patient_summary", "summary_review", "rag_governance", "records_access", "secure_messaging"),
    ),
    "nursing_ma": (
        ("nursing_ma",),
        ("patient_summary", "summary_review", "records_access", "secure_messaging", "intake_consent"),
    ),
    "front_desk": (("front_desk",), ("scheduling", "records_access", "intake_consent", "coverage_eligibility")),
    "scheduler": (("scheduler",), ("scheduling",)),
    "lab": (("lab",), ("records_access",)),
    "billing": (("billing",), ("coverage_eligibility",)),
    "roi_clerk": (("roi_clerk",), ("roi", "records_access")),
}

_EMPTY_SCOPE = ((), ())


def scope_for_role(role: str) -> RetrievalScope:
    """The deprecated legacy `staff` role and any other unrecognized role
    (it_admin, management, or a typo) get an empty scope — see module
    docstring for why that fails closed instead of guessing a broad one."""
    audiences, workflows = _ROLE_SCOPES.get(role, _EMPTY_SCOPE)
    return RetrievalScope(audiences=audiences, workflows=workflows)
