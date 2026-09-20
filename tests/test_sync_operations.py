import json
import subprocess
import tempfile

import pytest


def cp(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["cmd"], returncode=returncode, stdout=stdout, stderr=stderr)


def test_bulk_dotenv_line_renders_values_literally(module) -> None:
    """Single-quoted dotenv values are literal: no interpolation, no escapes, no comments."""

    assert module.bulk_dotenv_line("K", "a # b") == "K='a # b'"
    assert module.bulk_dotenv_line("K", "${HOME}") == "K='${HOME}'"
    assert module.bulk_dotenv_line("K", 'say "hi"') == "K='say \"hi\"'"
    assert module.bulk_dotenv_line("K", "C:\\tools\\new") == "K='C:\\tools\\new'"
    assert module.bulk_dotenv_line("K", "  padded  ") == "K='  padded  '"


def test_bulk_dotenv_line_refuses_what_it_cannot_express(module) -> None:
    """A single quote cannot be escaped inside single quotes, so those go per key."""

    assert module.bulk_dotenv_line("K", "it's") is None
    assert module.bulk_dotenv_line("K", "l1\nl2") is None
    assert module.bulk_dotenv_line("K", "a\rb") is None


def test_batched_values_survive_a_dotenv_parse(module, repo_root) -> None:
    """The batch format must be the exact inverse of a dotenv read.

    gh parses this stream with its own dotenv reader, so anything the reader
    would alter has to come back identical or the value is corrupted in transit.
    """

    values = {
        "A": "plain",
        "B": "a # b",
        "C": "${HOME}",
        "D": "C:\\tools\\new",
        "E": '{"effort": "low"}',
        "F": "  padded  ",
        "G": "#fff",
        "H": "http://x/y#frag",
        "I": 'say "hi"',
    }
    stream = "\n".join(module.bulk_dotenv_line(k, v) for k, v in values.items())

    _, parsed, _, from_file = module.parse_combined_env(
        "# --- vars ---\n" + stream + "\n", strict=True, base_dir=repo_root
    )
    assert from_file == {}
    assert parsed == values


def test_apply_to_github_dry_run_prints_preview(module, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    target = module.RepoTarget(owner_repo="o/r", env_name="prod")
    # @file values are typically multiline (private keys), so they take the
    # per-key path; the inline values are batched.
    secrets_file = {"S_M": module.InlineOrFileValue(content="line1\nline2", source="file:/tmp/s")}
    vars_file = {"V_M": module.InlineOrFileValue(content="line1\nline2", source="file:/tmp/v")}

    monkeypatch.setattr(module, "run", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("run should not execute")))
    monkeypatch.setattr(
        module,
        "set_key_with_stdin",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("set_key_with_stdin should not execute")),
    )

    module.apply_to_github(target, {"S1": "a"}, {"V1": "b"}, secrets_file, vars_file, dry_run=True)

    out = capsys.readouterr().out
    assert "Would set secrets: 2  vars: 2" in out
    assert "gh secret set -f -" in out
    assert "gh variable set -f -" in out
    assert "# stdin (file:/tmp/s)" in out
    # Payloads are secrets and must never appear in a preview.
    assert "'a'" not in out
    assert "S1='a'" not in out


def test_apply_to_github_batches_expressible_values_into_one_call_per_kind(
    module, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One gh process per kind, matching the throughput of the bulk env-file path."""

    target = module.RepoTarget(owner_repo="o/r", env_name=None)
    calls = []
    monkeypatch.setattr(
        module,
        "run",
        lambda cmd, **kwargs: calls.append((cmd, kwargs.get("input_text"))) or cp(returncode=0),
    )
    monkeypatch.setattr(
        module,
        "set_key_with_stdin",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("nothing should need a per-key call")),
    )

    module.apply_to_github(
        target, {"S1": "a # b", "S2": "x"}, {"V1": "${HOME}"}, {}, {}, dry_run=False
    )

    assert len(calls) == 2, f"expected one call per kind, got {len(calls)}"
    secret_cmd, secret_stdin = calls[0]
    var_cmd, var_stdin = calls[1]
    assert secret_cmd[:5] == ["gh", "secret", "set", "-f", "-"]
    assert var_cmd[:5] == ["gh", "variable", "set", "-f", "-"]
    assert secret_stdin == "S1='a # b'\nS2='x'\n"
    assert var_stdin == "V1='${HOME}'\n"


def test_apply_to_github_falls_back_per_key_for_inexpressible_values(
    module, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Values holding a single quote or newline bypass gh's parser entirely."""

    target = module.RepoTarget(owner_repo="o/r", env_name=None)
    batched = []
    per_key = []
    monkeypatch.setattr(
        module,
        "run",
        lambda cmd, **kwargs: batched.append(kwargs.get("input_text")) or cp(returncode=0),
    )
    monkeypatch.setattr(
        module,
        "set_key_with_stdin",
        lambda target, kind, key, value: per_key.append((key, value.content)),
    )

    module.apply_to_github(
        target,
        {},
        {"SAFE": "plain", "QUOTE": "it's", "MULTI": "l1\nl2"},
        {},
        {"FILE": module.InlineOrFileValue(content="k\nv", source="file:/tmp/k")},
        dry_run=False,
    )

    assert batched == ["SAFE='plain'\n"]
    assert sorted(per_key) == [("FILE", "k\nv"), ("MULTI", "l1\nl2"), ("QUOTE", "it's")]


def test_apply_to_github_never_writes_values_to_disk(module, monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither apply nor dry-run may materialise secret values in a temp file."""

    def explode(*args, **kwargs):
        raise AssertionError("apply must not create temporary files")

    monkeypatch.setattr(tempfile, "TemporaryDirectory", explode)
    monkeypatch.setattr(module, "set_key_with_stdin", lambda *a, **k: None)
    monkeypatch.setattr(module, "run", lambda *a, **k: cp(returncode=0))

    target = module.RepoTarget(owner_repo="o/r", env_name=None)
    module.apply_to_github(target, {"S1": "secret-value"}, {"V1": "b"}, {}, {}, dry_run=True)
    module.apply_to_github(target, {"S1": "secret-value"}, {"V1": "b"}, {}, {}, dry_run=False)


def test_apply_to_github_skips_empty_secret_and_var_values(
    module, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """GitHub rejects empty values, so they are skipped for secrets as well as vars."""

    target = module.RepoTarget(owner_repo="o/r", env_name=None)
    batched = []
    monkeypatch.setattr(
        module,
        "run",
        lambda cmd, **kwargs: batched.append(kwargs.get("input_text")) or cp(returncode=0),
    )
    monkeypatch.setattr(module, "set_key_with_stdin", lambda *a, **k: None)

    module.apply_to_github(
        target, {"S_EMPTY": "", "S_OK": "x"}, {"V_BLANK": "   ", "V_OK": "y"}, {}, {}, dry_run=False
    )

    assert batched == ["S_OK='x'\n", "V_OK='y'\n"]
    err = capsys.readouterr().err
    assert "S_EMPTY" in err
    assert "V_BLANK" in err


def test_apply_to_github_error_names_the_failing_key(module, monkeypatch: pytest.MonkeyPatch) -> None:
    """A per-key failure must identify the key rather than just the command."""

    target = module.RepoTarget(owner_repo="o/r", env_name=None)

    def fake_set_key(target, kind, key, value):
        raise module.GhError("boom")

    monkeypatch.setattr(module, "run", lambda *a, **k: cp(returncode=0))
    monkeypatch.setattr(module, "set_key_with_stdin", fake_set_key)

    with pytest.raises(module.GhError, match="V_BAD"):
        module.apply_to_github(target, {}, {"V_BAD": "it's"}, {}, {}, dry_run=False)


def test_export_from_github_with_and_without_secret_placeholders(module, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        module,
        "gh_secret_list",
        lambda target: [{"name": "B"}, {"name": "A"}],
    )
    monkeypatch.setattr(
        module,
        "gh_variable_list",
        lambda target: [{"name": "Y", "value": "2"}, {"name": "X", "value": "1"}],
    )
    target = module.RepoTarget(owner_repo="o/r", env_name=None)

    with_placeholder = module.export_from_github(target, include_secret_placeholders=True)
    without_placeholder = module.export_from_github(target, include_secret_placeholders=False)

    assert "A=__REDACTED__" in with_placeholder
    assert "B=__REDACTED__" in with_placeholder
    assert "A=" in without_placeholder
    assert "B=" in without_placeholder
    assert "X=1" in with_placeholder
    assert "Y=2" in with_placeholder


def test_diff_local_vs_remote_no_changes_returns_zero(module, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    target = module.RepoTarget(owner_repo="o/r", env_name=None)
    monkeypatch.setattr(module, "gh_secret_list", lambda t: [{"name": "S1"}])
    monkeypatch.setattr(module, "gh_variable_list", lambda t: [{"name": "V1", "value": "ok"}])

    rc = module.diff_local_vs_remote(
        target,
        {"S1": "ignored"},
        {"V1": "ok"},
        {},
        {},
    )

    out = capsys.readouterr().out
    assert rc == 0
    assert "OK (same names)" in out
    assert "OK (names and values match)" in out


def test_diff_local_vs_remote_detects_changes_returns_two(module, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    target = module.RepoTarget(owner_repo="o/r", env_name=None)
    monkeypatch.setattr(module, "gh_secret_list", lambda t: [{"name": "S_REMOTE"}])
    monkeypatch.setattr(module, "gh_variable_list", lambda t: [{"name": "V1", "value": "remote"}, {"name": "EXTRA", "value": "x"}])

    rc = module.diff_local_vs_remote(
        target,
        {"S_LOCAL": "ignored"},
        {"V1": "local"},
        {},
        {},
    )

    out = capsys.readouterr().out
    assert rc == 2
    assert "Missing on remote: S_LOCAL" in out
    assert "Extra on remote: S_REMOTE" in out
    assert "Different values: V1" in out


def test_cmd_list_text_output(module, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    target = module.RepoTarget(owner_repo="o/r", env_name="prod")
    monkeypatch.setattr(module, "gh_secret_list", lambda t: [{"name": "Z"}, {"name": "A"}])
    monkeypatch.setattr(module, "gh_variable_list", lambda t: [{"name": "V", "value": "1"}])

    module.cmd_list(target, json_out=False)

    out = capsys.readouterr().out
    assert "Target: o/r (env: prod)" in out
    assert "A" in out and "Z" in out
    assert "V=1" in out


def test_cmd_list_json_output(module, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    target = module.RepoTarget(owner_repo="o/r", env_name=None)
    monkeypatch.setattr(module, "gh_secret_list", lambda t: [{"name": "S"}])
    monkeypatch.setattr(module, "gh_variable_list", lambda t: [{"name": "V", "value": "x"}])

    module.cmd_list(target, json_out=True)
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["repo"] == "o/r"
    assert parsed["env"] is None
    assert parsed["secrets"] == [{"name": "S"}]
    assert parsed["vars"] == [{"name": "V", "value": "x"}]


def test_apply_to_github_refuses_redacted_secret_placeholders(
    module, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Applying an export would otherwise overwrite every secret with the placeholder."""

    target = module.RepoTarget(owner_repo="o/r", env_name=None)
    monkeypatch.setattr(
        module,
        "set_key_with_stdin",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("nothing may be written")),
    )

    with pytest.raises(ValueError) as excinfo:
        module.apply_to_github(
            target,
            {"S_OK": "real", "S_BAD": "__REDACTED__"},
            {},
            {},
            {},
            dry_run=False,
        )

    assert "S_BAD" in str(excinfo.value)
    assert "S_OK" not in str(excinfo.value)


def test_apply_to_github_refuses_redacted_file_backed_secret(
    module, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard covers @file-backed secrets too, not just inline ones."""

    target = module.RepoTarget(owner_repo="o/r", env_name=None)
    monkeypatch.setattr(
        module,
        "set_key_with_stdin",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("nothing may be written")),
    )

    with pytest.raises(ValueError, match="S_FILE"):
        module.apply_to_github(
            target,
            {},
            {},
            {"S_FILE": module.InlineOrFileValue(content="__REDACTED__\n", source="file:/tmp/s")},
            {},
            dry_run=False,
        )


def test_apply_to_github_refuses_redacted_placeholders_in_dry_run(
    module, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dry-run must report the problem rather than previewing a destructive apply."""

    target = module.RepoTarget(owner_repo="o/r", env_name=None)
    with pytest.raises(ValueError, match="S_BAD"):
        module.apply_to_github(target, {"S_BAD": "__REDACTED__"}, {}, {}, {}, dry_run=True)


def test_apply_to_github_allows_redacted_placeholder_as_a_var_value(
    module, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only secrets carry the placeholder on export, so a var may legitimately hold that text."""

    target = module.RepoTarget(owner_repo="o/r", env_name=None)
    sent = []
    monkeypatch.setattr(
        module,
        "set_key_with_stdin",
        lambda target, kind, key, value: sent.append((key, value.content)),
    )

    batched = []
    monkeypatch.setattr(
        module,
        "run",
        lambda cmd, **kwargs: batched.append(kwargs.get("input_text")) or cp(returncode=0),
    )

    module.apply_to_github(target, {}, {"V1": "__REDACTED__"}, {}, {}, dry_run=False)

    assert sent == []
    assert batched == ["V1='__REDACTED__'\n"]
