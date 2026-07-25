"""Stage 3 seeded, deterministic demo entry point.

Not a production HTTP route: no FastAPI app, no service, no network, no live
model provider. Prints three fixed seeded scenarios so a reviewer can see the
bounded supervisor's allowed, denied, and escalated (missing-evidence)
outcomes directly from a shell, without touching the gateway or
records-service. This does NOT exercise or remediate the real RIV-201 IDOR
in `services/gateway/app.py` / `services/records-service/app.py` — see
`docs/analysis/RIV-201-patient-records-IDOR.md`.

Run: `python -m libs.patient_view_agent.demo`
"""
from __future__ import annotations

import json

from .authorization import AuthorizationDenied, FakePolicyAuthorization
from .contracts import Action, AuthorizationRequest, Purpose
from .repository import SeededChartRepository, seed_derived_sample
from .runtime import run_patient_view

_ACTOR = "demo-clinician"
# 5000 is granted but present in NO seed row — this demonstrates the
# missing-evidence -> escalation path without needing a second real seeded
# patient with a deliberately empty chart.
_GRANTS = {_ACTOR: {1042, 1043, 5000}}


def _run(label: str, patient_id: int, *, purpose: Purpose = Purpose.TREATMENT, allowed_purposes=None) -> None:
    repository = SeededChartRepository(*seed_derived_sample())
    authorizer = FakePolicyAuthorization(
        _GRANTS,
        id_factory=lambda: f"demo-{label}",
        **({"allowed_purposes": allowed_purposes} if allowed_purposes else {}),
    )
    request = AuthorizationRequest(actor_id=_ACTOR, patient_id=patient_id, action=Action.VIEW_PATIENT_CHART, purpose=purpose)

    print(f"\n=== {label} (patient_id={patient_id}, purpose={purpose.value}) ===")
    try:
        result = run_patient_view(request, authorizer=authorizer, repository=repository)
    except AuthorizationDenied as exc:
        print(f"DENIED before any read (reason={exc.denial.reason.value}, repository_reads={repository.load_calls})")
        return
    print(json.dumps(result.model_dump(mode="json"), indent=2))


def main() -> None:
    _run("allowed-treatment", 1042)
    _run("denied-unauthorized-patient", 9999)
    _run("missing-evidence-escalation", 5000)
    # Purpose is explicitly authorized here so this reaches the SUPERVISOR's
    # own escalation decision (not an earlier authorization denial) — proving
    # the non-treatment-purpose escalation is the supervisor's own
    # deterministic rule, not a side effect of the policy fixture.
    _run(
        "non-treatment-purpose-escalation",
        1043,
        purpose=Purpose.PAYMENT,
        allowed_purposes={Purpose.TREATMENT, Purpose.PAYMENT},
    )


if __name__ == "__main__":
    main()
