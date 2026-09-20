"""Real GitHub integration scenarios for ``gha-env-sync``.

Execution model:
- These tests are opt-in via ``-m integration`` and require a real repo.
- The wrapper script sets ``GHA_ENV_SYNC_TEST_REPO`` and optional env scope.

Safety model:
- Remote mutations are limited to names starting with ``GHA_ENV_SYNC_IT_``.
- Cleanup helpers delete only prefixed keys and verify convergence.
- No non-prefixed remote keys are targeted by this suite.

Filesystem side effects:
- Test input files are created under pytest ``tmp_path``.
- One test creates a temporary file in ``Path.home()`` to validate ``~`` path
  expansion, then deletes it in a ``finally`` block.
"""

import json
import os
import shlex
import subprocess
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from _gha_module import SCRIPT_PATH, load_module


pytestmark = pytest.mark.integration

# Retry policy and transient-failure detection are the tool's own, not a
# second copy: if the shipped rules are wrong, these tests must feel it too.
GHA = load_module()
GH_RETRY_ATTEMPTS = GHA.GH_RETRY_ATTEMPTS
GH_RETRY_BASE_DELAY_SECONDS = GHA.GH_RETRY_BASE_DELAY_SECONDS
is_transient_gh_failure = GHA.is_transient_gh_failure

TEST_KEY_PREFIX = "GHA_ENV_SYNC_IT_"
DEFAULT_ENV_NAME = "gha-env-sync-it"
STATE_POLL_TIMEOUT_SECONDS = 30.0
STATE_POLL_INTERVAL_SECONDS = 1.0
STATE_SETTLE_EMPTY_CHECKS = 2
def fail_command(context: str, cmd: list[str], cp: subprocess.CompletedProcess[str], *, step: int) -> None:
    """Emit a consistent, verbose assertion failure for command execution."""

    details = (
        f"{context} failed (step {step}).\n"
        f"Command: {' '.join(cmd)}\n"
        f"Return code: {cp.returncode}\n"
        f"STDOUT:\n{cp.stdout}\n"
        f"STDERR:\n{cp.stderr}\n"
    )
    pytest.fail(details)


@dataclass(frozen=True)
class ScopeTarget:
    """Represents repo-level or environment-level target scope."""

    repo: str
    env_name: str | None

    @property
    def label(self) -> str:
        if self.env_name:
            return f"env:{self.env_name}"
        return "repo"


@dataclass
class ScenarioLogger:
    """Tiny structured logger so integration output is step-by-step readable."""

    scenario: str
    step_no: int = 0

    def log_step(self, message: str) -> int:
        self.step_no += 1
        print(f"[{self.scenario}] step {self.step_no}: {message}", flush=True)
        return self.step_no

    def log_cmd(self, step: int, cmd: list[str]) -> None:
        print(f"[{self.scenario}] step {step} cmd: {' '.join(shlex.quote(part) for part in cmd)}", flush=True)

    def log_result(self, step: int, message: str) -> None:
        print(f"[{self.scenario}] step {step} result: {message}", flush=True)


def is_delete_not_found(cmd: list[str], cp: subprocess.CompletedProcess[str]) -> bool:
    """Return true when a gh delete reports the target is already gone."""

    if len(cmd) < 4 or cmd[0] != "gh":
        return False
    if cmd[1] not in {"secret", "variable"} or cmd[2] != "delete":
        return False
    if cp.returncode == 0:
        return False
    combined = f"{cp.stdout}\n{cp.stderr}".lower()
    return "http 404" in combined or "not found" in combined


def run_cmd(
    cmd: list[str],
    *,
    logger: ScenarioLogger,
    context: str,
    env_overrides: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Execute a command with scenario logging and transient GH retries.

    The retry loop is intentionally limited to ``gh`` commands because those are
    subject to transient API/network faults in live integration runs.
    """

    step = logger.log_step(context)
    logger.log_cmd(step, cmd)

    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)

    max_attempts = GH_RETRY_ATTEMPTS if cmd and cmd[0] == "gh" else 1
    cp: subprocess.CompletedProcess[str] | None = None
    for attempt in range(1, max_attempts + 1):
        cp = subprocess.run(cmd, text=True, capture_output=True, check=False, env=env)
        if cp.returncode == 0:
            if attempt == 1:
                logger.log_result(step, "ok")
            else:
                logger.log_result(step, f"ok (attempt {attempt}/{max_attempts})")
            return cp

        if attempt < max_attempts and is_transient_gh_failure(cmd, cp):
            sleep_seconds = GH_RETRY_BASE_DELAY_SECONDS * attempt
            logger.log_result(
                step,
                (
                    f"transient rc={cp.returncode}; retrying in {sleep_seconds:.1f}s "
                    f"(attempt {attempt}/{max_attempts})"
                ),
            )
            time.sleep(sleep_seconds)
            continue
        break

    assert cp is not None

    if check and cp.returncode != 0:
        fail_command(context, cmd, cp, step=step)

    if cp.returncode != 0:
        logger.log_result(step, f"rc={cp.returncode}")
    return cp


def scope_gh_args(scope: ScopeTarget) -> list[str]:
    """Build scope args for direct ``gh`` calls."""

    args = ["-R", scope.repo]
    if scope.env_name:
        args += ["--env", scope.env_name]
    return args


def scope_cli_args(scope: ScopeTarget) -> list[str]:
    """Build scope args for ``gha-env-sync.py`` invocations."""

    args = ["--repo", scope.repo]
    if scope.env_name:
        args += ["--env", scope.env_name]
    return args


def run_cli(
    scope: ScopeTarget,
    *cli_args: str,
    logger: ScenarioLogger,
    context: str,
    env_overrides: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run the CLI script for a given scope and command."""

    cmd = [str(SCRIPT_PATH), *scope_cli_args(scope), *cli_args]
    return run_cmd(cmd, logger=logger, context=context, env_overrides=env_overrides, check=check)


def ensure_environment_exists(scope: ScopeTarget, logger: ScenarioLogger) -> None:
    """Create/update the GitHub Actions environment when env scope is used."""

    if not scope.env_name:
        return

    owner, repo = scope.repo.split("/", 1)
    endpoint = f"repos/{owner}/{repo}/environments/{scope.env_name}"
    run_cmd(
        ["gh", "api", "-X", "PUT", endpoint],
        logger=logger,
        context=f"ensure environment exists ({scope.env_name})",
    )


def list_prefixed_keys(
    scope: ScopeTarget,
    *,
    kind: str,
    prefix: str,
    logger: ScenarioLogger,
) -> list[str]:
    """List remote keys for one kind, filtered to the integration prefix."""

    if kind == "secret":
        cmd = ["gh", "secret", "list", *scope_gh_args(scope), "--json", "name"]
    elif kind == "variable":
        cmd = ["gh", "variable", "list", *scope_gh_args(scope), "--json", "name"]
    else:
        raise ValueError(f"unsupported kind: {kind}")

    cp = run_cmd(cmd, logger=logger, context=f"list {kind}s ({scope.label})")
    rows = json.loads(cp.stdout or "[]")
    names = sorted(
        row.get("name", "")
        for row in rows
        if isinstance(row, dict) and row.get("name", "").startswith(prefix)
    )
    logger.log_result(logger.step_no, f"prefixed {kind} keys: {names if names else 'none'}")
    return names


def clear_prefixed_keys(scope: ScopeTarget, *, prefix: str, logger: ScenarioLogger) -> None:
    """Delete only prefixed keys, with polling rounds for eventual consistency."""

    max_rounds = 12
    empty_streak = 0
    for round_no in range(1, max_rounds + 1):
        secret_keys = list_prefixed_keys(scope, kind="secret", prefix=prefix, logger=logger)
        variable_keys = list_prefixed_keys(scope, kind="variable", prefix=prefix, logger=logger)

        if not secret_keys and not variable_keys:
            empty_streak += 1
            logger.log_result(
                logger.step_no,
                (
                    f"cleanup empty check {empty_streak}/{STATE_SETTLE_EMPTY_CHECKS} "
                    f"({scope.label})"
                ),
            )
            if empty_streak >= STATE_SETTLE_EMPTY_CHECKS:
                logger.log_result(logger.step_no, f"cleanup complete ({scope.label})")
                return
            time.sleep(STATE_POLL_INTERVAL_SECONDS)
            continue
        empty_streak = 0

        logger.log_result(
            logger.step_no,
            f"cleanup round {round_no}/{max_rounds}: secrets={len(secret_keys)} vars={len(variable_keys)}",
        )

        for key in secret_keys:
            cmd = ["gh", "secret", "delete", key, *scope_gh_args(scope)]
            cp = run_cmd(
                cmd,
                logger=logger,
                context=f"delete secret {key} ({scope.label})",
                check=False,
            )
            if cp.returncode != 0 and not is_delete_not_found(cmd, cp):
                fail_command(f"delete secret {key} ({scope.label})", cmd, cp, step=logger.step_no)
            if cp.returncode != 0:
                logger.log_result(logger.step_no, f"already absent (404): {key}")

        for key in variable_keys:
            cmd = ["gh", "variable", "delete", key, *scope_gh_args(scope)]
            cp = run_cmd(
                cmd,
                logger=logger,
                context=f"delete variable {key} ({scope.label})",
                check=False,
            )
            if cp.returncode != 0 and not is_delete_not_found(cmd, cp):
                fail_command(f"delete variable {key} ({scope.label})", cmd, cp, step=logger.step_no)
            if cp.returncode != 0:
                logger.log_result(logger.step_no, f"already absent (404): {key}")

        if round_no < max_rounds:
            time.sleep(STATE_POLL_INTERVAL_SECONDS)

    remaining_secrets = list_prefixed_keys(scope, kind="secret", prefix=prefix, logger=logger)
    remaining_vars = list_prefixed_keys(scope, kind="variable", prefix=prefix, logger=logger)
    pytest.fail(
        "cleanup did not converge for prefixed keys "
        f"({scope.label}); remaining secrets={remaining_secrets} vars={remaining_vars}"
    )


def fetch_remote_state(scope: ScopeTarget, *, logger: ScenarioLogger) -> tuple[set[str], dict[str, str]]:
    """Fetch current remote state through CLI ``list --json``."""

    cp = run_cli(scope, "list", "--json", logger=logger, context=f"list remote state ({scope.label})")
    payload = json.loads(cp.stdout or "{}")
    secret_names = {
        row.get("name")
        for row in payload.get("secrets", [])
        if isinstance(row, dict) and row.get("name")
    }
    var_map = {
        row.get("name"): row.get("value", "")
        for row in payload.get("vars", [])
        if isinstance(row, dict) and row.get("name")
    }
    return secret_names, var_map


def write_combined_env(path: Path, *, secret_lines: list[str], var_lines: list[str]) -> None:
    """Write a temporary combined env file used as test input."""

    content = ["# --- secrets ---", *secret_lines, "", "# --- vars ---", *var_lines, ""]
    path.write_text("\n".join(content), encoding="utf-8")


def wait_for_prefixed_keys(
    scope: ScopeTarget,
    *,
    kind: str,
    prefix: str,
    logger: ScenarioLogger,
    expect_present: bool,
    timeout_seconds: float = STATE_POLL_TIMEOUT_SECONDS,
) -> list[str]:
    """Poll until prefixed keys appear/disappear as expected."""

    deadline = time.monotonic() + timeout_seconds
    last_keys: list[str] = []
    while True:
        last_keys = list_prefixed_keys(scope, kind=kind, prefix=prefix, logger=logger)
        if expect_present and last_keys:
            return last_keys
        if not expect_present and not last_keys:
            return []
        if time.monotonic() >= deadline:
            break
        time.sleep(STATE_POLL_INTERVAL_SECONDS)

    if expect_present:
        pytest.fail(
            f"timed out waiting for prefixed {kind} keys to appear in {scope.label}; "
            f"last keys={last_keys}"
        )
    pytest.fail(
        f"timed out waiting for prefixed {kind} keys to be deleted in {scope.label}; "
        f"remaining keys={last_keys}"
    )


def wait_for_diff_exit_code(
    scope: ScopeTarget,
    *,
    env_file: Path,
    expected_returncode: int,
    logger: ScenarioLogger,
    context: str,
    env_overrides: dict[str, str] | None = None,
    timeout_seconds: float = STATE_POLL_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Poll ``diff`` until it reaches the expected return code."""

    deadline = time.monotonic() + timeout_seconds
    attempt = 0
    last_cp: subprocess.CompletedProcess[str] | None = None
    while time.monotonic() < deadline:
        attempt += 1
        last_cp = run_cli(
            scope,
            "diff",
            "--file",
            str(env_file),
            logger=logger,
            context=f"{context} (attempt {attempt})",
            env_overrides=env_overrides,
            check=False,
        )
        if last_cp.returncode == expected_returncode:
            return last_cp
        time.sleep(STATE_POLL_INTERVAL_SECONDS)

    assert last_cp is not None
    pytest.fail(
        f"{context} did not reach rc={expected_returncode} within {timeout_seconds:.0f}s. "
        f"Last rc={last_cp.returncode}\nSTDOUT:\n{last_cp.stdout}\nSTDERR:\n{last_cp.stderr}"
    )


@contextmanager
def isolated_scope(scope: ScopeTarget, logger: ScenarioLogger):
    """Context manager that guarantees prefix-only cleanup before and after."""

    ensure_environment_exists(scope, logger)
    clear_prefixed_keys(scope, prefix=TEST_KEY_PREFIX, logger=logger)
    try:
        yield
    finally:
        clear_prefixed_keys(scope, prefix=TEST_KEY_PREFIX, logger=logger)


@pytest.fixture(scope="module")
def integration_repo() -> str:
    """Resolve and validate the externally managed integration repository."""

    repo = os.getenv("GHA_ENV_SYNC_TEST_REPO", "").strip()
    if not repo:
        pytest.skip("Set GHA_ENV_SYNC_TEST_REPO=OWNER/REPO to run real-repo integration tests.")

    logger = ScenarioLogger("preflight")

    if not SCRIPT_PATH.exists():
        pytest.fail(f"CLI script not found at {SCRIPT_PATH}")
    if not os.access(SCRIPT_PATH, os.X_OK):
        pytest.fail(f"CLI script is not executable: {SCRIPT_PATH} (run: chmod +x {SCRIPT_PATH})")

    run_cmd(["gh", "auth", "status"], logger=logger, context="verify gh auth")

    repo_exists = run_cmd(
        ["gh", "repo", "view", repo, "--json", "nameWithOwner"],
        logger=logger,
        context=f"verify target repo exists ({repo})",
        check=False,
    )
    if repo_exists.returncode != 0:
        details = (repo_exists.stderr or repo_exists.stdout or "").strip()
        pytest.fail(
            "Integration repo is not available. "
            f"Create it externally first, then rerun tests. Repo: {repo}\n{details}"
        )

    return repo


@pytest.fixture(scope="module")
def integration_env() -> str:
    """Return integration environment name (default when unset)."""

    return os.getenv("GHA_ENV_SYNC_TEST_ENV", DEFAULT_ENV_NAME)


@pytest.fixture
def repo_scope(integration_repo: str) -> ScopeTarget:
    """Repo-level scope fixture."""

    return ScopeTarget(repo=integration_repo, env_name=None)


@pytest.fixture
def env_scope(integration_repo: str, integration_env: str) -> ScopeTarget:
    """Environment-level scope fixture."""

    return ScopeTarget(repo=integration_repo, env_name=integration_env)


def test_repo_scope_create_apply_list_export_diff(repo_scope: ScopeTarget, tmp_path: Path) -> None:
    """Repo scope happy path: create/apply/list/export/diff all match expectations."""

    logger = ScenarioLogger("repo-create-apply-list-export-diff")
    with isolated_scope(repo_scope, logger):
        secret_file = tmp_path / "repo_secret_file.txt"
        secret_file.write_text("repo-secret-file-value", encoding="utf-8")

        var_file = tmp_path / "repo_var_file.txt"
        var_file.write_text("repo-var-file-value", encoding="utf-8")

        env_file = tmp_path / "repo_create.env"
        write_combined_env(
            env_file,
            secret_lines=[
                f"{TEST_KEY_PREFIX}REPO_SECRET_SIMPLE=simple-secret",
                f"{TEST_KEY_PREFIX}REPO_SECRET_FILE=@file:{secret_file}",
            ],
            var_lines=[
                f"{TEST_KEY_PREFIX}REPO_VAR_SIMPLE=simple-var",
                f"{TEST_KEY_PREFIX}REPO_VAR_FILE=@file:{var_file}",
            ],
        )

        run_cli(repo_scope, "apply", "--file", str(env_file), logger=logger, context="apply repo create baseline")

        diff_cp = run_cli(
            repo_scope,
            "diff",
            "--file",
            str(env_file),
            logger=logger,
            context="diff repo baseline",
        )
        assert diff_cp.returncode == 0

        secret_names, var_map = fetch_remote_state(repo_scope, logger=logger)
        assert f"{TEST_KEY_PREFIX}REPO_SECRET_SIMPLE" in secret_names
        assert f"{TEST_KEY_PREFIX}REPO_SECRET_FILE" in secret_names
        assert var_map.get(f"{TEST_KEY_PREFIX}REPO_VAR_SIMPLE") == "simple-var"
        assert var_map.get(f"{TEST_KEY_PREFIX}REPO_VAR_FILE") == "repo-var-file-value"

        export_cp = run_cli(repo_scope, "export", "--file", "-", logger=logger, context="export repo baseline")
        assert f"{TEST_KEY_PREFIX}REPO_SECRET_SIMPLE=__REDACTED__" in export_cp.stdout
        assert f"{TEST_KEY_PREFIX}REPO_SECRET_FILE=__REDACTED__" in export_cp.stdout
        assert f"{TEST_KEY_PREFIX}REPO_VAR_SIMPLE=simple-var" in export_cp.stdout
        assert f"{TEST_KEY_PREFIX}REPO_VAR_FILE=repo-var-file-value" in export_cp.stdout


def test_repo_scope_update_cycle_and_diff_detection(repo_scope: ScopeTarget, tmp_path: Path) -> None:
    """Repo scope update cycle: diff detects drift, then converges after apply."""

    logger = ScenarioLogger("repo-update-cycle")
    with isolated_scope(repo_scope, logger):
        baseline_var_file = tmp_path / "repo_update_var_file_baseline.txt"
        baseline_var_file.write_text("var-file-v1", encoding="utf-8")

        baseline_env = tmp_path / "repo_update_baseline.env"
        write_combined_env(
            baseline_env,
            secret_lines=[
                f"{TEST_KEY_PREFIX}REPO_SECRET_TRACKED=s-old",
            ],
            var_lines=[
                f"{TEST_KEY_PREFIX}REPO_VAR_TRACKED=v1",
                f"{TEST_KEY_PREFIX}REPO_VAR_FILE=@file:{baseline_var_file}",
            ],
        )
        run_cli(repo_scope, "apply", "--file", str(baseline_env), logger=logger, context="apply repo baseline for update")

        updated_var_file = tmp_path / "repo_update_var_file_updated.txt"
        updated_var_file.write_text("var-file-v2", encoding="utf-8")

        updated_env = tmp_path / "repo_update_updated.env"
        write_combined_env(
            updated_env,
            secret_lines=[
                f"{TEST_KEY_PREFIX}REPO_SECRET_TRACKED=s-new",
            ],
            var_lines=[
                f"{TEST_KEY_PREFIX}REPO_VAR_TRACKED=v2",
                f"{TEST_KEY_PREFIX}REPO_VAR_FILE=@file:{updated_var_file}",
            ],
        )

        drift_cp = run_cli(
            repo_scope,
            "diff",
            "--file",
            str(updated_env),
            logger=logger,
            context="diff repo updated before apply",
            check=False,
        )
        assert drift_cp.returncode == 2
        combined = (drift_cp.stdout or "") + (drift_cp.stderr or "")
        assert (
            "Missing on remote:" in combined
            or "Extra on remote:" in combined
            or "Different values:" in combined
        )

        run_cli(repo_scope, "apply", "--file", str(updated_env), logger=logger, context="apply repo updated")
        settled_cp = wait_for_diff_exit_code(
            repo_scope,
            env_file=updated_env,
            expected_returncode=0,
            logger=logger,
            context="diff repo updated after apply",
        )
        assert settled_cp.returncode == 0


def test_repo_scope_selective_apply_only_vars(repo_scope: ScopeTarget, tmp_path: Path) -> None:
    """Repo scope ``--only vars`` updates vars without adding new secrets."""

    logger = ScenarioLogger("repo-selective-only-vars")
    with isolated_scope(repo_scope, logger):
        baseline_env = tmp_path / "repo_only_vars_baseline.env"
        write_combined_env(
            baseline_env,
            secret_lines=[f"{TEST_KEY_PREFIX}REPO_ONLY_VARS_SECRET_OLD=s-old"],
            var_lines=[f"{TEST_KEY_PREFIX}REPO_ONLY_VARS_VAR=v-old"],
        )
        run_cli(repo_scope, "apply", "--file", str(baseline_env), logger=logger, context="apply baseline")

        updated_env = tmp_path / "repo_only_vars_updated.env"
        write_combined_env(
            updated_env,
            secret_lines=[f"{TEST_KEY_PREFIX}REPO_ONLY_VARS_SECRET_NEW=s-new"],
            var_lines=[f"{TEST_KEY_PREFIX}REPO_ONLY_VARS_VAR=v-new"],
        )
        run_cli(
            repo_scope,
            "apply",
            "--file",
            str(updated_env),
            "--only",
            "vars",
            logger=logger,
            context="apply updated with --only vars",
        )

        secret_names, var_map = fetch_remote_state(repo_scope, logger=logger)
        assert f"{TEST_KEY_PREFIX}REPO_ONLY_VARS_SECRET_OLD" in secret_names
        assert f"{TEST_KEY_PREFIX}REPO_ONLY_VARS_SECRET_NEW" not in secret_names
        assert var_map.get(f"{TEST_KEY_PREFIX}REPO_ONLY_VARS_VAR") == "v-new"


def test_repo_scope_selective_apply_only_secrets(repo_scope: ScopeTarget, tmp_path: Path) -> None:
    """Repo scope ``--only secrets`` updates secrets while keeping vars unchanged."""

    logger = ScenarioLogger("repo-selective-only-secrets")
    with isolated_scope(repo_scope, logger):
        baseline_env = tmp_path / "repo_only_secrets_baseline.env"
        write_combined_env(
            baseline_env,
            secret_lines=[f"{TEST_KEY_PREFIX}REPO_ONLY_SECRETS_SECRET_BASE=s-base"],
            var_lines=[f"{TEST_KEY_PREFIX}REPO_ONLY_SECRETS_VAR=v-old"],
        )
        run_cli(repo_scope, "apply", "--file", str(baseline_env), logger=logger, context="apply baseline")

        updated_env = tmp_path / "repo_only_secrets_updated.env"
        write_combined_env(
            updated_env,
            secret_lines=[
                f"{TEST_KEY_PREFIX}REPO_ONLY_SECRETS_SECRET_BASE=s-base-updated",
                f"{TEST_KEY_PREFIX}REPO_ONLY_SECRETS_SECRET_NEW=s-new",
            ],
            var_lines=[f"{TEST_KEY_PREFIX}REPO_ONLY_SECRETS_VAR=v-new"],
        )
        run_cli(
            repo_scope,
            "apply",
            "--file",
            str(updated_env),
            "--only",
            "secrets",
            logger=logger,
            context="apply updated with --only secrets",
        )

        secret_names, var_map = fetch_remote_state(repo_scope, logger=logger)
        assert f"{TEST_KEY_PREFIX}REPO_ONLY_SECRETS_SECRET_BASE" in secret_names
        assert f"{TEST_KEY_PREFIX}REPO_ONLY_SECRETS_SECRET_NEW" in secret_names
        assert var_map.get(f"{TEST_KEY_PREFIX}REPO_ONLY_SECRETS_VAR") == "v-old"


def test_repo_scope_cleanup_deletes_test_keys(repo_scope: ScopeTarget, tmp_path: Path) -> None:
    """Repo scope cleanup deletes only integration-prefixed keys."""

    logger = ScenarioLogger("repo-cleanup")
    with isolated_scope(repo_scope, logger):
        env_file = tmp_path / "repo_cleanup.env"
        write_combined_env(
            env_file,
            secret_lines=[f"{TEST_KEY_PREFIX}REPO_CLEANUP_SECRET=s1"],
            var_lines=[f"{TEST_KEY_PREFIX}REPO_CLEANUP_VAR=v1"],
        )
        run_cli(repo_scope, "apply", "--file", str(env_file), logger=logger, context="apply cleanup seed")

        wait_for_prefixed_keys(
            repo_scope,
            kind="secret",
            prefix=TEST_KEY_PREFIX,
            logger=logger,
            expect_present=True,
        )
        wait_for_prefixed_keys(
            repo_scope,
            kind="variable",
            prefix=TEST_KEY_PREFIX,
            logger=logger,
            expect_present=True,
        )

        clear_prefixed_keys(repo_scope, prefix=TEST_KEY_PREFIX, logger=logger)
        wait_for_prefixed_keys(
            repo_scope,
            kind="secret",
            prefix=TEST_KEY_PREFIX,
            logger=logger,
            expect_present=False,
        )
        wait_for_prefixed_keys(
            repo_scope,
            kind="variable",
            prefix=TEST_KEY_PREFIX,
            logger=logger,
            expect_present=False,
        )


def test_env_scope_create_apply_list_export_diff(env_scope: ScopeTarget, tmp_path: Path) -> None:
    """Environment scope happy path: create/apply/list/export/diff parity."""

    logger = ScenarioLogger("env-create-apply-list-export-diff")
    with isolated_scope(env_scope, logger):
        secret_file = tmp_path / "env_secret_file.txt"
        secret_file.write_text("env-secret-file-value", encoding="utf-8")

        var_file = tmp_path / "env_var_file.txt"
        var_file.write_text("env-var-file-value", encoding="utf-8")

        env_file = tmp_path / "env_create.env"
        write_combined_env(
            env_file,
            secret_lines=[
                f"{TEST_KEY_PREFIX}ENV_SECRET_SIMPLE=simple-secret",
                f"{TEST_KEY_PREFIX}ENV_SECRET_FILE=@file:{secret_file}",
            ],
            var_lines=[
                f"{TEST_KEY_PREFIX}ENV_VAR_SIMPLE=simple-var",
                f"{TEST_KEY_PREFIX}ENV_VAR_FILE=@file:{var_file}",
            ],
        )

        run_cli(env_scope, "apply", "--file", str(env_file), logger=logger, context="apply env baseline")

        diff_cp = wait_for_diff_exit_code(
            env_scope,
            env_file=env_file,
            expected_returncode=0,
            logger=logger,
            context="diff env baseline",
        )
        assert diff_cp.returncode == 0

        secret_names, var_map = fetch_remote_state(env_scope, logger=logger)
        assert f"{TEST_KEY_PREFIX}ENV_SECRET_SIMPLE" in secret_names
        assert f"{TEST_KEY_PREFIX}ENV_SECRET_FILE" in secret_names
        assert var_map.get(f"{TEST_KEY_PREFIX}ENV_VAR_SIMPLE") == "simple-var"
        assert var_map.get(f"{TEST_KEY_PREFIX}ENV_VAR_FILE") == "env-var-file-value"

        export_cp = run_cli(env_scope, "export", "--file", "-", logger=logger, context="export env baseline")
        assert f"{TEST_KEY_PREFIX}ENV_SECRET_SIMPLE=__REDACTED__" in export_cp.stdout
        assert f"{TEST_KEY_PREFIX}ENV_SECRET_FILE=__REDACTED__" in export_cp.stdout
        assert f"{TEST_KEY_PREFIX}ENV_VAR_SIMPLE=simple-var" in export_cp.stdout
        assert f"{TEST_KEY_PREFIX}ENV_VAR_FILE=env-var-file-value" in export_cp.stdout


def test_env_scope_update_cycle_and_diff_detection(env_scope: ScopeTarget, tmp_path: Path) -> None:
    """Environment scope update cycle: detect drift, apply, and converge."""

    logger = ScenarioLogger("env-update-cycle")
    with isolated_scope(env_scope, logger):
        baseline_env = tmp_path / "env_update_baseline.env"
        write_combined_env(
            baseline_env,
            secret_lines=[f"{TEST_KEY_PREFIX}ENV_SECRET_TRACKED=s-old"],
            var_lines=[f"{TEST_KEY_PREFIX}ENV_VAR_TRACKED=v1"],
        )
        run_cli(env_scope, "apply", "--file", str(baseline_env), logger=logger, context="apply env baseline")

        updated_env = tmp_path / "env_update_updated.env"
        write_combined_env(
            updated_env,
            secret_lines=[f"{TEST_KEY_PREFIX}ENV_SECRET_TRACKED=s-new"],
            var_lines=[f"{TEST_KEY_PREFIX}ENV_VAR_TRACKED=v2"],
        )

        drift_cp = run_cli(
            env_scope,
            "diff",
            "--file",
            str(updated_env),
            logger=logger,
            context="diff env updated before apply",
            check=False,
        )
        assert drift_cp.returncode == 2

        run_cli(env_scope, "apply", "--file", str(updated_env), logger=logger, context="apply env updated")
        settled_cp = wait_for_diff_exit_code(
            env_scope,
            env_file=updated_env,
            expected_returncode=0,
            logger=logger,
            context="diff env updated after apply",
        )
        assert settled_cp.returncode == 0


def test_env_scope_cleanup_deletes_test_keys(env_scope: ScopeTarget, tmp_path: Path) -> None:
    """Environment scope cleanup deletes only integration-prefixed keys."""

    logger = ScenarioLogger("env-cleanup")
    with isolated_scope(env_scope, logger):
        env_file = tmp_path / "env_cleanup.env"
        write_combined_env(
            env_file,
            secret_lines=[f"{TEST_KEY_PREFIX}ENV_CLEANUP_SECRET=s1"],
            var_lines=[f"{TEST_KEY_PREFIX}ENV_CLEANUP_VAR=v1"],
        )
        run_cli(env_scope, "apply", "--file", str(env_file), logger=logger, context="apply env cleanup seed")

        wait_for_prefixed_keys(
            env_scope,
            kind="secret",
            prefix=TEST_KEY_PREFIX,
            logger=logger,
            expect_present=True,
        )
        wait_for_prefixed_keys(
            env_scope,
            kind="variable",
            prefix=TEST_KEY_PREFIX,
            logger=logger,
            expect_present=True,
        )

        clear_prefixed_keys(env_scope, prefix=TEST_KEY_PREFIX, logger=logger)
        wait_for_prefixed_keys(
            env_scope,
            kind="secret",
            prefix=TEST_KEY_PREFIX,
            logger=logger,
            expect_present=False,
        )
        wait_for_prefixed_keys(
            env_scope,
            kind="variable",
            prefix=TEST_KEY_PREFIX,
            logger=logger,
            expect_present=False,
        )


def test_at_file_path_modes_relative_envvar_tilde(repo_scope: ScopeTarget, tmp_path: Path) -> None:
    """Validate ``@file`` path resolution for relative, env-var, and tilde modes."""

    logger = ScenarioLogger("at-file-path-modes")
    with isolated_scope(repo_scope, logger):
        relative_file = tmp_path / "relative.txt"
        relative_file.write_text("from-relative", encoding="utf-8")

        envvar_file = tmp_path / "envvar.txt"
        envvar_file.write_text("from-envvar", encoding="utf-8")

        # The tilde-mode case needs a file under HOME to exercise "~" expansion.
        # We remove it unconditionally in the finally block below.
        tilde_name = f".gha-env-sync-it-tilde-{uuid.uuid4().hex}.txt"
        tilde_file = Path.home() / tilde_name
        tilde_file.write_text("from-tilde", encoding="utf-8")

        env_file = tmp_path / "path_modes.env"
        write_combined_env(
            env_file,
            secret_lines=[],
            var_lines=[
                f"{TEST_KEY_PREFIX}PATHMODE_REL=@file:relative.txt",
                f"{TEST_KEY_PREFIX}PATHMODE_ENV=@file:$GHA_ENV_SYNC_IT_FILE",
                f"{TEST_KEY_PREFIX}PATHMODE_TILDE=@file:~/{tilde_name}",
            ],
        )

        try:
            env_overrides = {"GHA_ENV_SYNC_IT_FILE": str(envvar_file)}
            run_cli(
                repo_scope,
                "apply",
                "--file",
                str(env_file),
                logger=logger,
                context="apply path mode file references",
                env_overrides=env_overrides,
            )
            diff_cp = wait_for_diff_exit_code(
                repo_scope,
                env_file=env_file,
                expected_returncode=0,
                logger=logger,
                context="diff path mode file references",
                env_overrides=env_overrides,
            )
            assert diff_cp.returncode == 0

            _, var_map = fetch_remote_state(repo_scope, logger=logger)
            assert var_map.get(f"{TEST_KEY_PREFIX}PATHMODE_REL") == "from-relative"
            assert var_map.get(f"{TEST_KEY_PREFIX}PATHMODE_ENV") == "from-envvar"
            assert var_map.get(f"{TEST_KEY_PREFIX}PATHMODE_TILDE") == "from-tilde"
        finally:
            if tilde_file.exists():
                tilde_file.unlink()


def test_at_file_missing_path_fails_apply(repo_scope: ScopeTarget, tmp_path: Path) -> None:
    """Missing ``@file`` targets should fail ``apply`` with actionable messaging."""

    logger = ScenarioLogger("at-file-missing-path")
    with isolated_scope(repo_scope, logger):
        env_file = tmp_path / "missing_file.env"
        write_combined_env(
            env_file,
            secret_lines=[],
            var_lines=[f"{TEST_KEY_PREFIX}MISSING_FILE=@file:not-here.txt"],
        )

        cp = run_cli(
            repo_scope,
            "apply",
            "--file",
            str(env_file),
            logger=logger,
            context="apply with missing @file path",
            check=False,
        )
        assert cp.returncode == 1
        combined = (cp.stdout or "") + (cp.stderr or "")
        assert "@file path does not exist" in combined


@pytest.mark.integration
def test_repo_scope_value_fidelity_against_real_github(
    repo_scope: ScopeTarget, tmp_path: Path
) -> None:
    """The value GitHub stores must equal the value the parser produced.

    This is the scenario the rest of the matrix cannot prove: it only uses
    values like ``simple-var`` that survive any amount of re-parsing. These
    shapes are the ones that were silently corrupted when values were handed to
    ``gh ... set -f`` as a dotenv file for a second round of parsing.
    """

    logger = ScenarioLogger("repo-value-fidelity")
    with isolated_scope(repo_scope, logger):
        # key suffix -> (line written to the env file, value GitHub must hold)
        cases = {
            "HASH_QUOTED": ('"a # b"', "a # b"),
            "HASH_BARE": ("keep # dropped", "keep"),
            "HASH_TIGHT": ("a#b", "a#b"),
            "COLOR": ("#fff", "#fff"),
            "URL_FRAGMENT": ("http://example.com/x#frag", "http://example.com/x#frag"),
            "DOLLAR_BRACED": ("${HOME}", "${HOME}"),
            "DOLLAR_BARE": ("$HOME", "$HOME"),
            "NEWLINE": ('"l1\\nl2"', "l1\nl2"),
            "TAB": ('"a\\tb"', "a\tb"),
            "WINPATH": ("'C:\\tools\\new'", "C:\\tools\\new"),
            "JSON": ('\'{"effort": "low"}\'', '{"effort": "low"}'),
            "PADDED": ('"  padded  "', "  padded  "),
            "UNKNOWN_ESCAPE": ('"\\d+"', "\\d+"),
        }

        env_file = tmp_path / "value_fidelity.env"
        write_combined_env(
            env_file,
            secret_lines=[],
            var_lines=[f"{TEST_KEY_PREFIX}FIDELITY_{name}={line}" for name, (line, _) in cases.items()],
        )

        run_cli(
            repo_scope,
            "apply",
            "--file",
            str(env_file),
            logger=logger,
            context="apply value-fidelity matrix",
        )

        _, var_map = fetch_remote_state(repo_scope, logger=logger)

        mismatches = []
        for name, (_, expected) in cases.items():
            actual = var_map.get(f"{TEST_KEY_PREFIX}FIDELITY_{name}")
            if actual != expected:
                mismatches.append(f"  {name}: expected {expected!r}, GitHub holds {actual!r}")
        assert not mismatches, "values were altered in transit:\n" + "\n".join(mismatches)

        # Round trip: what export writes must diff clean against the same remote.
        exported = tmp_path / "value_fidelity.exported.env"
        run_cli(
            repo_scope,
            "export",
            "--file",
            str(exported),
            logger=logger,
            context="export value-fidelity matrix",
        )
        diff_cp = run_cli(
            repo_scope,
            "diff",
            "--file",
            str(exported),
            logger=logger,
            context="diff exported value-fidelity matrix",
            check=False,
        )
        # Secrets are name-only in diff and this scope has none, so any drift
        # here means an exported var did not parse back to its own value.
        assert diff_cp.returncode == 0, f"export output did not round-trip:\n{diff_cp.stdout}"
