"""Single loader for the CLI under test.

``gha-env-sync.py`` is a hyphenated executable script, not an importable module
name, so it has to be loaded by path. Both ``conftest.py`` and the integration
suite need it -- the latter at import time, to reuse the tool's own retry rules
rather than restating them -- so the loading lives here once.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "gha-env-sync.py"
MODULE_NAME = "gha_env_sync_module_under_test"


def load_module() -> ModuleType:
    """Import the CLI script once and return it, cached in ``sys.modules``."""

    cached = sys.modules.get(MODULE_NAME)
    if cached is not None:
        return cached

    spec = importlib.util.spec_from_file_location(MODULE_NAME, SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {SCRIPT_PATH}")

    loaded = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = loaded
    spec.loader.exec_module(loaded)
    return loaded
