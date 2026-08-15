"""Services that verify no caller identity must not be reachable from the host.

Four services — eligibility, scheduling, interop, roi — perform no
`INTERNAL_SERVICE_TOKEN` check and no authorization of their own. While their
ports were published, the gateway's RBAC was bypassable for all four: anything
that could reach the host could call them directly and supply whatever
`X-Actor-Id` it liked. `roi-service` is the sharpest case, because
`/disclosures/{patient_id}` takes only a database session and releases records.

Unpublishing those ports is containment, not the fix. The permanent fix is
token verification plus data-layer authorization in each service (the cycle's
branch 7). This test exists so the containment cannot be quietly undone in the
meantime — re-adding a `ports:` entry for any of these four fails here.

The services keep talking to each other unchanged: compose networking resolves
`http://roi-service:8076` by service name, which never depended on host
publishing. Container healthchecks likewise run inside the container against
`localhost`, so they are unaffected.
"""
import pathlib

import pytest

yaml = pytest.importorskip("yaml")

_COMPOSE = pathlib.Path(__file__).resolve().parents[1] / "docker-compose.yml"

# Services with no caller verification. Keep this list in step with reality: if
# one of them gains a real token check and authorization, publishing its port
# becomes a deliberate decision rather than an oversight, and it can move to
# _MAY_PUBLISH with that reasoning recorded.
_MUST_NOT_PUBLISH = ("eligibility-service", "scheduling-service", "interop-service", "roi-service")

# Deliberately reachable: the gateway is the entry point, the frontend is the
# UI, and intake/records verify the internal token before honouring a forwarded
# actor. Postgres and Redis are published for local tooling and tests.
_MAY_PUBLISH = ("gateway", "frontend", "intake-service", "records-service", "postgres", "redis")


def _services():
    return yaml.safe_load(_COMPOSE.read_text())["services"]


@pytest.mark.parametrize("service", _MUST_NOT_PUBLISH)
def test_a_service_that_verifies_nothing_is_not_published_to_the_host(service):
    published = _services()[service].get("ports")
    assert not published, (
        f"{service} publishes {published} to the host. It performs no caller "
        f"verification, so a published port makes the gateway's authorization "
        f"bypassable for it. Remove the ports entry, or close the trust gap in "
        f"that service first and update this test with the reasoning."
    )


@pytest.mark.parametrize("service", _MAY_PUBLISH)
def test_the_intended_entry_points_are_still_reachable(service):
    """The containment must not have been applied by unpublishing everything.

    A change that made the whole stack unreachable would also make the test
    above pass, which would be the wrong kind of green.
    """
    assert _services()[service].get("ports"), (
        f"{service} publishes no ports — the stack would be unusable locally"
    )


def test_every_service_is_accounted_for():
    """Nobody adds a service without deciding whether it may be published.

    A new service defaulting to "published" is exactly how this gap appeared
    the first time.
    """
    known = set(_MUST_NOT_PUBLISH) | set(_MAY_PUBLISH)
    actual = set(_services())
    unaccounted = actual - known
    assert not unaccounted, (
        f"{sorted(unaccounted)} not classified. Decide whether each verifies "
        f"its callers, then add it to _MUST_NOT_PUBLISH or _MAY_PUBLISH."
    )
