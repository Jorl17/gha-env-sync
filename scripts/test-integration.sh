#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

usage() {
  cat <<'EOF'
Usage:
  ./scripts/test-integration.sh OWNER/REPO [ENV_NAME]

Examples:
  ./scripts/test-integration.sh Jorl17/gh-env-sync-test
  ./scripts/test-integration.sh Jorl17/gh-env-sync-test gha-env-sync-it-env
EOF
}

if [[ $# -lt 1 || $# -gt 2 ]]; then
  usage >&2
  exit 1
fi

repo="$1"
env_name="${2:-gha-env-sync-it}"

if [[ "${repo}" != */* ]]; then
  echo "Error: expected OWNER/REPO, got '${repo}'" >&2
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "Error: uv is required but not found on PATH." >&2
  echo "Install uv: https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
fi

echo "[integration] target repo: ${repo}"
echo "[integration] target env: ${env_name}"
echo "[integration] running real GitHub scenario matrix with verbose logs..."

GHA_ENV_SYNC_RUN_INTEGRATION=1 \
GHA_ENV_SYNC_TEST_REPO="${repo}" \
GHA_ENV_SYNC_TEST_ENV="${env_name}" \
uv run --with pytest --with pytest-cov python -m pytest \
  -vv -s -rA -m integration --no-cov \
  tests/test_real_repo_integration.py
