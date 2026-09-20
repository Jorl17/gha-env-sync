"""Focused unit checks for low-level helper behavior.

These tests keep only high-signal edge cases that are hard to hit in integration
runs (error formatting, path normalization, and stdin wiring).
"""

import subprocess
from pathlib import Path

import pytest


def cp(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["cmd"], returncode=returncode, stdout=stdout, stderr=stderr)


def test_run_nonzero_check_false_returns_process(module, monkeypatch: pytest.MonkeyPatch) -> None:
    """``run(..., check=False)`` should return process info without raising."""

    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: cp(returncode=1, stderr="bad"))
    result = module.run(["gh", "whatever"], check=False)
    assert result.returncode == 1


def test_run_nonzero_check_true_non_redacted(module, monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-redacted failures should include the concrete command in error text."""

    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: cp(returncode=1, stderr="boom"))
    with pytest.raises(module.GhError, match="gh secret set"):
        module.run(["gh", "secret", "set", "TOKEN"], check=True, redacted=False)


def test_run_nonzero_check_true_redacted(module, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redacted failures should hide sensitive arguments in error text."""

    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: cp(returncode=1, stderr="boom"))
    with pytest.raises(module.GhError, match=r"Command failed \(gh \.\.\.\): boom"):
        module.run(["gh", "secret", "set", "TOKEN"], check=True, redacted=True)


def test_run_missing_command_raises_gh_error(module, monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing binaries should raise a friendly ``GhError``."""

    def fake_run(*args, **kwargs):
        raise FileNotFoundError("missing")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    with pytest.raises(module.GhError, match="Command not found"):
        module.run(["not-here"], check=True)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("owner/repo", "owner/repo"),
        ("https://github.com/owner/repo.git", "owner/repo"),
    ],
)
def test_normalize_repo_valid(module, value: str, expected: str) -> None:
    """Repo normalization should accept canonical short and URL forms."""

    assert module.normalize_repo(value) == expected


def test_normalize_repo_invalid(module) -> None:
    """Invalid repo shapes should fail fast with a clear message."""

    with pytest.raises(ValueError, match="Invalid repo format"):
        module.normalize_repo("owner/repo/extra")


def test_set_key_with_stdin_calls_run(module, monkeypatch: pytest.MonkeyPatch) -> None:
    """File-backed values must be sent over stdin via the expected gh command."""

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return cp(returncode=0)

    monkeypatch.setattr(module, "run", fake_run)
    target = module.RepoTarget(owner_repo="a/b", env_name="prod")
    value = module.InlineOrFileValue(content="line1\nline2", source="file:/tmp/key.pem")

    module.set_key_with_stdin(target, kind="secret", key="MY_KEY", value=value)

    assert len(calls) == 1
    cmd, kwargs = calls[0]
    assert cmd == ["gh", "secret", "set", "MY_KEY", "-R", "a/b", "--env", "prod"]
    assert kwargs["redacted"] is True
    assert kwargs["input_text"] == "line1\nline2"


def test_set_key_with_stdin_rejects_unknown_kind(module) -> None:
    """Only secret/variable kinds are supported."""

    target = module.RepoTarget(owner_repo="a/b", env_name=None)
    value = module.InlineOrFileValue(content="abc", source="file:/tmp/abc")
    with pytest.raises(ValueError, match="Unknown kind"):
        module.set_key_with_stdin(target, kind="invalid", key="KEY", value=value)


def test_ensure_environment_exists_probes_before_writing(module, monkeypatch) -> None:
    """An existing environment is left alone: a bodiless PUT would reset its protection rules."""

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(module, "run", fake_run)
    module.ensure_environment_exists(module.RepoTarget(owner_repo="o/r", env_name="prod"))

    assert calls == [["gh", "api", "repos/o/r/environments/prod"]]


def test_ensure_environment_exists_creates_when_missing(module, monkeypatch) -> None:
    """A missing environment is created with PUT."""

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        missing = "-X" not in cmd
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=1 if missing else 0,
            stdout="",
            stderr="gh: Not Found (HTTP 404)",
        )

    monkeypatch.setattr(module, "run", fake_run)
    module.ensure_environment_exists(module.RepoTarget(owner_repo="o/r", env_name="prod"))

    assert calls == [
        ["gh", "api", "repos/o/r/environments/prod"],
        ["gh", "api", "-X", "PUT", "repos/o/r/environments/prod"],
    ]


@pytest.mark.parametrize(
    "raw",
    [
        "git@github.com:Owner/Repo.git",
        "git@github.com:Owner/Repo",
        "ssh://git@github.com/Owner/Repo.git",
        "https://github.com/Owner/Repo",
        "https://github.com/Owner/Repo.git",
        "Owner/Repo",
    ],
)
def test_normalize_repo_accepts_every_github_url_form(module, raw: str) -> None:
    """The --repo flag must handle the SSH forms that auto-detection already handles."""

    assert module.normalize_repo(raw) == "Owner/Repo"


@pytest.mark.parametrize(
    "value",
    ["plain", "a # b", "  padded  ", "l1\nl2", "@file:/etc/passwd", "'quoted'", "", "#fff"],
)
def test_export_values_round_trip_through_the_parser(module, value: str) -> None:
    """Whatever export writes must parse back to the identical value."""

    line = f"K={module.serialize_dotenv_value(value)}"
    _, vars_simple, _, vars_file = module.parse_combined_env(
        f"# --- vars ---\n{line}\n", strict=True, base_dir=Path.cwd()
    )
    assert vars_file == {}, "an exported value must never be read back as an @file reference"
    assert vars_simple == {"K": value}


def test_run_fails_loudly_instead_of_hanging_forever(module, monkeypatch) -> None:
    """A wedged gh process must surface as an error, not an indefinite hang."""

    seen = {}

    def fake_subprocess_run(cmd, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(module.subprocess, "run", fake_subprocess_run)

    with pytest.raises(module.GhError, match="timed out"):
        module.run(["gh", "--version"])

    assert seen["timeout"], "run() must pass a timeout to subprocess.run"


def test_run_timeout_error_does_not_leak_a_redacted_command(module, monkeypatch) -> None:
    """A timeout on a secret-bearing call must not echo the command line."""

    def fake_subprocess_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(module.subprocess, "run", fake_subprocess_run)

    with pytest.raises(module.GhError) as excinfo:
        module.run(["gh", "secret", "set", "MY_KEY"], redacted=True, input_text="s3cr3t")

    assert "s3cr3t" not in str(excinfo.value)
    assert "MY_KEY" not in str(excinfo.value)


def test_write_export_output_warns_before_overwriting(module, tmp_path: Path, capsys) -> None:
    """Overwriting an existing env file should not be silent."""

    target = tmp_path / "existing.env"
    target.write_text("# --- vars ---\nOLD=1\n", encoding="utf-8")

    module.write_export_output("# --- vars ---\nNEW=2\n", str(target))

    captured = capsys.readouterr()
    assert "Overwrote" in captured.out or "overwrit" in captured.err.lower()
    assert target.read_text(encoding="utf-8") == "# --- vars ---\nNEW=2\n"


def test_write_export_output_is_quiet_for_a_new_file(module, tmp_path: Path, capsys) -> None:
    """Writing a fresh file needs no warning."""

    target = tmp_path / "fresh.env"
    module.write_export_output("x", str(target))

    assert "overwrit" not in capsys.readouterr().err.lower()


def gh_failure(message: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["gh"], returncode=1, stdout="", stderr=message)


def gh_success() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["gh"], returncode=0, stdout="ok", stderr="")


@pytest.mark.parametrize(
    "message",
    [
        "HTTP 500: Server Error",
        "HTTP 502: Bad Gateway",
        "HTTP 503: Service Unavailable",
        "HTTP 504: Gateway Timeout",
        "connection reset by peer",
        "the service is temporarily unavailable",
    ],
)
def test_run_retries_transient_github_failures(module, monkeypatch, message: str) -> None:
    """GitHub's Actions endpoints intermittently 5xx; a secrets sync must not give up."""

    attempts = []
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)

    def flaky(cmd, **kwargs):
        attempts.append(cmd)
        return gh_failure(message) if len(attempts) < 3 else gh_success()

    monkeypatch.setattr(module.subprocess, "run", flaky)

    cp = module.run(["gh", "variable", "set", "K"])

    assert cp.returncode == 0
    assert len(attempts) == 3


def test_run_does_not_retry_a_real_failure(module, monkeypatch) -> None:
    """A 404 or a bad argument is not going to fix itself."""

    attempts = []
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)

    def always_404(cmd, **kwargs):
        attempts.append(cmd)
        return gh_failure("HTTP 404: Not Found")

    monkeypatch.setattr(module.subprocess, "run", always_404)

    with pytest.raises(module.GhError, match="404"):
        module.run(["gh", "api", "repos/o/r/environments/nope"])

    assert len(attempts) == 1


def test_run_gives_up_after_the_attempt_cap(module, monkeypatch) -> None:
    """Retries are bounded, and the final error still reaches the caller."""

    attempts = []
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)

    def always_500(cmd, **kwargs):
        attempts.append(cmd)
        return gh_failure("HTTP 500: Server Error")

    monkeypatch.setattr(module.subprocess, "run", always_500)

    with pytest.raises(module.GhError, match="500"):
        module.run(["gh", "variable", "set", "K"])

    assert len(attempts) == module.GH_RETRY_ATTEMPTS


def test_run_backs_off_between_attempts(module, monkeypatch) -> None:
    """Hammering a struggling endpoint immediately would make things worse."""

    delays = []
    monkeypatch.setattr(module.time, "sleep", lambda seconds: delays.append(seconds))
    monkeypatch.setattr(
        module.subprocess, "run", lambda cmd, **kwargs: gh_failure("HTTP 500: Server Error")
    )

    with pytest.raises(module.GhError):
        module.run(["gh", "variable", "set", "K"])

    assert len(delays) == module.GH_RETRY_ATTEMPTS - 1
    assert delays == sorted(delays), "delays should not shrink"
    assert delays[0] >= module.GH_RETRY_BASE_DELAY_SECONDS


def test_run_does_not_retry_non_gh_commands(module, monkeypatch) -> None:
    """The retry policy is about GitHub's API, not about every subprocess."""

    attempts = []
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)

    def flaky(cmd, **kwargs):
        attempts.append(cmd)
        return gh_failure("HTTP 500: Server Error")

    monkeypatch.setattr(module.subprocess, "run", flaky)

    with pytest.raises(module.GhError):
        module.run(["git", "rev-parse", "HEAD"])

    assert len(attempts) == 1


def test_run_retry_does_not_leak_a_redacted_payload(module, monkeypatch) -> None:
    """Retrying a secret write must not start echoing the command or its value."""

    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        module.subprocess, "run", lambda cmd, **kwargs: gh_failure("HTTP 500: Server Error")
    )

    with pytest.raises(module.GhError) as excinfo:
        module.run(["gh", "secret", "set", "MY_KEY"], redacted=True, input_text="s3cr3t")

    assert "s3cr3t" not in str(excinfo.value)
    assert "MY_KEY" not in str(excinfo.value)


@pytest.mark.parametrize(
    "stderr",
    [
        "gh: Forbidden (HTTP 403)",
        "gh: Server Error (HTTP 500)",
        "gh: Bad credentials (HTTP 401)",
        "error connecting to api.github.com",
    ],
)
def test_ensure_environment_exists_refuses_to_create_on_a_non_404_probe(
    module, monkeypatch, stderr: str
) -> None:
    """A probe that fails for any reason other than absence must not trigger the PUT.

    The PUT is create-or-update with no body, so issuing it against an environment
    that does exist wipes its wait timer, reviewers and branch policy. Treating a
    403 or an exhausted-retry 5xx as "missing" would do exactly that.
    """

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr=stderr)

    monkeypatch.setattr(module, "run", fake_run)

    with pytest.raises(module.GhError):
        module.ensure_environment_exists(module.RepoTarget(owner_repo="o/r", env_name="prod"))

    assert calls == [["gh", "api", "repos/o/r/environments/prod"]], "must not have issued a PUT"


def test_ensure_environment_exists_error_names_the_environment(module, monkeypatch) -> None:
    """The refusal has to say which environment and why, or it is unactionable."""

    monkeypatch.setattr(
        module,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(
            args=cmd, returncode=1, stdout="", stderr="gh: Forbidden (HTTP 403)"
        ),
    )

    with pytest.raises(module.GhError) as excinfo:
        module.ensure_environment_exists(module.RepoTarget(owner_repo="o/r", env_name="prod"))

    message = str(excinfo.value)
    assert "prod" in message
    assert "403" in message


def test_apply_to_github_skips_empty_file_backed_values(
    module, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An @file pointing at an empty file is as unusable as an empty inline value."""

    target = module.RepoTarget(owner_repo="o/r", env_name=None)
    sent = []
    monkeypatch.setattr(
        module,
        "run",
        lambda cmd, **kwargs: sent.append(kwargs.get("input_text"))
        or subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(
        module,
        "set_key_with_stdin",
        lambda target, kind, key, value: sent.append((key, value.content)),
    )

    module.apply_to_github(
        target,
        {"S_FILE_EMPTY": module.InlineOrFileValue(content="", source="file:/tmp/empty")}
        and {},
        {},
        {"S_FILE_EMPTY": module.InlineOrFileValue(content="", source="file:/tmp/empty")},
        {"V_FILE_BLANK": module.InlineOrFileValue(content="  \n ", source="file:/tmp/blank")},
        dry_run=False,
    )

    assert sent == [], "empty file-backed values must not be sent"
    err = capsys.readouterr().err
    assert "S_FILE_EMPTY" in err
    assert "V_FILE_BLANK" in err
