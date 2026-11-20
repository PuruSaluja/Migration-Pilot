"""P1-03 / A10: the token is read once at startup and scrubbed from the environment."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from mpilot.core.errors import ConfigError
from mpilot.core.github_token import (
    GITHUB_TOKEN_ENV,
    current_github_token,
    load_github_token,
)

TOKEN = "github_pat_TESTONLY_0123456789abcdef"


def _token_file(directory: Path, content: str = TOKEN + "\n", mode: int = 0o600) -> Path:
    path = directory / "token"
    path.write_text(content)
    path.chmod(mode)
    return path


def test_env_token_is_loaded_and_removed_from_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(GITHUB_TOKEN_ENV, TOKEN)
    token = load_github_token()
    assert token is not None
    assert token.get_secret_value() == TOKEN
    assert GITHUB_TOKEN_ENV not in os.environ
    assert current_github_token() == token
    assert TOKEN not in repr(token)
    assert TOKEN not in str(token)


def test_no_token_configured() -> None:
    assert load_github_token() is None
    assert current_github_token() is None


def test_token_is_read_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(GITHUB_TOKEN_ENV, TOKEN)
    first = load_github_token()
    monkeypatch.setenv(GITHUB_TOKEN_ENV, "github_pat_LATER")
    with caplog.at_level(logging.WARNING):
        second = load_github_token()
    assert second == first
    assert GITHUB_TOKEN_ENV not in os.environ, "a late value is scrubbed too"
    assert "github_pat_LATER" not in caplog.text
    assert TOKEN not in caplog.text


def test_token_file(tmp_path: Path) -> None:
    token = load_github_token(token_file=_token_file(tmp_path))
    assert token is not None
    assert token.get_secret_value() == TOKEN


def test_env_wins_over_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(GITHUB_TOKEN_ENV, TOKEN)
    token = load_github_token(token_file=_token_file(tmp_path, "github_pat_FILE\n"))
    assert token is not None
    assert token.get_secret_value() == TOKEN
    assert GITHUB_TOKEN_ENV not in os.environ


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o666, 0o700])
def test_token_file_with_group_or_other_bits_is_refused(tmp_path: Path, mode: int) -> None:
    path = _token_file(tmp_path, mode=mode)
    if mode == 0o700:
        assert load_github_token(token_file=path) is not None  # owner-only bits are fine
        return
    with pytest.raises(ConfigError, match="0600 or stricter"):
        load_github_token(token_file=path)


def test_symlinked_token_file_is_refused(tmp_path: Path) -> None:
    real = _token_file(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(ConfigError, match="cannot open"):
        load_github_token(token_file=link)


def test_fifo_token_file_is_refused(tmp_path: Path) -> None:
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(ConfigError, match="not a regular file"):
        load_github_token(token_file=fifo)


def test_token_file_inside_a_git_worktree_is_refused(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "conf").mkdir()
    with pytest.raises(ConfigError, match="inside the git working tree"):
        load_github_token(token_file=_token_file(repo / "conf"))


def test_token_file_reached_through_a_symlinked_dir_into_a_repo_is_refused(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    _token_file(repo)
    alias = tmp_path / "alias"
    alias.symlink_to(repo, target_is_directory=True)
    with pytest.raises(ConfigError, match="inside the git working tree"):
        load_github_token(token_file=alias / "token")


@pytest.mark.parametrize("content", ["", "\n", "two words\n", "a\tb"])
def test_empty_or_malformed_token_is_refused(tmp_path: Path, content: str) -> None:
    with pytest.raises(ConfigError, match="empty or contains whitespace"):
        load_github_token(token_file=_token_file(tmp_path, content))


def test_missing_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot open"):
        load_github_token(token_file=tmp_path / "missing")


def test_env_is_scrubbed_even_when_loading_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(GITHUB_TOKEN_ENV, "   ")
    with pytest.raises(ConfigError):
        load_github_token()
    assert GITHUB_TOKEN_ENV not in os.environ


def test_token_file_owned_by_another_user_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _token_file(tmp_path)
    real_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)
    with pytest.raises(ConfigError, match="not owned by the current user"):
        load_github_token(token_file=path)


def test_oversized_token_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="too large"):
        load_github_token(token_file=_token_file(tmp_path, "x" * 5000))


def test_unreadable_parent_dir_is_a_config_error(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    inner = locked / "inner"
    inner.mkdir(parents=True)
    path = _token_file(inner)
    locked.chmod(0o000)
    try:
        with pytest.raises(ConfigError):
            load_github_token(token_file=path)
    finally:
        locked.chmod(0o700)


def test_gitfile_marks_a_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "wt"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: /elsewhere\n")
    with pytest.raises(ConfigError, match="inside the git working tree"):
        load_github_token(token_file=_token_file(repo))
