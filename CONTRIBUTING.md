# Contributing

Thanks for helping improve `gha-env-sync`.

This project is intentionally small and practical. Contributions are welcome if they keep behavior clear, testable, and safe.

## Local setup (uv-first)

Requirements are the same two tools the README lists: `uv` and `gh`. You do not
need a system Python — uv provisions an interpreter from the script's PEP 723
header.

Clone and verify the script runs:

```bash
git clone https://github.com/Jorl17/gha-env-sync.git
cd gha-env-sync
./gha-env-sync.py --help
```

Notes:
- This project is self-contained (`gha-env-sync.py` is an executable `uv run --script` file).
- For IDE autocomplete/intellisense, create a local virtual environment with `uv`:

```bash
uv venv .venv
uv pip install --python .venv/bin/python pytest pytest-cov
source .venv/bin/activate
```

- In your IDE, use interpreter: `.venv/bin/python`

## Running tests

Unit/mocked suite (default):

```bash
./scripts/test.sh
```

That provisions `pytest` and `pytest-cov` via uv, skips integration tests, and
fails if coverage drops below 80%.

Run a subset:

```bash
./scripts/test.sh --cov-fail-under=0 -k parse
./scripts/test.sh --cov-fail-under=0 tests/test_sync_operations.py
```

If your environment restricts `~/.cache/uv`:

```bash
UV_CACHE_DIR=/tmp/uv-cache ./scripts/test.sh
```

## Integration tests (real GitHub)

Integration tests are opt-in and mutate only keys prefixed with `GHA_ENV_SYNC_IT_`.

```bash
./scripts/test-integration.sh OWNER/REPO [ENV_NAME]
```

Before running:
- Make sure the target repo exists.
- Make sure `gh auth status` is healthy.

## Scope expectations

When submitting PRs:
- Keep CLI behavior stable unless the change is intentional and documented.
- Prefer straightforward code over clever abstractions.
- Add or update tests for behavior changes.
- Keep security guardrails in mind (`.env*`, key files, and token leaks).
- Let's be real: this was vibecoded, and it's a personal project. I have no real expectations on PRs and give no guarantees either.

## Pull request checklist

- [ ] `./scripts/test.sh` passes locally.
- [ ] If touching integration behavior, run `./scripts/test-integration.sh ...`.
- [ ] Docs updated when behavior/flags/output change.
- [ ] No secrets or private key material in git diff.

## Codebase layout

Main implementation lives in `gha-env-sync.py`.

Key responsibilities:
- `build_parser`, `main`: CLI surface, command dispatch, exit codes
- `parse_combined_env`: parse the combined format
- `normalize_inline_value`: dotenv quoting, escapes and comment rules
- `bulk_dotenv_line`: render a value gh's reader cannot alter, or refuse it
- `serialize_dotenv_value`: render export output so it parses back identically
- `apply_to_github`: batch what can be batched, send the rest per key
- `export_from_github`, `diff_local_vs_remote`, `cmd_list`: read-side commands
- `run`, `is_transient_gh_failure`: every gh call, with timeout and retry
- `ensure_environment_exists`: GET, then create only when genuinely missing
- repo detection: `detect_repo_from_git_remote`, `detect_repo_from_gh`, `detect_repo_or_fail`
