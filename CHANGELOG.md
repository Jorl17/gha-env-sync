# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog.

## [0.2.0] - 2026-09-20

First release since 0.1.0. Several of these are data-loss fixes; upgrading is
strongly recommended.

### Breaking
- `--out` is now `--file`, for consistency across subcommands.
- Exit codes now distinguish failure from drift. The tool exits `1` when it
  fails (missing `gh`, not authenticated, unresolvable repo, parse error, gh
  call failed, bad CLI usage) and `2` only when `diff` ran successfully and
  found differences. Previously every one of those returned `2`, so CI could
  not tell a broken run from drift. Bad CLI usage exits `1` as well, rather
  than argparse's default of `2`.

### Fixed
- **Values are no longer corrupted on write.** Values were parsed as dotenv
  twice: once by this tool, then again by `gh` when it read the temporary env
  file passed to `gh ... set -f`. Once inline quotes were stripped locally, that
  second parse mangled anything containing a hash, a dollar or a backslash:
  `"a # b"` reached GitHub as `a`. Values are now single-quoted into a stream
  piped to `gh ... set -f -`, where dotenv guarantees a literal read — no
  interpolation, escapes, comments or trimming — and the few values that cannot
  be expressed that way (those holding a single quote or a newline) are sent on
  their own stdin, past any parser. Nothing is written to disk either way.
- **`apply --dry-run` no longer writes secrets to disk.** The temporary env
  file was created before the dry-run early return, materialising every secret
  in plaintext under `/tmp` and printing the path.
- **`export` output can no longer destroy your secrets.** `export` writes
  `__REDACTED__` for each secret, since GitHub never returns secret values.
  Nothing rejected that placeholder on the way back in, so the documented
  export-edit-apply loop replaced every secret with the literal string. `apply`
  now refuses such a file and points at `--only vars`.
- **`--env` commands no longer modify the environment.** `ensure_environment_exists`
  ran before dispatch on every command and issued a bodiless
  `PUT /repos/{o}/{r}/environments/{name}`. That endpoint is create-or-update,
  so naming an existing environment cleared its wait timer, required reviewers
  and deployment branch policy — and a typo created a stray environment, while
  anyone without admin could not run the read-only commands at all. The
  environment is now probed with `GET` and created only when genuinely missing,
  and only by a real `apply`.
- **`export` output now round-trips.** Values were written unquoted, so a
  variable whose value began with `@file:` was read back as a file reference,
  and values with newlines, padding or a trailing comment did not survive.
- `--repo` accepts the SSH URL forms (`git@github.com:OWNER/REPO.git`,
  `ssh://git@github.com/...`) that auto-detection already handled. They
  previously passed validation as a single `OWNER/REPO` pair and silently
  targeted the wrong thing.
- Duplicate keys within a section are rejected. Defining a key twice applied
  one value while `diff` compared the other, so `diff` reported drift after a
  clean apply.
- Keys using the reserved `GITHUB_` prefix are rejected at parse time instead
  of failing with a 422 from the API.
- Malformed-line parse errors no longer echo the line, which may be a secret.
- Every `gh` call has a 120s timeout, so a wedged process fails with a clear
  message instead of hanging indefinitely. The timeout message honours
  redaction.
- Transient GitHub failures are retried. The Actions secrets and variables
  endpoints intermittently return 5xx, which previously surfaced as a hard
  error part-way through an apply, leaving the file half-applied. Such
  failures are now attempted up to four times in total (three retries, backing
  off 1s, 2s, 4s), while a 404 or a rejected value fails immediately as before.
  The local 120s subprocess timeout is not retried, since each attempt would
  stall for the full timeout.
- Empty and whitespace-only values are skipped for secrets, not just variables.
- The CI badge linked to the repository's former name.

### Changed
- Inline values follow dotenv semantics properly. A value wrapped in matching
  quotes has that pair removed, so `'{"effort": "low"}'` no longer showed as
  permanent false drift in `diff`. Inside double quotes, escapes are translated
  (`"a\nb"` becomes two lines) rather than having their backslash deleted;
  unrecognised escapes such as `\d` pass through intact; single-quoted values
  stay literal. On an unquoted value a `#` preceded by whitespace starts a
  comment, while a `#` without one stays in the value, keeping hex colors and
  URL fragments safe.
- `export` output carries a generation timestamp.
- `export --file` says `Overwrote:` rather than `Wrote:` when it replaces an
  existing file.
- A failed `apply` names the key that failed rather than only the command.
- Added `SECURITY.md`. The 0.1.0 entry below used to claim it shipped; it never
  did, and that claim has been removed.

## [0.1.0] - 2026-02-15

First public release.

### Added
- Unix install script (`scripts/install.sh`).
- Unit/mocked test runner (`scripts/test.sh`) with coverage gate.
- Real GitHub integration runner (`scripts/test-integration.sh`).
- CI workflow for unit/mocked tests (`.github/workflows/ci.yml`).
- Contributor guide (`CONTRIBUTING.md`).
- Test strategy documentation (`tests/README.md`).

### Changed
- CLI metadata/version support and banner output.
- Integration test suite expanded to scenario-based real GitHub validation with step logs.
- Integration cleanup hardened for transient API behavior and eventual consistency.
- `@multiline` support removed; `@file` remains supported.
