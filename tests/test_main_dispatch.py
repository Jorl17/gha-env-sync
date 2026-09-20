"""Focused unit checks for ``main`` command dispatch.

These tests intentionally keep scope narrow. We rely on real integration tests for
end-to-end behavior, and keep this file for fast checks of critical control-flow
and exit-code behavior.
"""

from pathlib import Path

import pytest


def run_main(module, monkeypatch: pytest.MonkeyPatch, args: list[str]) -> int:
    """Invoke ``main`` with a synthetic argv."""

    monkeypatch.setattr(module.sys, "argv", [module.APP_NAME, *args])
    return module.main()


def patch_runtime_ok(module, monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch external prerequisites so dispatch can be tested in isolation."""

    monkeypatch.setattr(module, "ensure_gh_or_fail", lambda: None)
    monkeypatch.setattr(module, "ensure_logged_in_or_fail", lambda: None)
    monkeypatch.setattr(module, "detect_repo_or_fail", lambda explicit: "owner/repo")
    monkeypatch.setattr(module, "ensure_environment_exists", lambda target: None)


def test_main_returns_1_on_prereq_failure(
    module,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Prerequisite GH errors are failures (1), not drift (2)."""

    monkeypatch.setattr(module, "ensure_gh_or_fail", lambda: (_ for _ in ()).throw(module.GhError("gh missing")))

    rc = run_main(module, monkeypatch, ["list"])
    assert rc == 1
    assert "gh missing" in capsys.readouterr().err


def test_main_list_dispatches_with_target(module, monkeypatch: pytest.MonkeyPatch) -> None:
    """``list --json`` should dispatch once with resolved repo/env target."""

    patch_runtime_ok(module, monkeypatch)
    calls = []

    def fake_cmd_list(target, json_out):
        calls.append((target.owner_repo, target.env_name, json_out))

    monkeypatch.setattr(module, "cmd_list", fake_cmd_list)

    rc = run_main(module, monkeypatch, ["--env", "prod", "list", "--json"])
    assert rc == 0
    assert calls == [("owner/repo", "prod", True)]


@pytest.mark.parametrize(
    ("only", "expect_secrets", "expect_vars"),
    [
        ("secrets", True, False),
        ("vars", False, True),
        ("all", True, True),
    ],
)
def test_main_apply_respects_only_filter(
    module,
    monkeypatch: pytest.MonkeyPatch,
    only: str,
    expect_secrets: bool,
    expect_vars: bool,
) -> None:
    """``apply --only`` should forward only the selected payload categories."""

    patch_runtime_ok(module, monkeypatch)
    monkeypatch.setattr(
        module,
        "load_env_from_file",
        lambda path, strict: (
            {"S": "1"},
            {"V": "2"},
            {"SM": module.InlineOrFileValue(content="s", source="file:/tmp/s")},
            {"VM": module.InlineOrFileValue(content="v", source="file:/tmp/v")},
        ),
    )

    calls = []

    def fake_apply(target, secrets_simple, vars_simple, secrets_file, vars_file, dry_run):
        calls.append((secrets_simple, vars_simple, secrets_file, vars_file, dry_run))

    monkeypatch.setattr(module, "apply_to_github", fake_apply)

    rc = run_main(module, monkeypatch, ["apply", "--file", "dummy.env", "--only", only])
    assert rc == 0
    assert len(calls) == 1

    secrets_simple, vars_simple, secrets_file, vars_file, dry_run = calls[0]
    assert bool(secrets_simple) is expect_secrets
    assert bool(secrets_file) is expect_secrets
    assert bool(vars_simple) is expect_vars
    assert bool(vars_file) is expect_vars
    assert dry_run is False


def test_main_diff_propagates_return_code(module, monkeypatch: pytest.MonkeyPatch) -> None:
    """Diff command should return the exact diff exit code."""

    patch_runtime_ok(module, monkeypatch)
    monkeypatch.setattr(module, "load_env_from_file", lambda path, strict: ({}, {}, {}, {}))
    monkeypatch.setattr(module, "diff_local_vs_remote", lambda target, ss, vs, sm, vm: 2)

    rc = run_main(module, monkeypatch, ["diff", "--file", "dummy.env"])
    assert rc == 2


def test_main_returns_1_on_apply_gh_error(
    module,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Command-level GH errors are failures (1), so CI can tell them from drift."""

    patch_runtime_ok(module, monkeypatch)
    monkeypatch.setattr(module, "load_env_from_file", lambda path, strict: ({}, {}, {}, {}))
    monkeypatch.setattr(
        module,
        "apply_to_github",
        lambda *args, **kwargs: (_ for _ in ()).throw(module.GhError("command failed")),
    )

    rc = run_main(module, monkeypatch, ["apply", "--file", "dummy.env"])
    assert rc == 1
    assert "command failed" in capsys.readouterr().err


def test_main_export_writes_to_file(module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Export output should be written when ``--file`` points to a file path."""

    patch_runtime_ok(module, monkeypatch)
    monkeypatch.setattr(module, "export_from_github", lambda target, include_secret_placeholders: "DATA")

    output_file = tmp_path / "out.env"
    rc = run_main(module, monkeypatch, ["export", "--file", str(output_file)])
    assert rc == 0
    assert output_file.read_text(encoding="utf-8") == "DATA"


def env_tracking_patch(module, monkeypatch: pytest.MonkeyPatch) -> list:
    """Patch prerequisites but record every ensure_environment_exists call."""

    calls: list = []
    monkeypatch.setattr(module, "ensure_gh_or_fail", lambda: None)
    monkeypatch.setattr(module, "ensure_logged_in_or_fail", lambda: None)
    monkeypatch.setattr(module, "detect_repo_or_fail", lambda explicit: "owner/repo")
    monkeypatch.setattr(module, "ensure_environment_exists", lambda target: calls.append(target))
    return calls


def test_main_read_only_commands_never_create_the_environment(
    module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """list/export/diff must not mutate the repo just by naming an environment."""

    monkeypatch.setattr(module, "cmd_list", lambda target, json_out: None)
    monkeypatch.setattr(module, "export_from_github", lambda target, **kwargs: "")
    monkeypatch.setattr(module, "write_export_output", lambda text, out: None)
    monkeypatch.setattr(module, "diff_local_vs_remote", lambda *args: 0)

    env_file = tmp_path / ".env.gh"
    env_file.write_text("# --- vars ---\nV1=x\n", encoding="utf-8")

    for argv in (
        ["--env", "prod", "list"],
        ["--env", "prod", "export", "--file", "-"],
        ["--env", "prod", "diff", "--file", str(env_file)],
    ):
        calls = env_tracking_patch(module, monkeypatch)
        assert run_main(module, monkeypatch, argv) == 0
        assert calls == [], f"{argv[2]} must not touch the environment"


def test_main_apply_dry_run_never_creates_the_environment(
    module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A dry run is a preview and must not write to GitHub."""

    calls = env_tracking_patch(module, monkeypatch)
    monkeypatch.setattr(module, "apply_to_github", lambda *args, **kwargs: None)

    env_file = tmp_path / ".env.gh"
    env_file.write_text("# --- vars ---\nV1=x\n", encoding="utf-8")

    rc = run_main(module, monkeypatch, ["--env", "prod", "apply", "--file", str(env_file), "--dry-run"])

    assert rc == 0
    assert calls == []


def test_main_real_apply_ensures_the_environment(
    module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A real apply is the one command allowed to create the environment."""

    calls = env_tracking_patch(module, monkeypatch)
    monkeypatch.setattr(module, "apply_to_github", lambda *args, **kwargs: None)

    env_file = tmp_path / ".env.gh"
    env_file.write_text("# --- vars ---\nV1=x\n", encoding="utf-8")

    rc = run_main(module, monkeypatch, ["--env", "prod", "apply", "--file", str(env_file)])

    assert rc == 0
    assert [t.env_name for t in calls] == ["prod"]


def test_main_returns_1_on_unresolvable_repo(
    module, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Repo detection failure is a failure, not drift."""

    monkeypatch.setattr(module, "ensure_gh_or_fail", lambda: None)
    monkeypatch.setattr(module, "ensure_logged_in_or_fail", lambda: None)
    monkeypatch.setattr(
        module,
        "detect_repo_or_fail",
        lambda explicit: (_ for _ in ()).throw(ValueError("no repo")),
    )

    assert run_main(module, monkeypatch, ["list"]) == 1
    assert "no repo" in capsys.readouterr().err


def test_main_returns_1_on_bad_cli_usage(module, monkeypatch: pytest.MonkeyPatch) -> None:
    """argparse exits 2 by default, which would be indistinguishable from drift."""

    monkeypatch.setattr(module.sys, "argv", [module.APP_NAME, "nonsuch-command"])
    with pytest.raises(SystemExit) as excinfo:
        module.main()
    assert excinfo.value.code == 1
