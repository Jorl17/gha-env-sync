#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

if ! command -v uv >/dev/null 2>&1; then
  echo "Error: uv is required but not found on PATH." >&2
  echo "Install uv: https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
fi

# Keep cache location configurable for restricted environments.
if [[ -n "${UV_CACHE_DIR:-}" ]]; then
  echo "Using UV_CACHE_DIR=${UV_CACHE_DIR}"
fi

marker_arg_present=0
for arg in "$@"; do
  if [[ "$arg" == "-m" ]]; then
    marker_arg_present=1
    break
  fi
done

pytest_args=(
  -q
  --cov=gha_env_sync_module_under_test
  --cov-report=term-missing
  --cov-fail-under=80
)

# Integration tests need a real external repo and are opt-in by default.
if [[ "${GHA_ENV_SYNC_RUN_INTEGRATION:-0}" != "1" && "${marker_arg_present}" -eq 0 ]]; then
  pytest_args+=(-m "not integration")
fi

uv run --with pytest --with pytest-cov python -m pytest \
  "${pytest_args[@]}" \
  "$@"
