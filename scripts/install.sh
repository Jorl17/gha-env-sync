#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SOURCE_SCRIPT="${REPO_ROOT}/gha-env-sync.py"

# Override the destination with PREFIX, e.g. PREFIX=/usr/local/bin ./scripts/install.sh
TARGET_DIR="${PREFIX:-${HOME}/.local/bin}"
TARGET_SCRIPT="${TARGET_DIR}/gha-env-sync"

MODE="copy"
ACTION="install"
WITH_SKILL=0

SKILL_NAME="gha-env-sync"
SKILL_SOURCE="${REPO_ROOT}/skills/${SKILL_NAME}"

# Agent Skills is an open format, but it defines no install path -- each agent
# reads its own directory. These are the global paths for the agents this script
# knows about; only ones that already exist are touched, so we never create a
# config tree for an agent that is not installed.
SKILL_TARGET_DIRS=(
  "${HOME}/.agents/skills"
  "${HOME}/.claude/skills"
  "${HOME}/.codex/skills"
  "${HOME}/.cursor/skills"
  "${HOME}/.config/agents/skills"
)

usage() {
  cat <<'EOF'
Usage:
  ./scripts/install.sh [--link] [--skill] [--uninstall]

Options:
  --link       Symlink to the repo instead of copying. Edits to the working
               tree take effect immediately -- useful when developing, but the
               command breaks if the repo is moved or deleted.
  --skill      Also install the Agent Skill into every known agent skills
               directory that already exists on this machine.
  --uninstall  Remove a previously installed command (and the skill).
  -h, --help   Show this help.

Environment:
  PREFIX       Install directory (default: ~/.local/bin)

By default the script is copied, so the installed command is a stable snapshot
and keeps working if the repo moves. Re-run this script after pulling changes.

The skill is not installed unless you ask for it: installing a CLI should not
write into your agent configuration uninvited. For a managed install that also
handles updates and agents this script does not know about, use the skills CLI
instead: npx skills add Jorl17/gha-env-sync
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --link) MODE="link"; shift ;;
    --skill) WITH_SKILL=1; shift ;;
    --uninstall) ACTION="uninstall"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Error: unknown option '$1'" >&2; usage >&2; exit 1 ;;
  esac
done

if [[ "${ACTION}" == "uninstall" ]]; then
  if [[ -e "${TARGET_SCRIPT}" || -L "${TARGET_SCRIPT}" ]]; then
    rm -f "${TARGET_SCRIPT}"
    echo "Removed ${TARGET_SCRIPT}"
  else
    echo "Nothing to remove at ${TARGET_SCRIPT}"
  fi

  for dir in "${SKILL_TARGET_DIRS[@]}"; do
    target="${dir}/${SKILL_NAME}"
    if [[ -e "${target}" || -L "${target}" ]]; then
      rm -rf "${target}"
      echo "Removed ${target}"
    fi
  done
  exit 0
fi

# uv is required: the script's shebang runs it. gh is only needed at runtime,
# so a missing gh is a warning rather than a failed install.
if ! command -v uv >/dev/null 2>&1; then
  echo "Error: uv is required but not found on PATH." >&2
  echo "Install uv: https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
fi

if ! command -v gh >/dev/null 2>&1; then
  echo "Warning: GitHub CLI (gh) not found on PATH. Install it before running the tool:" >&2
  echo "  https://cli.github.com/" >&2
fi

if [[ ! -f "${SOURCE_SCRIPT}" ]]; then
  echo "Error: source script not found at ${SOURCE_SCRIPT}" >&2
  exit 1
fi

if [[ -e "${TARGET_SCRIPT}" || -L "${TARGET_SCRIPT}" ]]; then
  existing="$(grep -m1 '^APP_VERSION' "${TARGET_SCRIPT}" 2>/dev/null | cut -d'"' -f2 || true)"
  incoming="$(grep -m1 '^APP_VERSION' "${SOURCE_SCRIPT}" 2>/dev/null | cut -d'"' -f2 || true)"
  echo "Replacing existing install (${existing:-unknown} -> ${incoming:-unknown})"
fi

install -d "${TARGET_DIR}"

if [[ "${MODE}" == "link" ]]; then
  ln -sfn "${SOURCE_SCRIPT}" "${TARGET_SCRIPT}"
  echo "Linked ${TARGET_SCRIPT} -> ${SOURCE_SCRIPT}"
  echo "Note: tracks the working tree. Moving or deleting the repo breaks it."
else
  install -m 0755 "${SOURCE_SCRIPT}" "${TARGET_SCRIPT}"
  echo "Installed ${TARGET_SCRIPT}"
  echo "Note: this is a copy. Re-run this script after pulling changes."
fi

if [[ "${WITH_SKILL}" -eq 1 ]]; then
  if [[ ! -d "${SKILL_SOURCE}" ]]; then
    echo "Error: skill not found at ${SKILL_SOURCE}" >&2
    exit 1
  fi

  installed_any=0
  for dir in "${SKILL_TARGET_DIRS[@]}"; do
    [[ -d "${dir}" ]] || continue
    target="${dir}/${SKILL_NAME}"
    rm -rf "${target}"
    if [[ "${MODE}" == "link" ]]; then
      ln -sfn "${SKILL_SOURCE}" "${target}"
      echo "Linked skill ${target} -> ${SKILL_SOURCE}"
    else
      cp -R "${SKILL_SOURCE}" "${target}"
      echo "Installed skill ${target}"
    fi
    installed_any=1
  done

  if [[ "${installed_any}" -eq 0 ]]; then
    echo "No known agent skills directory found; skill not installed." >&2
    echo "Looked in: ${SKILL_TARGET_DIRS[*]}" >&2
    echo "Create one, or use: npx skills add Jorl17/gha-env-sync" >&2
  fi
fi

if [[ ":${PATH}:" != *":${TARGET_DIR}:"* ]]; then
  echo "PATH hint: add '${TARGET_DIR}' to your PATH."
  echo "Example: export PATH=\"${TARGET_DIR}:\$PATH\""
fi

echo "Run: gha-env-sync --version"
