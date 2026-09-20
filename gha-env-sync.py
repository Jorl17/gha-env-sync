#!/usr/bin/env -S uv run -q --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///

"""Sync GitHub Actions secrets/variables from a combined env-style file.

Execution model:
- This file is both source code and executable script via ``uv run --script``.
- ``main`` parses CLI args, validates runtime prereqs, resolves the repo target,
  then dispatches the selected command.

High-level flow:
- argparse -> validate gh auth/tooling -> resolve repo/environment target
- command dispatch -> parse local input (if needed) -> gh CLI calls

Security posture:
- All gh calls are funneled through ``run``.
- Operations that could include secret payloads use ``redacted=True`` so
  command-line arguments are not echoed in failure errors.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


APP_NAME = "gha-env-sync"
APP_VERSION = "0.2.0"
APP_VERSION_BANNER = f"{APP_NAME} {APP_VERSION}"


SECRETS_MARKERS = {
    "secrets": [
        r"^\s*#\s*---\s*secrets\s*---\s*$",
        r"^\s*#\s*GHA:SECRETS\s*$",
    ],
    "vars": [
        r"^\s*#\s*---\s*vars\s*---\s*$",
        r"^\s*#\s*GHA:VARS\s*$",
    ],
}

KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
FILE_PREFIX_RE = re.compile(r"^\s*@file:(.+)\s*$", flags=re.IGNORECASE)
UNSUPPORTED_INLINE_MULTILINE_SENTINEL = "@multiline"
SECRET_PLACEHOLDER = "__REDACTED__"

# POSIX-style codes. 1 means the tool failed; 2 means it ran fine and found
# differences. Keeping them distinct lets CI tell a broken run from drift.
EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_DIFFERENCES = 2

# Upper bound on any single gh invocation, so a wedged network call fails
# with a clear message instead of hanging a CI job indefinitely.
GH_TIMEOUT_SECONDS = 120

# GitHub's Actions secrets/variables endpoints intermittently return 5xx, and a
# tool whose whole job is writing them should not surrender a half-applied file
# to a blip. Retries are bounded, backed off, and only ever triggered by these
# signatures -- a 404 or a rejected argument is never retried.
GH_RETRY_ATTEMPTS = 4
GH_RETRY_BASE_DELAY_SECONDS = 1.0
TRANSIENT_GH_ERROR_MARKERS = (
    "http 500",
    "http 502",
    "http 503",
    "http 504",
    "server error",
    "timeout",
    "timed out",
    "connection reset",
    "temporarily unavailable",
)


DOUBLE_QUOTE_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "\\": "\\",
    '"': '"',
    "'": "'",
}

QUOTED_VALUE_RE = re.compile(r"^(?P<quote>['\"])(?P<body>.*)(?P=quote)\s*(?:#.*)?$", flags=re.DOTALL)
UNQUOTED_COMMENT_RE = re.compile(r"\s#")


def unescape_double_quoted(body: str) -> str:
    """Translate dotenv escape sequences inside a double-quoted value.

    Recognised escapes become the characters they name; anything else keeps its
    backslash, so a value such as ``"\\d+"`` survives as a usable regex.
    """

    return re.sub(
        r"\\(.)",
        lambda match: DOUBLE_QUOTE_ESCAPES.get(match.group(1), "\\" + match.group(1)),
        body,
        flags=re.DOTALL,
    )


def normalize_inline_value(value: str) -> str:
    """Apply dotenv-style normalization to an inline ``KEY=value`` value.

    A combined env file is meant to behave like a real ``.env`` file, so the
    same quoting rules apply here that ``.env`` consumers (python-dotenv,
    django-environ) apply on read:

    - Surrounding whitespace is trimmed.
    - A value fully wrapped in a matching pair of single or double quotes has
      exactly that outer pair removed. Without this, a value such as
      ``'{"effort": "low"}'`` would be pushed to GitHub with its literal quotes
      still attached.
    - Inside double quotes, escapes are translated (``\\n`` becomes a newline).
      Single quotes are literal, so a Windows path keeps its backslashes.
    - On an unquoted value, a ``#`` preceded by whitespace starts a comment. A
      ``#`` that is not preceded by whitespace is part of the value, which keeps
      hex colors and URL fragments intact.

    Values not wrapped in a matching pair (mismatched quotes, or only an
    interior quote) are returned unchanged apart from the whitespace trim.
    """

    trimmed = value.strip()

    quoted = QUOTED_VALUE_RE.match(trimmed)
    if quoted:
        body = quoted.group("body")
        if quoted.group("quote") == '"':
            return unescape_double_quoted(body)
        return body

    comment = UNQUOTED_COMMENT_RE.search(trimmed)
    if comment:
        trimmed = trimmed[: comment.start()]
    return trimmed.strip()


@dataclass(frozen=True)
class RepoTarget:
    """Remote scope where values will be read/applied.

    Attributes:
        owner_repo: GitHub repo in OWNER/REPO format.
        env_name: Optional GitHub Actions environment name.
    """

    owner_repo: str
    env_name: str | None


@dataclass(frozen=True)
class InlineOrFileValue:
    """Represents a file-backed payload with provenance metadata.

    Attributes:
        content: Exact text payload sent to GitHub.
        source: Human-readable source descriptor (for example ``file:/...``).
    """

    content: str
    source: str


ParsedEnv = tuple[
    dict[str, str],
    dict[str, str],
    dict[str, InlineOrFileValue],
    dict[str, InlineOrFileValue],
]


class GhError(RuntimeError):
    """Raised when a gh invocation or command prerequisite fails."""


def eprint(*args: object) -> None:
    """Print a message to stderr."""

    print(*args, file=sys.stderr)


def is_transient_gh_failure(cmd: list[str], cp: subprocess.CompletedProcess[str]) -> bool:
    """Return whether a finished gh call failed for a reason worth retrying.

    Only gh invocations qualify, and only when the output carries one of
    ``TRANSIENT_GH_ERROR_MARKERS``. Anything else -- a 404, a validation error,
    a bad flag -- will fail identically on a second attempt.
    """

    if not cmd or cmd[0] != "gh" or cp.returncode == 0:
        return False
    combined = f"{cp.stdout}\n{cp.stderr}".lower()
    return any(marker in combined for marker in TRANSIENT_GH_ERROR_MARKERS)


def run(
    cmd: list[str],
    *,
    check: bool = True,
    capture: bool = True,
    redacted: bool = False,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess command with consistent error semantics.

    Args:
        cmd: Command and args.
        check: Raise ``GhError`` when return code is non-zero.
        capture: Capture stdout/stderr when true.
        redacted: Hide full command in raised errors.
        input_text: Optional stdin payload.

    Returns:
        The completed process object.

    Raises:
        GhError: When the command is missing, times out, or exits non-zero with
            ``check=True``.
    """

    max_attempts = GH_RETRY_ATTEMPTS if cmd and cmd[0] == "gh" else 1

    for attempt in range(1, max_attempts + 1):
        try:
            cp = subprocess.run(
                cmd,
                text=True,
                input=input_text,
                capture_output=capture,
                check=False,
                timeout=GH_TIMEOUT_SECONDS,
            )
        except FileNotFoundError as ex:
            raise GhError(f"Command not found: {cmd[0]}") from ex
        except subprocess.TimeoutExpired as ex:
            # Deliberately not retried: another full timeout per attempt would
            # stall far longer than any caller expects.
            detail = cmd[0] if redacted else " ".join(map(shlex.quote, cmd))
            raise GhError(
                f"Command timed out after {GH_TIMEOUT_SECONDS}s ({detail}"
                + (" ..." if redacted else "")
                + ")"
            ) from ex

        if attempt == max_attempts or not is_transient_gh_failure(cmd, cp):
            break

        delay = GH_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1))
        target = cmd[0] if redacted else " ".join(map(shlex.quote, cmd))
        eprint(
            f"Transient failure from {target}"
            + (" ..." if redacted else "")
            + f"; retrying in {delay:.0f}s ({attempt}/{max_attempts - 1})"
        )
        time.sleep(delay)

    if check and cp.returncode != 0:
        msg = (cp.stderr or cp.stdout or "").strip() or f"exit code {cp.returncode}"
        if redacted:
            raise GhError(f"Command failed ({cmd[0]} ...): {msg}")
        raise GhError(f"Command failed ({' '.join(map(shlex.quote, cmd))}): {msg}")
    return cp


def install_hint_for_gh() -> str:
    """Return platform-specific install guidance for GitHub CLI."""

    plat = sys.platform
    if plat == "darwin":
        return "Install GitHub CLI (gh): brew install gh"
    if plat.startswith("linux"):
        return (
            "Install GitHub CLI (gh):\n"
            "  - Debian/Ubuntu: sudo apt-get update && sudo apt-get install -y gh\n"
            "  - Fedora: sudo dnf install -y gh\n"
            "  - Arch: sudo pacman -S github-cli\n"
            "  - Or see official instructions: https://cli.github.com/"
        )
    if plat.startswith("win"):
        return "Install GitHub CLI (gh): winget install --id GitHub.cli"
    return "Install GitHub CLI (gh): https://cli.github.com/"


def ensure_gh_or_fail() -> None:
    """Ensure ``gh`` is installed and available on PATH."""

    try:
        run(["gh", "--version"], check=True)
    except GhError as ex:
        raise GhError(
            "GitHub CLI 'gh' is not installed (this tool uses it).\n" + install_hint_for_gh()
        ) from ex


def ensure_logged_in_or_fail() -> None:
    """Ensure the active user is authenticated in GitHub CLI."""

    try:
        run(["gh", "auth", "status"], check=True)
    except GhError as ex:
        raise GhError("Not logged in to GitHub CLI. Run: gh auth login") from ex


def normalize_repo(repo: str) -> str:
    """Normalize OWNER/REPO and GitHub URL input to OWNER/REPO.

    Args:
        repo: OWNER/REPO or github URL.

    Returns:
        Normalized OWNER/REPO.

    Raises:
        ValueError: If input does not map to OWNER/REPO.
    """

    normalized = repo.strip()
    normalized = re.sub(r"^ssh://git@github\.com/", "", normalized)
    normalized = re.sub(r"^git@github\.com:", "", normalized)
    normalized = re.sub(r"^https?://github\.com/", "", normalized)
    normalized = normalized.removesuffix(".git")
    if "/" not in normalized or normalized.count("/") != 1:
        raise ValueError(
            f"Invalid repo format: {normalized!r}. Expected OWNER/REPO or GitHub URL."
        )
    return normalized


def git_exists() -> bool:
    """Return whether git is available on PATH."""

    cp = subprocess.run(["git", "--version"], text=True, capture_output=True)
    return cp.returncode == 0


def detect_repo_from_git_remote() -> str | None:
    """Resolve OWNER/REPO from ``git remote get-url origin`` if possible."""

    if not git_exists():
        return None

    cp = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        text=True,
        capture_output=True,
    )
    if cp.returncode != 0 or (cp.stdout or "").strip() != "true":
        return None

    cp2 = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        text=True,
        capture_output=True,
    )
    if cp2.returncode != 0:
        return None

    url = (cp2.stdout or "").strip().removesuffix(".git")
    match = re.search(r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/\s]+)$", url)
    if not match:
        return None
    return f"{match.group('owner')}/{match.group('repo')}"


def detect_repo_from_gh() -> str | None:
    """Resolve OWNER/REPO from ``gh repo view`` context."""

    cp = run(["gh", "repo", "view", "--json", "nameWithOwner"], check=False)
    if cp.returncode != 0:
        return None
    try:
        data = json.loads(cp.stdout or "{}")
    except json.JSONDecodeError:
        return None

    name_with_owner = data.get("nameWithOwner")
    if isinstance(name_with_owner, str) and "/" in name_with_owner:
        return name_with_owner
    return None


def detect_repo_or_fail(explicit_repo: str | None) -> str:
    """Resolve a target repo from explicit value, git, or gh context.

    Args:
        explicit_repo: User-provided repo argument, if any.

    Returns:
        OWNER/REPO string.

    Raises:
        ValueError: If no repository can be determined.
    """

    if explicit_repo:
        return normalize_repo(explicit_repo)

    repo = detect_repo_from_git_remote()
    if repo:
        return normalize_repo(repo)

    repo = detect_repo_from_gh()
    if repo:
        return normalize_repo(repo)

    missing_git_note = ""
    if not git_exists():
        missing_git_note = " (also: 'git' not found, so origin-remote auto-detect is unavailable)"

    raise ValueError(
        "Could not auto-detect repo. Pass --repo OWNER/REPO or run inside a git repo "
        "with an 'origin' remote." + missing_git_note
    )


def marker_match(line: str, section: str) -> bool:
    """Return whether a line matches one of the section marker patterns."""

    return any(re.match(pat, line, flags=re.IGNORECASE) for pat in SECRETS_MARKERS[section])


def gh_scope_args(target: RepoTarget) -> list[str]:
    """Build gh CLI scope flags for repo and optional environment."""

    args = ["-R", target.owner_repo]
    if target.env_name:
        args += ["--env", target.env_name]
    return args


def ensure_environment_exists(target: RepoTarget) -> None:
    """Create the environment scope if, and only if, it does not already exist.

    GitHub's environments endpoint is create-or-update, and a ``PUT`` with no
    body is an update that clears the environment's configuration -- wait timer,
    required reviewers and deployment branch policy. Probing with ``GET`` first
    means an existing ``production`` is never touched.

    Side Effects:
        Issues ``gh api -X PUT`` only when the probe reports the environment is
        missing.
    """

    if not target.env_name:
        return

    owner, repo = target.owner_repo.split("/", 1)
    endpoint = f"repos/{owner}/{repo}/environments/{target.env_name}"

    probe = run(["gh", "api", endpoint], check=False, redacted=True)
    if probe.returncode == 0:
        return

    # Only a genuine 404 means "absent". Treating any failure as absence would
    # fire the destructive PUT at an environment that exists but whose probe was
    # refused (403), unauthenticated (401), or still failing after retries (5xx).
    detail = f"{probe.stdout}\n{probe.stderr}".strip()
    if "404" not in detail:
        raise GhError(
            f"Could not determine whether environment {target.env_name!r} exists in "
            f"{target.owner_repo}: {detail or 'no output'}\n"
            "Refusing to create it, because creating is an update that would clear "
            "any wait timer, required reviewers and deployment branch policy."
        )

    run(["gh", "api", "-X", "PUT", endpoint], check=True, redacted=True)


def parse_combined_env(
    text: str,
    *,
    strict: bool = True,
    base_dir: Path | None = None,
) -> ParsedEnv:
    """Parse a combined secrets/vars env file into simple and file-backed maps.

    Supported sections:
    - ``# --- secrets ---`` or ``# GHA:SECRETS``
    - ``# --- vars ---`` or ``# GHA:VARS``

    File-backed provider:
    - ``KEY=@file:path`` (file loaded verbatim; supports ``~`` and ``$VARS``)

    Args:
        text: Raw file content.
        strict: Enforce marker and line validity rules.
        base_dir: Base path for resolving relative ``@file`` references.

    Returns:
        ``(secrets_simple, vars_simple, secrets_file, vars_file)``.

    Raises:
        ValueError: For malformed input in strict mode or missing file refs.
    """

    if base_dir is None:
        base_dir = Path.cwd()

    secrets_simple: dict[str, str] = {}
    vars_simple: dict[str, str] = {}
    secrets_file: dict[str, InlineOrFileValue] = {}
    vars_file: dict[str, InlineOrFileValue] = {}

    section: str | None = None
    seen_any_marker = False
    seen_keys: dict[tuple[str, str], int] = {}
    lines = text.splitlines()

    i = 0
    while i < len(lines):
        raw = lines[i]
        line = raw.rstrip("\r")

        if marker_match(line, "secrets"):
            section = "secrets"
            seen_any_marker = True
            i += 1
            continue
        if marker_match(line, "vars"):
            section = "vars"
            seen_any_marker = True
            i += 1
            continue

        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue

        if "=" not in line:
            if strict:
                raise ValueError(
                    f"Line {i+1}: expected KEY=VALUE. "
                    "The line is not echoed here in case it holds a secret."
                )
            i += 1
            continue

        key, val = line.split("=", 1)
        key = key.strip()

        if strict and not KEY_RE.match(key):
            raise ValueError(f"Line {i+1}: invalid key: {key!r}")

        if strict and key.upper().startswith("GITHUB_"):
            raise ValueError(
                f"Line {i+1}: {key!r} uses the reserved GITHUB_ prefix. "
                "GitHub rejects these names, so this would fail with a 422 on apply."
            )

        # Keyed by section so a key in both sections still gets the clearer
        # "appears in both secrets and vars" error raised below.
        if (section, key) in seen_keys:
            raise ValueError(
                f"Line {i+1}: duplicate key {key!r} in the {section} section "
                f"(first seen on line {seen_keys[(section, key)]}). "
                "Defining a key twice makes apply and diff disagree about its value."
            )
        seen_keys[(section, key)] = i + 1

        if section is None:
            if strict:
                raise ValueError(
                    f"Line {i+1}: entry before any section marker. "
                    f"Add '# --- secrets ---' or '# --- vars ---' first."
                )
            section = "vars"

        if val.strip() == UNSUPPORTED_INLINE_MULTILINE_SENTINEL:
            raise ValueError(
                f"Line {i+1}: '@multiline' is no longer supported. Use '@file:<path>' for multiline values."
            )

        file_match = FILE_PREFIX_RE.match(val)
        if file_match:
            file_ref = file_match.group(1).strip()
            expanded = os.path.expandvars(file_ref)
            file_path = Path(expanded).expanduser()
            if not file_path.is_absolute():
                file_path = (base_dir / file_path).expanduser().resolve()

            if not file_path.exists():
                raise ValueError(f"Line {i+1}: @file path does not exist: {file_path}")

            content = file_path.read_text(encoding="utf-8")
            target_file = secrets_file if section == "secrets" else vars_file
            target_file[key] = InlineOrFileValue(content=content, source=f"file:{file_path}")
            i += 1
            continue

        target_simple = secrets_simple if section == "secrets" else vars_simple
        target_simple[key] = normalize_inline_value(val)
        i += 1

    if strict and not seen_any_marker:
        raise ValueError("No section markers found. Add '# --- secrets ---' and/or '# --- vars ---'.")

    secrets_keys = set(secrets_simple) | set(secrets_file)
    vars_keys = set(vars_simple) | set(vars_file)
    overlap = secrets_keys & vars_keys
    if overlap:
        raise ValueError(f"Keys appear in both secrets and vars: {sorted(overlap)}")

    return secrets_simple, vars_simple, secrets_file, vars_file


def bulk_dotenv_line(key: str, value: str) -> str | None:
    """Render ``KEY=value`` for gh's dotenv reader, or ``None`` if it cannot be.

    ``gh <kind> set -f -`` sets every key in one process, which is what keeps
    apply fast, but it reads the stream with its own dotenv parser. A value is
    only safe to send that way if the parser is guaranteed to hand back exactly
    what went in.

    Single quoting gives that guarantee: in dotenv a single-quoted value is
    literal -- no ``${VAR}`` interpolation, no backslash escapes, no comment
    scanning, no whitespace trimming. The one thing it cannot contain is a
    single quote, which has no escape inside single quotes. Newlines are
    excluded too, since line splitting differs between parsers.

    Anything this returns ``None`` for is sent on its own stdin instead, where
    no parser is involved at all.
    """

    if not value or any(char in value for char in ("'", "\n", "\r")):
        return None
    return f"{key}='{value}'"


def serialize_dotenv_value(value: str) -> str:
    """Render a value so parsing it back yields the identical string.

    Export output is a combined env file, and users do re-apply it. Without
    quoting, a variable whose value happens to start with ``@file:`` would be
    read back as a file reference, and values with newlines, padding or a
    trailing ``#`` comment would not survive the round trip.
    """

    needs_quoting = (
        value != value.strip()
        or value.startswith(("'", '"'))
        or FILE_PREFIX_RE.match(value) is not None
        or UNQUOTED_COMMENT_RE.search(value) is not None
        or any(char in value for char in "\n\r\t")
    )
    if not needs_quoting:
        return value

    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def set_key_with_stdin(target: RepoTarget, kind: str, key: str, value: InlineOrFileValue) -> None:
    """Set one secret/variable by piping payload through stdin.

    Args:
        target: Repo/environment scope.
        kind: ``secret`` or ``variable``.
        key: Secret/variable key.
        value: Payload and provenance metadata.

    Raises:
        ValueError: If ``kind`` is not supported.
        GhError: If gh command fails.
    """

    if kind not in {"secret", "variable"}:
        raise ValueError(f"Unknown kind: {kind}")

    cmd = ["gh", kind, "set", key, *gh_scope_args(target)]
    run(cmd, check=True, redacted=True, input_text=value.content)


def gh_secret_list(target: RepoTarget) -> list[dict]:
    """List remote secrets for a target scope."""

    cp = run(
        ["gh", "secret", "list", *gh_scope_args(target), "--json", "name,updatedAt,visibility"],
        check=True,
    )
    return json.loads(cp.stdout or "[]")


def gh_variable_list(target: RepoTarget) -> list[dict]:
    """List remote variables for a target scope."""

    cp = run(
        ["gh", "variable", "list", *gh_scope_args(target), "--json", "name,value,updatedAt"],
        check=True,
    )
    return json.loads(cp.stdout or "[]")


def apply_to_github(
    target: RepoTarget,
    secrets_simple: dict[str, str],
    vars_simple: dict[str, str],
    secrets_from_file: dict[str, InlineOrFileValue],
    vars_from_file: dict[str, InlineOrFileValue],
    *,
    dry_run: bool,
) -> None:
    """Apply parsed env data to GitHub Actions secrets/variables.

    Every value is piped to gh on stdin, so the payload gh receives is exactly
    the payload this tool parsed. Writing values into a temporary env file for
    ``gh ... set -f`` would hand them to gh's own dotenv parser for a second
    round of quote, escape, comment and ``${VAR}`` processing, silently
    corrupting anything containing ``#``, ``$`` or a backslash -- and would put
    plaintext secrets on disk.

    Sending every key on its own stdin would be exact but slow: one gh process
    per key, plus a public-key fetch per secret, is around nine times slower
    than a single bulk call. So values the dotenv reader provably cannot alter
    are single-quoted into one batched call per kind, and only the rest -- those
    holding a single quote or a newline -- take a call of their own.

    Side Effects:
        - Executes gh commands (unless ``dry_run=True``).
        - Prints command preview in dry-run mode.
    """

    scope = gh_scope_args(target)

    # `export` writes SECRET_PLACEHOLDER for every secret, since GitHub never
    # returns secret values. Applying that file back would replace real secrets
    # with the placeholder text, so refuse before anything is written. Vars are
    # exported with their real values and may legitimately hold this string.
    redacted_keys = sorted(
        [key for key, value in secrets_simple.items() if value.strip() == SECRET_PLACEHOLDER]
        + [key for key, value in secrets_from_file.items() if value.content.strip() == SECRET_PLACEHOLDER]
    )
    if redacted_keys:
        raise ValueError(
            f"Refusing to apply: these secrets still hold the {SECRET_PLACEHOLDER} placeholder "
            f"written by 'export': {', '.join(redacted_keys)}.\n"
            "Export cannot recover secret values from GitHub. Replace them with real values, "
            "or use --only vars to apply just the variables."
        )

    # GitHub rejects empty values, for secrets as well as variables.
    skipped_empty = sorted(
        [
            key
            for mapping in (secrets_simple, vars_simple)
            for key, value in mapping.items()
            if not value.strip()
        ]
        + [
            key
            for mapping in (secrets_from_file, vars_from_file)
            for key, value in mapping.items()
            if not value.content.strip()
        ]
    )
    if skipped_empty:
        eprint(f"Warning: skipping empty values (GitHub rejects them): {', '.join(skipped_empty)}")

    ops: list[tuple[str, str, InlineOrFileValue]] = []
    for key, value in sorted(secrets_simple.items()):
        if value.strip():
            ops.append(("secret", key, InlineOrFileValue(content=value, source="inline")))
    for key, value in sorted(secrets_from_file.items()):
        if value.content.strip():
            ops.append(("secret", key, value))
    for key, value in sorted(vars_simple.items()):
        if value.strip():
            ops.append(("variable", key, InlineOrFileValue(content=value, source="inline")))
    for key, value in sorted(vars_from_file.items()):
        if value.content.strip():
            ops.append(("variable", key, value))

    # Batch everything the dotenv reader cannot alter into one call per kind,
    # and send the rest on their own stdin where no parser is involved.
    batches: dict[str, list[str]] = {"secret": [], "variable": []}
    per_key: list[tuple[str, str, InlineOrFileValue]] = []
    for kind, key, value in ops:
        line = bulk_dotenv_line(key, value.content)
        if line is None:
            per_key.append((kind, key, value))
        else:
            batches[kind].append(line)

    secret_count = sum(1 for kind, _, _ in ops if kind == "secret")
    var_count = len(ops) - secret_count
    where = f" (env: {target.env_name})" if target.env_name else " (repo-level)"

    if dry_run:
        print(f"Target: {target.owner_repo}{where}")
        print(f"Would set secrets: {secret_count}  vars: {var_count}")

        applicable_secrets = {k: v for k, v in secrets_simple.items() if v.strip()}
        applicable_vars = {k: v for k, v in vars_simple.items() if v.strip()}
        if applicable_secrets:
            print("Secrets (simple):", ", ".join(sorted(applicable_secrets)))
        if secrets_from_file:
            print("Secrets (@file):", ", ".join(sorted(secrets_from_file)))
        if applicable_vars:
            print("Vars (simple):", ", ".join(sorted(applicable_vars)))
        if vars_from_file:
            print("Vars (@file):", ", ".join(sorted(vars_from_file)))
        if skipped_empty:
            print("Skipped (empty):", ", ".join(skipped_empty))

        # Dry-run output mirrors the concrete gh commands that would execute.
        # Payloads are never printed: they are secrets.
        print("\nCommands:")
        for kind in ("secret", "variable"):
            if batches[kind]:
                preview = ["gh", kind, "set", "-f", "-", *scope]
                plural = "" if len(batches[kind]) == 1 else "s"
                print(
                    "  " + " ".join(map(shlex.quote, preview))
                    + f"  # stdin ({len(batches[kind])} value{plural})"
                )
        for kind, key, value in per_key:
            preview = ["gh", kind, "set", key, *scope]
            print("  " + " ".join(map(shlex.quote, preview)) + f"  # stdin ({value.source})")
        return

    if not ops:
        print(f"Nothing to apply to {target.owner_repo}{where}")
        return

    for kind in ("secret", "variable"):
        lines = batches[kind]
        if not lines:
            continue
        run(
            ["gh", kind, "set", "-f", "-", *scope],
            check=True,
            redacted=True,
            input_text="\n".join(lines) + "\n",
        )

    for kind, key, value in per_key:
        try:
            set_key_with_stdin(target, kind=kind, key=key, value=value)
        except GhError as ex:
            raise GhError(
                f"Failed to set {kind} {key!r} on {target.owner_repo}{where}: {ex}"
            ) from ex

    print(
        f"Applied secrets: {secret_count}  vars: {var_count} to {target.owner_repo}{where}"
    )


def export_from_github(target: RepoTarget, *, include_secret_placeholders: bool = True) -> str:
    """Export remote scope into combined env-file format."""

    secrets_remote = gh_secret_list(target)
    vars_remote = gh_variable_list(target)

    now = datetime.now(UTC).isoformat(timespec="seconds")
    lines: list[str] = [f"# Auto-generated by {APP_NAME} on {now}", "# --- secrets ---"]
    for item in sorted(secrets_remote, key=lambda row: row.get("name", "")):
        name = item.get("name", "")
        if name:
            lines.append(f"{name}={SECRET_PLACEHOLDER if include_secret_placeholders else ''}")

    lines.append("")
    lines.append("# --- vars ---")
    for item in sorted(vars_remote, key=lambda row: row.get("name", "")):
        name = item.get("name", "")
        if name:
            value = item.get("value", "")
            lines.append(f"{name}={serialize_dotenv_value(value)}")

    lines.append("")
    return "\n".join(lines)


def diff_local_vs_remote(
    target: RepoTarget,
    secrets_simple: dict[str, str],
    vars_simple: dict[str, str],
    secrets_file: dict[str, InlineOrFileValue],
    vars_file: dict[str, InlineOrFileValue],
) -> int:
    """Compare local desired state with remote GitHub Actions config.

    Secrets are compared by name only (GitHub does not return secret values).
    Variables are compared by both name and value.

    Returns:
        ``EXIT_OK`` when equivalent, ``EXIT_DIFFERENCES`` when they differ.

    Side Effects:
        Prints a human-readable diff summary.
    """

    secrets_remote = {item["name"] for item in gh_secret_list(target) if "name" in item}
    vars_remote_items = gh_variable_list(target)
    vars_remote = {item["name"]: item.get("value", "") for item in vars_remote_items if "name" in item}

    exit_code = EXIT_OK

    secrets_local_names = set(secrets_simple) | set(secrets_file)
    vars_local_names = set(vars_simple) | set(vars_file)

    missing_on_remote = sorted(secrets_local_names - secrets_remote)
    extra_on_remote = sorted(secrets_remote - secrets_local_names)
    if missing_on_remote or extra_on_remote:
        exit_code = EXIT_DIFFERENCES

    print("Secrets (name-based):")
    if missing_on_remote:
        print("  Missing on remote:", ", ".join(missing_on_remote))
    if extra_on_remote:
        print("  Extra on remote:", ", ".join(extra_on_remote))
    if not missing_on_remote and not extra_on_remote:
        print("  OK (same names)")

    remote_var_names = set(vars_remote.keys())
    missing_on_remote_vars = sorted(vars_local_names - remote_var_names)
    extra_on_remote_vars = sorted(remote_var_names - vars_local_names)

    changed_values: list[str] = []
    for key in sorted(set(vars_simple.keys()) & remote_var_names):
        if vars_simple.get(key, "") != vars_remote.get(key, ""):
            changed_values.append(key)
    for key in sorted(set(vars_file.keys()) & remote_var_names):
        if vars_file[key].content != vars_remote.get(key, ""):
            changed_values.append(key)

    if missing_on_remote_vars or extra_on_remote_vars or changed_values:
        exit_code = max(exit_code, EXIT_DIFFERENCES)

    print("\nVars:")
    if missing_on_remote_vars:
        print("  Missing on remote:", ", ".join(missing_on_remote_vars))
    if extra_on_remote_vars:
        print("  Extra on remote:", ", ".join(extra_on_remote_vars))
    if changed_values:
        print("  Different values:", ", ".join(sorted(set(changed_values))))
    if not missing_on_remote_vars and not extra_on_remote_vars and not changed_values:
        print("  OK (names and values match)")

    return exit_code


def cmd_list(target: RepoTarget, *, json_out: bool) -> None:
    """Print remote secrets/variables in text or JSON format."""

    secrets = gh_secret_list(target)
    vars_ = gh_variable_list(target)
    if json_out:
        print(
            json.dumps(
                {
                    "repo": target.owner_repo,
                    "env": target.env_name,
                    "secrets": secrets,
                    "vars": vars_,
                },
                indent=2,
            )
        )
        return

    print(
        f"Target: {target.owner_repo}"
        + (f" (env: {target.env_name})" if target.env_name else " (repo-level)")
    )
    print("\nSecrets:")
    for row in sorted(secrets, key=lambda item: item.get("name", "")):
        name = row.get("name", "")
        if name:
            print(f"  {name}")
    print("\nVars:")
    for row in sorted(vars_, key=lambda item: item.get("name", "")):
        name = row.get("name", "")
        if name:
            value = row.get("value", "")
            print(f"  {name}={value}")


class ExitOneArgumentParser(argparse.ArgumentParser):
    """Argument parser that exits 1 on usage errors.

    argparse exits 2 by default, which this CLI reserves for "differences
    found", so a mistyped flag would be indistinguishable from drift.
    """

    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        self.exit(EXIT_FAILURE, f"{self.prog}: error: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    """Build and return the top-level CLI parser."""

    examples = """Examples:
  # 1) Your env-scoped diff
  gha-env-sync --env playground diff --file .env.gh.playground

  # 2) Apply repo-level (no --env)
  gha-env-sync apply --file .env.gh

  # 3) Apply env-scoped
  gha-env-sync --env production apply --file .env.gh.production
"""

    parser = ExitOneArgumentParser(
        prog=APP_NAME,
        description="Sync combined .env (secrets + vars) with GitHub Actions via gh.",
        epilog=examples,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=APP_VERSION_BANNER,
    )
    parser.add_argument(
        "--repo",
        default=None,
        help=(
            "OWNER/REPO, an https://github.com/OWNER/REPO(.git) URL, or an SSH form "
            "(git@github.com:OWNER/REPO.git, ssh://git@github.com/OWNER/REPO). "
            "If omitted, auto-detect from git/gh."
        ),
    )
    parser.add_argument(
        "--env",
        default=None,
        help="GitHub Actions Environment name (optional; if omitted uses repo-level)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        default=True,
        help="Strict parsing (default on).",
    )
    parser.add_argument(
        "--non-strict",
        dest="strict",
        action="store_false",
        help="Non-strict parsing (defaults entries before marker to vars).",
    )

    sub = parser.add_subparsers(dest="cmd", required=True)

    sp_apply = sub.add_parser("apply", help="Apply local combined env file to GitHub (secrets + vars).")
    sp_apply.add_argument("--file", required=True, help="Path to combined env file")
    sp_apply.add_argument("--dry-run", action="store_true", help="Show what would happen, do not change remote")
    sp_apply.add_argument("--only", choices=["secrets", "vars", "all"], default="all")

    sp_list = sub.add_parser("list", help="List server-side secrets/vars (vars include values; secrets do not).")
    sp_list.add_argument("--json", action="store_true", help="JSON output")

    sp_export = sub.add_parser("export", help="Export server-side config into combined file format.")
    sp_export.add_argument("--file", default="-", help="Output path or '-' for stdout")
    sp_export.add_argument(
        "--no-secret-placeholders",
        action="store_true",
        help="Export secrets as NAME= (empty) instead of the placeholder",
    )

    sp_diff = sub.add_parser("diff", help="Diff local file vs remote (secrets name-based; vars name+value).")
    sp_diff.add_argument("--file", required=True, help="Path to combined env file")

    return parser


def load_env_from_file(path: Path, strict: bool) -> ParsedEnv:
    """Read and parse a combined env file from disk."""

    content = path.read_text(encoding="utf-8")
    return parse_combined_env(content, strict=strict, base_dir=path.parent.resolve())


def apply_only_filter(parsed_env: ParsedEnv, only: str) -> ParsedEnv:
    """Apply --only semantics while preserving tuple shape.

    Args:
        parsed_env: Parsed env tuple.
        only: One of ``all``, ``secrets``, ``vars``.

    Returns:
        Filtered parsed env tuple.
    """

    secrets_simple, vars_simple, secrets_file, vars_file = parsed_env
    if only == "secrets":
        return secrets_simple, {}, secrets_file, {}
    if only == "vars":
        return {}, vars_simple, {}, vars_file
    return parsed_env


def write_export_output(text: str, out: str) -> None:
    """Write export output to stdout or to a target file path."""

    if out == "-" or out.strip() == "":
        print(text)
        return

    path = Path(out)
    existed = path.exists()
    path.write_text(text, encoding="utf-8", newline="\n")
    print(f"{'Overwrote' if existed else 'Wrote'}: {out}")


def main() -> int:
    """CLI entrypoint.

    Returns:
        0 on success, 1 when the tool failed, 2 when differences were found.
    """

    parser = build_parser()
    args = parser.parse_args()

    try:
        ensure_gh_or_fail()
        ensure_logged_in_or_fail()
    except GhError as ex:
        eprint(str(ex))
        return EXIT_FAILURE

    try:
        owner_repo = detect_repo_or_fail(args.repo)
        target = RepoTarget(owner_repo=owner_repo, env_name=args.env)
    except ValueError as ex:
        eprint(str(ex))
        return EXIT_FAILURE

    try:
        if args.cmd == "list":
            cmd_list(target, json_out=args.json)
            return EXIT_OK

        if args.cmd == "export":
            text = export_from_github(
                target,
                include_secret_placeholders=not args.no_secret_placeholders,
            )
            write_export_output(text, args.file)
            return EXIT_OK

        if args.cmd in {"apply", "diff"}:
            env_path = Path(args.file)
            parsed_env = load_env_from_file(env_path, strict=args.strict)

            if args.cmd == "apply":
                # Only a real apply may create the environment. Read-only
                # commands and dry runs must leave the repo untouched.
                if not args.dry_run:
                    try:
                        ensure_environment_exists(target)
                    except GhError as ex:
                        eprint(
                            f"Failed to ensure environment exists: {target.env_name!r} "
                            f"in {target.owner_repo}.\n"
                            f"Underlying error: {ex}\n"
                            "Likely cause: missing repo permissions."
                        )
                        return EXIT_FAILURE

                filtered = apply_only_filter(parsed_env, args.only)
                apply_to_github(
                    target,
                    filtered[0],
                    filtered[1],
                    filtered[2],
                    filtered[3],
                    dry_run=args.dry_run,
                )
                return EXIT_OK

            return diff_local_vs_remote(
                target,
                parsed_env[0],
                parsed_env[1],
                parsed_env[2],
                parsed_env[3],
            )

        eprint("Unknown command")
        return EXIT_FAILURE

    except (ValueError, GhError) as ex:
        eprint(str(ex))
        return EXIT_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
