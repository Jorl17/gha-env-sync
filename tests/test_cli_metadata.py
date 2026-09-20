import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "gha-env-sync.py"


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        text=True,
        capture_output=True,
        check=False,
    )


def test_version_command_returns_success() -> None:
    cp = run_cli("--version")
    assert cp.returncode == 0
    assert "gha-env-sync" in f"{cp.stdout}\n{cp.stderr}"


def test_help_lists_core_subcommands() -> None:
    cp = run_cli("--help")
    assert cp.returncode == 0
    assert "{apply,list,export,diff}" in cp.stdout
