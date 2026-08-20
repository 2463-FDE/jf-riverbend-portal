"""Branch 7B — interop-service and roi-service verify their callers.

Both services previously accepted any in-network caller with any forged
X-Actor-Id. #39 unpublished their host ports, which is containment, not
authentication: anything already on the compose network was trusted blind.

What matters here is the fail-CLOSED direction. A guard that refuses a missing
header but accepts an empty-vs-empty comparison, or a "changeme" placeholder,
reopens the exact bypass it was added to close — so those cases are asserted
explicitly rather than assumed to follow from the happy path.

roi-service is the sharpest case in the repo: `/disclosures/{patient_id}`
releases a patient's records and has no gateway route at all, so before this
branch its only reachable caller was an unauthenticated direct one.
"""
import pytest

from conftest import load_module

interop = load_module("services/interop-service/app.py", "interop_app_trust_7b")
roi = load_module("services/roi-service/app.py", "roi_app_trust_7b")

TOKEN = "test-internal-token-for-7b-well-over-32-chars"

# (module, label) — both services are held to identical semantics on purpose.
SERVICES = [(interop, "interop-service"), (roi, "roi-service")]
IDS = ["interop", "roi"]


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    for mod, _ in SERVICES:
        monkeypatch.setattr(mod.settings, "internal_service_token", TOKEN)


@pytest.mark.parametrize("mod,label", SERVICES, ids=IDS)
def test_the_matching_token_is_accepted(mod, label):
    # Without this the suite would pass if the guard simply rejected
    # everything, which is the failure mode a refusal-only test cannot see.
    assert mod._verify_internal_token(TOKEN) is None


@pytest.mark.parametrize("mod,label", SERVICES, ids=IDS)
def test_a_caller_with_no_token_is_refused(mod, label):
    with pytest.raises(mod.HTTPException) as exc:
        mod._verify_internal_token(None)
    assert exc.value.status_code == 401


@pytest.mark.parametrize("mod,label", SERVICES, ids=IDS)
def test_a_wrong_token_is_refused(mod, label):
    with pytest.raises(mod.HTTPException) as exc:
        mod._verify_internal_token("not-the-token-but-still-over-32-characters")
    assert exc.value.status_code == 401


@pytest.mark.parametrize("mod,label", SERVICES, ids=IDS)
def test_an_unconfigured_token_does_not_match_an_empty_header(mod, label, monkeypatch):
    # If INTERNAL_SERVICE_TOKEN is unset on both sides, "" must not compare
    # equal to "" — that would silently reopen the bypass for every route.
    monkeypatch.setattr(mod.settings, "internal_service_token", "")
    with pytest.raises(mod.HTTPException) as exc:
        mod._verify_internal_token("")
    assert exc.value.status_code == 401


@pytest.mark.parametrize("mod,label", SERVICES, ids=IDS)
def test_a_short_placeholder_is_never_treated_as_configured(mod, label, monkeypatch):
    # "changeme" is the value a hurried deploy actually gets. Matching it must
    # not be enough to pass.
    monkeypatch.setattr(mod.settings, "internal_service_token", "changeme")
    with pytest.raises(mod.HTTPException) as exc:
        mod._verify_internal_token("changeme")
    assert exc.value.status_code == 401
    assert mod._internal_token_is_configured() is False


@pytest.mark.parametrize("mod,label", SERVICES, ids=IDS)
def test_the_service_refuses_to_start_on_an_unusable_token(mod, label, monkeypatch):
    # Compose's ${VAR:?...} catches a MISSING value. It cannot catch a present
    # but unusable one, which is what this startup hook exists for: fail loudly
    # instead of serving traffic that 401s every gateway call.
    monkeypatch.setattr(mod.settings, "internal_service_token", "too-short")
    with pytest.raises(RuntimeError, match="INTERNAL_SERVICE_TOKEN"):
        mod._fail_fast_on_an_unusable_token()


@pytest.mark.parametrize("mod,label", SERVICES, ids=IDS)
def test_healthz_is_the_only_unguarded_route(mod, label):
    """A guard applied route-by-route is a guard that gets forgotten on the
    next route added. This pins the intended shape: everything except the
    container healthcheck carries the dependency.
    """
    unguarded = []
    for route in mod.app.routes:
        path = getattr(route, "path", None)
        if path in (None, "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"):
            continue
        deps = getattr(getattr(route, "dependant", None), "dependencies", [])
        if not any(d.call is mod._verify_internal_token for d in deps):
            unguarded.append(path)

    assert unguarded == ["/healthz"], (
        f"{label}: expected only /healthz to be unguarded, found {unguarded}. "
        f"Every other route must carry Depends(_verify_internal_token)."
    )
