"""
Integration test proving the Stage 3 Docker/libs import gap fix: intake-
service and eligibility-service now import libs/ (libs.tracing,
libs.eligibility_agent, libs.safe_logging), which requires their build
context to be the repo root (see docker-compose.yml + the two Dockerfiles).
This actually builds the images and runs `import app` inside each container
— the same failure mode a stale/wrong build context would produce.

Requires a working Docker daemon. Skipped (not failed) if `docker` isn't on
PATH, mirroring test_records_flow.py's `pytest.importorskip` pattern for a
missing dependency rather than treating an unavailable environment as a
failure.

Run with:  pytest -m integration
Skipped by default in CI (`pytest -m "not integration"`).
"""
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.integration

if shutil.which("docker") is None:
    pytest.skip("docker CLI not available in this environment", allow_module_level=True)


def _docker_available() -> bool:
    try:
        subprocess.run(["docker", "info"], capture_output=True, timeout=10, check=True)
        return True
    except Exception:
        return False


if not _docker_available():
    pytest.skip("docker daemon not reachable in this environment", allow_module_level=True)


PROJECT_NAME = "jf-riverbend-portal"


@pytest.mark.parametrize("service,image_suffix", [("intake-service", "intake-service"), ("eligibility-service", "eligibility-service")])
def test_service_image_builds_and_imports_app_with_libs_available(service, image_suffix):
    build = subprocess.run(
        ["docker", "compose", "build", service],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert build.returncode == 0, f"docker compose build {service} failed:\n{build.stdout}\n{build.stderr}"

    image = f"{PROJECT_NAME}-{image_suffix}"
    run = subprocess.run(
        ["docker", "run", "--rm", image, "python", "-c", "import app; print('import ok')"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == 0, f"container import check failed for {image}:\n{run.stdout}\n{run.stderr}"
    assert "import ok" in run.stdout


def test_libs_eligibility_agent_and_tracing_import_inside_eligibility_service_container():
    image = f"{PROJECT_NAME}-eligibility-service"
    run = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            image,
            "python",
            "-c",
            "import libs.tracing, libs.eligibility_agent, libs.safe_logging; print('libs ok')",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == 0, f"libs import failed inside {image}:\n{run.stdout}\n{run.stderr}"
    assert "libs ok" in run.stdout


def test_compose_config_is_valid():
    result = subprocess.run(["docker", "compose", "config", "-q"], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"docker compose config failed:\n{result.stderr}"
