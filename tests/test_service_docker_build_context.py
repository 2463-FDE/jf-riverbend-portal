"""W10 Final Stage 7 sub-slice 4 discovery (OBS-M02) — any service that
imports `libs.*` (a shared package living at the repo root, outside every
service's own directory) must build from the repo-root Compose context and
COPY libs/ into its image. `scheduling-service`, `roi-service`, and
`interop-service` all imported libs.* while still building from their own
per-service directory — the image built fine (COPY . . just copies that
directory), but the container crashed at startup with
ModuleNotFoundError, invisible to CI (image build only, never runs the
container) and to pytest (imports directly from the repo checkout, where
libs/ is on the path regardless of any Dockerfile).

This is a static check — no docker build/run — so it runs in the regular
suite and catches the very next service that grows a `from libs...` import
without updating its build context and Dockerfile to match.
"""
import pathlib
import re

import pytest

yaml = pytest.importorskip("yaml")

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_COMPOSE = _ROOT / "docker-compose.yml"
_SERVICES_DIR = _ROOT / "services"

_LIBS_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+libs(?:\.\w+)*\b", re.MULTILINE)


def _service_dirs():
    return sorted(p.name for p in _SERVICES_DIR.iterdir() if p.is_dir())


def _service_imports_libs(service_name):
    service_dir = _SERVICES_DIR / service_name
    for py_file in service_dir.rglob("*.py"):
        if _LIBS_IMPORT_RE.search(py_file.read_text()):
            return True
    return False


def _compose_services():
    return yaml.safe_load(_COMPOSE.read_text())["services"]


@pytest.mark.parametrize("service_name", _service_dirs())
def test_a_service_that_imports_libs_builds_from_the_repo_root(service_name):
    if not _service_imports_libs(service_name):
        pytest.skip(f"{service_name} does not import libs.*")

    compose_services = _compose_services()
    assert service_name in compose_services, f"{service_name} has no docker-compose.yml service entry"
    build = compose_services[service_name].get("build")
    assert isinstance(build, dict) and build.get("context") == ".", (
        f"{service_name} imports libs.* but its Compose build context is "
        f"{build!r}, not the repo root — libs/ would not be present in its "
        f"build context at all"
    )

    dockerfile_path = _ROOT / build["dockerfile"]
    dockerfile_text = dockerfile_path.read_text()
    assert "COPY libs/ ./libs/" in dockerfile_text, (
        f"{dockerfile_path.relative_to(_ROOT)} does not COPY libs/ into the "
        f"image even though {service_name} imports libs.*"
    )


# W10 Metrics completion-gate fix: `make rag-eval-publish`'s patient-record-
# corpus half (python3 -m libs.rag_eval.harness --publish, run inside
# records-service) reads these three fixed, synthetic seed fixtures directly
# off disk (libs/rag_corpus/corpus.py, libs/rag_eval/identity_proxy.py,
# libs/rag_eval/clinical_fields.py, libs/rag_eval/goldset.py) — never copied
# into the image before this fix, so the target failed with FileNotFoundError
# the first time it actually ran inside a container. Same static-check style
# as the parametrized test above: no live docker build, just proving the
# Dockerfile COPYs each file AND .dockerignore actually re-includes it (a
# COPY alone is not enough — db/* is excluded by default; see .dockerignore's
# own per-ancestor-level re-inclusion comments).
_RECORDS_SERVICE_SEED_FIXTURES = ("db/seed/patients.csv", "db/seed/encounters.csv", "db/seed/goldset.json")


@pytest.mark.parametrize("fixture_path", _RECORDS_SERVICE_SEED_FIXTURES)
def test_records_service_copies_and_unignores_each_rag_eval_seed_fixture(fixture_path):
    assert (_ROOT / fixture_path).is_file(), f"{fixture_path} does not exist on disk"

    dockerfile_text = (_ROOT / "services" / "records-service" / "Dockerfile").read_text()
    assert f"COPY {fixture_path} ./{fixture_path}" in dockerfile_text, (
        f"services/records-service/Dockerfile does not COPY {fixture_path} into the image"
    )

    dockerignore_text = (_ROOT / ".dockerignore").read_text()
    assert f"!{fixture_path}" in dockerignore_text or f"!{fixture_path.rsplit('/', 1)[0]}/" in dockerignore_text, (
        f".dockerignore does not re-include {fixture_path} (or its parent directory) — "
        f"db/* is excluded by default, so the Dockerfile's COPY would silently see nothing"
    )
