---
name: gha-env-sync
description: Skill for agents that need to use gha-env-sync safely and correctly to sync GitHub Actions secrets/variables from one combined env file.
disable-model-invocation: true
argument-hint: "[--repo OWNER/REPO] [--env ENV] <command> [--file PATH] [--dry-run] [--only secrets|vars|all]"
---

# gha-env-sync skill

This skill exists to tell agents exactly how to use `gha-env-sync`.

Use it when an agent needs to diff/apply/list/export GitHub Actions secrets/variables from a combined env file.

## Purpose

- Audience: AI coding agents working in this repository.
- Goal: provide the authoritative operational rules for using this tool correctly.
- Non-goal: end-user onboarding (that belongs in `README.md`).

## Current behavior to follow

- Combined file uses two sections:
  - `# --- secrets ---` (alias: `# GHA:SECRETS`)
  - `# --- vars ---` (alias: `# GHA:VARS`)
- Values can be inline (`KEY=value`) or file-backed via `@file:<path>`.
- `apply` is additive/update behavior, not a prune/delete operation.
- `diff` semantics:
  - secrets: name-based only (GitHub never returns secret values)
  - vars: name + value
- `--version` prints the app name and version banner.

## Exit codes

- `0` — success, or `diff` found no differences.
- `1` — the tool failed (missing `gh`, not authenticated, unresolvable repo, parse error, gh call failed, bad CLI usage).
- `2` — `diff` ran successfully and found differences.

Never treat `2` as a failure, and never treat a non-zero exit as drift without checking which code it was.

## Value semantics

Inline values follow dotenv rules:

- A value wrapped in matching quotes has that pair removed.
- Inside double quotes, escapes are translated: `"a\nb"` becomes two lines. Use single quotes to keep backslashes literal, so a Windows path is `'C:\tools\new'`.
- On an unquoted value, a `#` preceded by whitespace starts a comment. `KEY=a # b` sets `a`. A `#` without preceding whitespace stays in the value, so `#fff` and `http://x/y#frag` are safe.
- Values reach GitHub byte-identical to what the parser produced. They are piped to `gh` on stdin and never written to a temp file.
- Empty and whitespace-only values are skipped with a warning, including `@file:` references to an empty file; GitHub rejects them.
- A key may not appear twice in one section, nor in both sections.
- The reserved `GITHUB_` prefix is rejected at parse time in strict mode (the default). `--non-strict` allows it, and GitHub then rejects it with a 422.

Use `@file:<path>` for multiline values rather than trying to encode them inline.

## Safety expectations for agents

- Prefer `diff` and `apply --dry-run` before real `apply`.
- Use explicit `--repo OWNER/REPO` in automation/non-interactive contexts. `--repo` accepts `OWNER/REPO`, an HTTPS URL, or the SSH forms (`git@github.com:OWNER/REPO.git`, `ssh://...`).
- Use `--env ENV_NAME` only when environment scope is intended. Only a real `apply` will create a missing environment; `list`, `export`, `diff` and `apply --dry-run` never write to the repo. An existing environment is never modified, so its protection rules are left alone.
- Never assume `apply` deletes keys that are absent from local config.
- **`export` output cannot be re-applied for secrets.** GitHub does not return secret values, so export writes `__REDACTED__` as a placeholder. `apply` refuses a file containing it rather than overwriting real secrets with that string. To apply only the variables from an export, use `--only vars`.

## Command templates

Diff:
```bash
gha-env-sync --repo OWNER/REPO [--env ENV] diff --file .env.gh
```

Dry run apply:
```bash
gha-env-sync --repo OWNER/REPO [--env ENV] apply --file .env.gh --dry-run
```

Apply:
```bash
gha-env-sync --repo OWNER/REPO [--env ENV] apply --file .env.gh
```

List:
```bash
gha-env-sync --repo OWNER/REPO [--env ENV] list --json
```

Export:
```bash
gha-env-sync --repo OWNER/REPO [--env ENV] export --file -
```

## Operational notes

- Tooling prerequisites: `uv` on PATH (the script's shebang is `uv run --script`) and `gh` installed and authenticated.
- Repo can auto-detect from git/gh, but explicit `--repo` is more reliable for agents.
- Every `gh` call has a 120s timeout, so a wedged call fails rather than hanging.
- Transient GitHub failures (5xx, server error, timeout/timed out, connection reset, temporarily unavailable) are attempted up to four times in total (three retries, backing off 1s, 2s, 4s). A 404 or a rejected value is never retried, and the local 120s subprocess timeout is not retried either. Do not add your own retry loop around the command.
- For real integration validation, use `./scripts/test-integration.sh OWNER/REPO [ENV_NAME]`. It writes to a real repository, so point it at a scratch repo, never a production one.
