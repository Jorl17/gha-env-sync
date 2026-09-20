# Security Policy

`gha-env-sync` handles GitHub Actions secrets. This document describes what it
does with them and how to report a problem.

## Reporting a vulnerability

Please report security issues privately through
[GitHub Security Advisories](https://github.com/Jorl17/gha-env-sync/security/advisories/new)
rather than opening a public issue.

This is a personal project with no service-level commitment. Expect a
best-effort response.

## How values are handled

- **Secrets never touch disk.** Every value is piped to `gh` on stdin. Earlier
  versions wrote them into a temporary env file for `gh ... set -f`, including
  during `--dry-run`; that path has been removed.
- **Values are never printed.** `--dry-run` shows the `gh` commands that would
  run and the source of each payload, never the payload itself.
- **Command lines are redacted on failure.** Calls that carry secret payloads
  raise errors naming only the program, not the full argument list. Timeout
  errors follow the same rule.
- **Malformed lines are not echoed.** A parse error reports the line number and
  key, never the line contents, which may be a secret.
- **Retries are bounded and never widen disclosure.** A retried call reuses
  the same redaction, so a failing secret write still reports only the
  program name.
- **Transport is `gh`.** This tool shells out to the authenticated GitHub CLI
  and holds no credentials of its own. `gh` encrypts secret values locally
  before sending them to GitHub.

## What the tool will and will not do

- `apply` is additive. It creates and updates; it never deletes a secret or
  variable that is absent from your local file.
- `export` writes `__REDACTED__` for every secret by default, or an empty value
  under `--no-secret-placeholders`, because GitHub does not return secret
  values. `apply` refuses any file still containing that
  placeholder, so an export cannot be round-tripped into overwriting your real
  secrets.
- Only a real `apply` creates a missing GitHub Actions environment. `list`,
  `export`, `diff` and `apply --dry-run` never write to the repository, and an
  environment that already exists is never modified, so its wait timer,
  required reviewers and deployment branch policy are left intact.

## Your responsibilities

- Keep your combined env file out of version control. The shipped `.gitignore`
  covers `.env`, `.env.*`, `*.pem` and `*.key`.
- Point `./scripts/test-integration.sh` at a scratch repository. It writes real
  secrets and variables to whatever repository you name.
- Review `--dry-run` output before applying to an environment that matters.
