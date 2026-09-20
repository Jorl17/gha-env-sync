from pathlib import Path
from types import ModuleType

import pytest

from _gha_module import REPO_ROOT, load_module


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def module() -> ModuleType:
    return load_module()
