# gha-env-sync

[![CI](https://github.com/Jorl17/gha-env-sync/actions/workflows/ci.yml/badge.svg)](https://github.com/Jorl17/gha-env-sync/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**Problem**: You have a single `.env` on your local machine and want to sync it to GitHub Actions.
GitHub Actions separates secrets from variables, so there is no single place to put it.

**Solution**: `gha-env-sync` — one combined source file, both kinds synced.

**_(Disclaimer: Entirely vibe-coded, use at your own risk. If you are handling truly sensitive data, consider using `gh` directly.)_**

`gha-env-sync` syncs GitHub Actions Secrets (private) and Variables (non-secret)
from one combined `.env`-style file.

It is a single-file CLI that wraps the GitHub CLI (`gh`).

## A single .env file?!

Yes!

You add two marker sections to your combined file:
- `# --- secrets ---`
- `# --- vars ---`

and `gha-env-sync` handles the sync.

Use `@file:<path>` when you need multiline values without putting them inline in the combined file.

`gha-env-sync` also auto-detects the repository from the current git remote.

## Prerequisites

- [uv](https://docs.astral.sh/uv/)
- [GitHub CLI (`gh`)](https://cli.github.com/)

No Python needed: the script's [PEP 723](https://peps.python.org/pep-0723/)
header declares `requires-python = ">=3.11"` and no dependencies, and uv
resolves an interpreter for it. The shebang uses `env -S`, which requires
coreutils 8.30 or later; on older systems run `uv run gha-env-sync.py`.

Log in once:

```bash
gh auth login
```

<details>
<summary><strong>Install (Unix)</strong></summary>

### Install from repo clone

```bash
git clone https://github.com/Jorl17/gha-env-sync.git
cd gha-env-sync
./scripts/install.sh
```

That **copies** to `~/.local/bin/gha-env-sync`, so re-run it after pulling.

```bash
PREFIX=/usr/local/bin ./scripts/install.sh   # install somewhere else
./scripts/install.sh --link                  # symlink to the repo instead
./scripts/install.sh --uninstall             # remove it
```

`--link` tracks your working tree, so edits apply without reinstalling. It
breaks if you move the repo.

### Agent Skill

The repo ships an [Agent Skill](https://agentskills.io) at
`skills/gha-env-sync/`, so agents know how to drive the tool safely. Install it
with the skills CLI, which handles Claude Code, Codex, Cursor, Copilot, Gemini
CLI, Amp, Zed and others:

```bash
npx skills add Jorl17/gha-env-sync
```

No Node? The install script will do it, into whichever agent skill directories
already exist on your machine:

```bash
./scripts/install.sh --skill
```

It never creates a config directory for an agent you don't have, and
`--uninstall` removes the skill again. The skills CLI is still the better option
if you want updates managed for you.

### Manual install

```bash
install -d ~/.local/bin
install -m 0755 ./gha-env-sync.py ~/.local/bin/gha-env-sync
```

If needed, add `~/.local/bin` to your PATH.

</details>


## Commands

### Diff local file vs remote
Compares local `.env.gh.playground` file vs remote `playground` environment.
```bash
gha-env-sync --env playground diff --file .env.gh.playground
```

### Apply (dry-run first)

```bash
gha-env-sync --env playground apply --file .env.gh.playground --dry-run
gha-env-sync --env playground apply --file .env.gh.playground
```

### Apply repo-level (no `--env`)

```bash
gha-env-sync apply --file .env.gh
```

### List remote

```bash
gha-env-sync --env playground list
```

Secrets are listed by name only — GitHub never returns their values. Variables
are listed as `NAME=value`, so think before piping `list` into a CI log.

### Export remote to combined format

```bash
gha-env-sync --env playground export --file .env.gh.playground.exported
```

Exported **variables** carry their real values and can be re-applied.
Exported **secrets** cannot: GitHub never returns secret values, so they are
written as `NAME=__REDACTED__`. Applying such a file would replace every secret
with that literal string, so `apply` refuses it. To apply just the variables
from an export:

```bash
gha-env-sync apply --file .env.gh.exported --only vars
```

### Apply only one kind

`--only` limits what `apply` touches:

```bash
gha-env-sync apply --file .env.gh --only secrets
gha-env-sync apply --file .env.gh --only vars
```

## Does `apply` delete anything?

No. It creates and updates. A key you drop from your file stays on GitHub —
remove it yourself with `gh secret delete` / `gh variable delete`.

Only a real `apply` creates a missing `--env`. `list`, `export`, `diff` and
`apply --dry-run` never write to your repo, and an environment that already
exists is left alone, so its protection rules survive.

## Options

Before the subcommand:

| Flag | Meaning |
|------|---------|
| `--repo OWNER/REPO` | Target repo. Also accepts an HTTPS URL or the SSH forms (`git@github.com:OWNER/REPO.git`, `ssh://git@github.com/OWNER/REPO`). If omitted, resolved from the `origin` remote, then from `gh`. |
| `--env NAME` | Use an Actions Environment instead of repo level. |
| `--version` | Print the version banner and exit. |
| `--non-strict` | Skip malformed lines, allow non-conforming key names and the `GITHUB_` prefix, and treat entries before the first marker as vars. Strict is the default. |

Per subcommand:

| Flag | Where | Meaning |
|------|-------|---------|
| `--file PATH` | `apply`, `diff`, `export` | Combined env file. For `export`, `-` means stdout (the default). |
| `--dry-run` | `apply` | Show the `gh` commands it would run, change nothing. |
| `--only secrets\|vars\|all` | `apply` | Do just one kind. Defaults to `all`. |
| `--json` | `list` | Machine-readable output. |
| `--no-secret-placeholders` | `export` | Write `NAME=` for secrets instead of `NAME=__REDACTED__`. Neither one can be applied back over real secrets. |

## Exit codes

| Code | Meaning |
|------|---------|
| `0`  | Success, or `diff` found no differences |
| `1`  | Something broke — no `gh`, not logged in, no repo, bad file, failed `gh` call, bad usage |
| `2`  | `diff` worked and **found differences** |

Drift checks in CI use `diff`: `2` means drift, `1` means the check itself broke.

## Testing

```bash
./scripts/test.sh
```

Integration tests run against a real GitHub repository and are documented in
[tests/README.md](tests/README.md). Contributing details are in
[CONTRIBUTING.md](CONTRIBUTING.md).

## Combined env file format

Two marker sections. Every entry belongs to the marker above it:

- `# --- secrets ---` (alias: `# GHA:SECRETS`)
- `# --- vars ---` (alias: `# GHA:VARS`)

```dotenv
# --- secrets ---
DJANGO_SECRET_KEY=super-secret
PRIVATE_KEY=@file:~/keys/app-private-key.pem

# --- vars ---
DJANGO_SETTINGS_MODULE=config.settings.production
LOG_LEVEL=INFO
```

### Value rules

Inline values follow dotenv rules:

```dotenv
PLAIN=hello
QUOTED="hello"                  # -> hello
JSON='{"effort": "low"}'        # single quotes keep the inner ones
MULTILINE="line1\nline2"        # double-quoted escapes become real characters
WINPATH='C:\tools\new'          # single quotes keep backslashes literal
COMMENTED=keep # this is dropped
COLOR=#fff                      # no space before #, so it stays in the value
```

Use `@file:<path>` for multiline values rather than inlining them. The file is
read verbatim; `~` and `$VARS` are expanded in the path.

These are refused before anything is sent: a key defined twice in one section,
a key in both sections, and anything starting with `GITHUB_` (GitHub rejects
those itself). Empty values are skipped with a warning, including an `@file:` pointing at an
empty file, for the same reason.

Values reach GitHub byte-identical to what the rules above produce, and are
never written to disk.

Transient failures (5xx, server error, timeout, connection reset, temporarily
unavailable) are attempted up to four times in total — three retries, backing
off 1s, 2s, 4s. A 404, or a value GitHub rejects, fails on the first attempt.
Every `gh` call times out locally after 120s, and that timeout is not retried.

## This is utterly irresponsible!

Yes, it is.

## License

MIT. See `LICENSE`.
