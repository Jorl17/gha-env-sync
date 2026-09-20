"""Unit tests for repository auto-detection helpers.

Real integration tests pass explicit ``--repo`` and validate live behavior. These
checks keep fallback logic predictable for local developer usage.
"""

import subprocess

import pytest


def cp(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["cmd"], returncode=returncode, stdout=stdout, stderr=stderr)


def test_detect_repo_from_git_remote_success(module, monkeypatch: pytest.MonkeyPatch) -> None:
    """Inside a git repo, origin URL should normalize to OWNER/REPO."""

    monkeypatch.setattr(module, "git_exists", lambda: True)

    def fake_run(cmd, text=True, capture_output=True):
        if cmd[:3] == ["git", "rev-parse", "--is-inside-work-tree"]:
            return cp(returncode=0, stdout="true\n")
        if cmd[:3] == ["git", "remote", "get-url"]:
            return cp(returncode=0, stdout="git@github.com:Owner/Repo.git\n")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    assert module.detect_repo_from_git_remote() == "Owner/Repo"


def test_detect_repo_from_git_remote_returns_none_outside_repo(module, monkeypatch: pytest.MonkeyPatch) -> None:
    """When cwd is not a git worktree, git detection should return ``None``."""

    monkeypatch.setattr(module, "git_exists", lambda: True)

    def fake_run(cmd, text=True, capture_output=True):
        if cmd[:3] == ["git", "rev-parse", "--is-inside-work-tree"]:
            return cp(returncode=1, stdout="false\n")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    assert module.detect_repo_from_git_remote() is None


def test_detect_repo_from_gh_success(module, monkeypatch: pytest.MonkeyPatch) -> None:
    """`gh repo view` JSON should be parsed when available."""

    monkeypatch.setattr(
        module,
        "run",
        lambda *args, **kwargs: cp(returncode=0, stdout='{"nameWithOwner":"a/b"}'),
    )
    assert module.detect_repo_from_gh() == "a/b"


def test_detect_repo_or_fail_falls_back_to_git_then_gh(module, monkeypatch: pytest.MonkeyPatch) -> None:
    """Auto-detection should use git first, then gh as fallback."""

    monkeypatch.setattr(module, "detect_repo_from_git_remote", lambda: None)
    monkeypatch.setattr(module, "detect_repo_from_gh", lambda: "owner/repo")
    assert module.detect_repo_or_fail(None) == "owner/repo"


def test_detect_repo_or_fail_missing_git_note(module, monkeypatch: pytest.MonkeyPatch) -> None:
    """Error should explain when both detection paths fail and git is missing."""

    monkeypatch.setattr(module, "detect_repo_from_git_remote", lambda: None)
    monkeypatch.setattr(module, "detect_repo_from_gh", lambda: None)
    monkeypatch.setattr(module, "git_exists", lambda: False)

    with pytest.raises(ValueError, match="also: 'git' not found"):
        module.detect_repo_or_fail(None)
