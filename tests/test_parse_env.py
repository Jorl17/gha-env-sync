"""Parser unit tests with focus on behavior not trivially covered by integration.

The integration suite validates end-to-end apply/diff/list/export behavior against
real GitHub. This file keeps smaller parser edge-cases deterministic and fast.
"""

from pathlib import Path

import pytest


def test_parse_combined_env_valid_sections(module, repo_root: Path) -> None:
    """Basic secrets/vars sections should parse into the expected tuple shape."""

    text = """# --- secrets ---
API_TOKEN=abc

# --- vars ---
LOG_LEVEL=INFO
"""
    secrets_simple, vars_simple, secrets_file, vars_file = module.parse_combined_env(
        text,
        strict=True,
        base_dir=repo_root,
    )
    assert secrets_simple == {"API_TOKEN": "abc"}
    assert vars_simple == {"LOG_LEVEL": "INFO"}
    assert secrets_file == {}
    assert vars_file == {}


def test_parse_combined_env_alias_markers(module, repo_root: Path) -> None:
    """Alias marker syntax should map to the same sections as default markers."""

    text = """# GHA:SECRETS
S1=a
# GHA:VARS
V1=b
"""
    secrets_simple, vars_simple, _, _ = module.parse_combined_env(
        text,
        strict=True,
        base_dir=repo_root,
    )
    assert secrets_simple == {"S1": "a"}
    assert vars_simple == {"V1": "b"}


def test_parse_combined_env_rejects_overlap(module, repo_root: Path) -> None:
    """Keys may not exist in both sections."""

    text = """# --- secrets ---
SHARED_KEY=foo
# --- vars ---
SHARED_KEY=bar
"""
    with pytest.raises(ValueError, match="both secrets and vars"):
        module.parse_combined_env(text, strict=True, base_dir=repo_root)


def test_parse_combined_env_multiline_is_rejected(module, repo_root: Path) -> None:
    """Legacy ``@multiline`` is intentionally unsupported."""

    text = """# --- secrets ---
PEM=@multiline
"""
    with pytest.raises(ValueError, match="no longer supported"):
        module.parse_combined_env(text, strict=True, base_dir=repo_root)


def test_parse_combined_env_rejects_invalid_key_strict(module, repo_root: Path) -> None:
    """Strict parsing should reject invalid key names."""

    text = """# --- vars ---
123_BAD=value
"""
    with pytest.raises(ValueError, match="invalid key"):
        module.parse_combined_env(text, strict=True, base_dir=repo_root)


def test_parse_combined_env_rejects_no_markers_in_strict(module, repo_root: Path) -> None:
    """Strict mode requires at least one recognized marker."""

    with pytest.raises(ValueError, match="No section markers"):
        module.parse_combined_env("# just a comment\n", strict=True, base_dir=repo_root)


def test_parse_combined_env_entry_before_marker_errors_in_strict(module, repo_root: Path) -> None:
    """Strict mode should reject data before any section marker."""

    with pytest.raises(ValueError, match="entry before any section marker"):
        module.parse_combined_env("A=1\n", strict=True, base_dir=repo_root)


def test_parse_combined_env_file_reference_relative(module, tmp_path: Path) -> None:
    """Relative ``@file`` references resolve from the combined-file directory."""

    payload = tmp_path / "payload.txt"
    payload.write_text("secret-value", encoding="utf-8")
    text = """# --- secrets ---
K=@file:payload.txt
"""
    _, _, secrets_file, _ = module.parse_combined_env(text, strict=True, base_dir=tmp_path)
    assert secrets_file["K"].content == "secret-value"
    assert "payload.txt" in secrets_file["K"].source


def test_parse_combined_env_file_reference_env_var(module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Environment variable expansion should work in ``@file`` paths."""

    payload = tmp_path / "payload.txt"
    payload.write_text("from-env", encoding="utf-8")
    monkeypatch.setenv("PAYLOAD_PATH", str(payload))

    text = """# --- vars ---
FROM_ENV=@file:$PAYLOAD_PATH
"""
    _, _, _, vars_file = module.parse_combined_env(text, strict=True, base_dir=tmp_path)
    assert vars_file["FROM_ENV"].content == "from-env"


def test_parse_combined_env_file_reference_tilde(module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Home-directory expansion should work in ``@file`` paths."""

    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True, exist_ok=True)
    payload = fake_home / "tilde.txt"
    payload.write_text("tilde-content", encoding="utf-8")
    monkeypatch.setenv("HOME", str(fake_home))

    text = """# --- secrets ---
T=@file:~/tilde.txt
"""
    _, _, secrets_file, _ = module.parse_combined_env(text, strict=True, base_dir=tmp_path)
    assert secrets_file["T"].content == "tilde-content"


def test_parse_combined_env_file_reference_missing(module, tmp_path: Path) -> None:
    """Missing ``@file`` targets should fail with actionable errors."""

    text = """# --- secrets ---
MISSING=@file:does-not-exist.txt
"""
    with pytest.raises(ValueError, match="does not exist"):
        module.parse_combined_env(text, strict=True, base_dir=tmp_path)


def test_parse_combined_env_strips_single_quotes(module, repo_root: Path) -> None:
    """A value fully wrapped in single quotes drops the outer pair (dotenv semantics).

    Single quotes are literal, so interior content (including double quotes and
    spaces, as in a JSON reasoning payload) is preserved verbatim.
    """

    text = """# --- vars ---
KB_VERIFICATION_REASONING='{"effort": "low"}'
"""
    _, vars_simple, _, _ = module.parse_combined_env(text, strict=True, base_dir=repo_root)
    assert vars_simple == {"KB_VERIFICATION_REASONING": '{"effort": "low"}'}


def test_parse_combined_env_strips_double_quotes_and_unescapes(module, repo_root: Path) -> None:
    """Double-quoted values drop the outer pair and unescape backslash escapes."""

    text = '# --- vars ---\nMSG="he said \\"hi\\""\n'
    _, vars_simple, _, _ = module.parse_combined_env(text, strict=True, base_dir=repo_root)
    assert vars_simple == {"MSG": 'he said "hi"'}


def test_parse_combined_env_trims_unquoted_whitespace(module, repo_root: Path) -> None:
    """Unquoted values are whitespace-trimmed, like dotenv."""

    text = "# --- vars ---\nLOG_LEVEL=  INFO  \n"
    _, vars_simple, _, _ = module.parse_combined_env(text, strict=True, base_dir=repo_root)
    assert vars_simple == {"LOG_LEVEL": "INFO"}


def test_parse_combined_env_preserves_interior_and_mismatched_quotes(module, repo_root: Path) -> None:
    """Interior quotes and mismatched wrapping quotes are left untouched."""

    text = """# --- vars ---
INTERIOR=say "hi" now
MISMATCH='abc"
"""
    _, vars_simple, _, _ = module.parse_combined_env(text, strict=True, base_dir=repo_root)
    assert vars_simple == {"INTERIOR": 'say "hi" now', "MISMATCH": "'abc\""}


def test_parse_combined_env_strips_unquoted_inline_comment(module, repo_root: Path) -> None:
    """dotenv treats ' #' as a comment on an unquoted value."""

    text = "# --- vars ---\nSPACED=a # b\n"
    _, vars_simple, _, _ = module.parse_combined_env(text, strict=True, base_dir=repo_root)
    assert vars_simple == {"SPACED": "a"}


def test_parse_combined_env_hash_without_leading_space_is_literal(module, repo_root: Path) -> None:
    """A '#' not preceded by whitespace is part of the value: colors, fragments, tight hashes."""

    text = "# --- vars ---\nTIGHT=a#b\nCOLOR=#fff\nURL=http://x/y#frag\n"
    _, vars_simple, _, _ = module.parse_combined_env(text, strict=True, base_dir=repo_root)
    assert vars_simple == {"TIGHT": "a#b", "COLOR": "#fff", "URL": "http://x/y#frag"}


def test_parse_combined_env_quoted_hash_is_not_a_comment(module, repo_root: Path) -> None:
    """Inside quotes a '#' is literal, and a comment may follow the closing quote."""

    text = '# --- vars ---\nQ1="a # b"\nQ2="c # d"  # trailing comment\n'
    _, vars_simple, _, _ = module.parse_combined_env(text, strict=True, base_dir=repo_root)
    assert vars_simple == {"Q1": "a # b", "Q2": "c # d"}


def test_parse_combined_env_translates_double_quoted_escapes(module, repo_root: Path) -> None:
    """Double-quoted escapes become real characters; unknown escapes pass through."""

    text = '# --- vars ---\nNL="l1\\nl2"\nTAB="a\\tb"\nUNKNOWN="\\d+"\nBACKSLASH="a\\\\b"\n'
    _, vars_simple, _, _ = module.parse_combined_env(text, strict=True, base_dir=repo_root)
    assert vars_simple == {
        "NL": "l1\nl2",
        "TAB": "a\tb",
        "UNKNOWN": "\\d+",
        "BACKSLASH": "a\\b",
    }


def test_parse_combined_env_single_quotes_do_not_unescape(module, repo_root: Path) -> None:
    """Single-quoted values are literal, so a Windows path survives intact."""

    text = "# --- vars ---\nWINPATH='C:\\tools\\new'\n"
    _, vars_simple, _, _ = module.parse_combined_env(text, strict=True, base_dir=repo_root)
    assert vars_simple == {"WINPATH": "C:\\tools\\new"}


def test_parse_combined_env_rejects_duplicate_key_across_inline_and_file(
    module, repo_root: Path, tmp_path: Path
) -> None:
    """A key defined both inline and via @file would apply one value and diff against the other."""

    payload = tmp_path / "payload.txt"
    payload.write_text("from-file", encoding="utf-8")
    text = f"# --- vars ---\nDUP=inline\nDUP=@file:{payload}\n"

    with pytest.raises(ValueError, match="DUP"):
        module.parse_combined_env(text, strict=True, base_dir=tmp_path)


def test_parse_combined_env_rejects_github_reserved_prefix(module, repo_root: Path) -> None:
    """GitHub rejects GITHUB_* names with a 422; fail locally with a clear message."""

    text = "# --- vars ---\nGITHUB_TOKEN=x\n"
    with pytest.raises(ValueError, match="GITHUB_"):
        module.parse_combined_env(text, strict=True, base_dir=repo_root)


def test_parse_combined_env_error_does_not_echo_the_value(module, repo_root: Path) -> None:
    """A malformed secret line must not have its contents printed back."""

    text = "# --- secrets ---\nthis-line-holds-a-real-secret-value\n"
    with pytest.raises(ValueError) as excinfo:
        module.parse_combined_env(text, strict=True, base_dir=repo_root)

    assert "this-line-holds-a-real-secret-value" not in str(excinfo.value)
    assert "line 2" in str(excinfo.value).lower()
