# Test Suite Guide

This directory has two layers of testing:

- unit/mocked tests: fast, deterministic checks of parser/dispatch/helper behavior.
- integration tests: real GitHub end-to-end validation.

## Running tests

Unit/mocked (default):

```bash
./scripts/test.sh
```

Real integration:

```bash
./scripts/test-integration.sh OWNER/REPO [ENV_NAME]
```

## Test file map

- `tests/test_parse_env.py`: combined env parsing and `@file` path behavior.
- `tests/test_sync_operations.py`: apply/export/diff/list core behavior with mocks.
- `tests/test_main_dispatch.py`: command dispatch and key exit-code paths.
- `tests/test_repo_detection.py`: repo auto-detection fallback logic.
- `tests/test_core_helpers.py`: low-level helper/error edge cases.
- `tests/test_cli_metadata.py`: `--version` / `--help` CLI surface checks.
- `tests/test_real_repo_integration.py`: real GitHub scenario matrix.
- `tests/conftest.py`, `tests/_gha_module.py`: load the hyphenated script as an
  importable module; the integration suite imports the tool's own retry rules
  from it rather than restating them.

## Running the integration suite

These tests use a real external GitHub repository and real `gh` calls.

Run integration tests (recommended wrapper):

```bash
./scripts/test-integration.sh OWNER/REPO [ENV_NAME]
```

What the wrapper does:
- configures `GHA_ENV_SYNC_TEST_REPO`, `GHA_ENV_SYNC_TEST_ENV`, and `GHA_ENV_SYNC_RUN_INTEGRATION=1`
- runs pytest in verbose mode (`-vv -s -rA`) so each step/command log is visible
- runs only `tests/test_real_repo_integration.py`

Scenario matrix covered:
- repo scope lifecycle: create/apply, list, export, diff, update cycle, selective apply (`--only vars`, `--only secrets`), cleanup delete
- environment scope lifecycle: create/apply, list, export, diff, update cycle, cleanup delete
- `@file` resolution modes: relative path, `$ENV_VAR`, and `~`
- missing `@file` path failure with actionable error
- value fidelity: hashes, `${VAR}`, newlines, tabs, backslashes, padding, JSON
  and hex colors are read back from GitHub and compared, plus an export
  round-trip

Preflight/failure behavior:
- if `gh auth status` fails, integration tests fail immediately
- if `gh repo view OWNER/REPO` fails, tests fail with: create repo externally, then rerun

Interpreting output:
- expected result is multiple passed tests (not a single happy-path test)
- each scenario prints step-by-step logs showing commands and outcomes

Advanced/manual equivalent:

```bash
GHA_ENV_SYNC_RUN_INTEGRATION=1 \
GHA_ENV_SYNC_TEST_REPO=OWNER/REPO \
GHA_ENV_SYNC_TEST_ENV=gha-env-sync-it \
uv run --with pytest --with pytest-cov python -m pytest \
  -vv -s -rA -m integration --no-cov \
  tests/test_real_repo_integration.py
```

## Integration safety boundaries

Integration tests in `tests/test_real_repo_integration.py` enforce strict guardrails:

- remote writes/deletes are limited to keys with prefix `GHA_ENV_SYNC_IT_`.
- cleanup helpers operate only on prefixed keys.
- non-prefixed secrets/vars are never targeted.

## Integration side effects

Local side effects:
- temporary combined env files under pytest `tmp_path`.
- temporary `~` file for tilde path-resolution test, deleted in `finally`.

Remote side effects:
- creation/update/deletion of test keys under repo/environment scope.
- optional creation/ensure of the test GitHub Actions environment.

## Common failure modes

- `gh auth status` fails:
  - re-authenticate via `gh auth login`.
- repo missing/inaccessible:
  - create/fix permissions, then rerun.
- transient GitHub API errors:
  - tests include retry/poll logic, but rerun may still be needed.
