"""
Shared test helpers.

There is no shared Python package across services (see adr/0001), so tests load
the specific module-under-test directly from its service directory by file path.
This avoids the module-name collisions you'd otherwise get from every service
having its own `app.py` / `config.py` / `models.py`.
"""
import importlib.util
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Real shared packages (currently libs/llm_client, libs/safe_logging) live at
# the repo root and are imported normally (`import libs.llm_client`), unlike
# the per-service modules below which have no shared package (adr/0001) and
# are loaded by file path instead. Plain `pytest` only adds this file's own
# directory to sys.path, not the repo root, so add it explicitly.
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def load_module(relpath: str, name: str):
    """Load <REPO_ROOT>/<relpath> as a uniquely-named module."""
    path = os.path.join(REPO_ROOT, relpath)
    service_dir = os.path.dirname(path)
    # allow the module to import its own siblings (config, etc.)
    if service_dir not in sys.path:
        sys.path.insert(0, service_dir)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
