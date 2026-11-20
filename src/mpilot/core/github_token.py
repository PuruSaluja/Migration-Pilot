"""Read the GitHub token once at startup and scrub it from the environment (A10, SPEC §9.3).

Sources, in order:
1. the `MPILOT_GITHUB_TOKEN` environment variable;
2. a token file: regular, not a symlink, owned by the current user, no group/other
   permission bits (0600 or stricter), and not inside any git working tree.

Whatever the source, `MPILOT_GITHUB_TOKEN` is removed from `os.environ` before this returns,
so no child process can inherit it even by accident (children are also started with an
explicit env, `core.spawn`).

Removing a variable does not change the kernel's copy: `/proc/<pid>/environ` shows the
process's *initial* environment block, and every same-uid host child (git, tree-sitter, the
Claude CLI) can read its parent's. So before unsetting, the value bytes are overwritten in
place through libc `getenv`, and a test asserts that a spawned child reading
`/proc/$PPID/environ` no longer finds the token.
"""

from __future__ import annotations

import ctypes
import logging
import os
import stat
from collections.abc import MutableMapping
from pathlib import Path

from pydantic import SecretStr

from mpilot.core.errors import ConfigError

logger = logging.getLogger(__name__)

GITHUB_TOKEN_ENV = "MPILOT_GITHUB_TOKEN"  # noqa: S105  (a variable name, not a secret)
MAX_TOKEN_FILE_BYTES = 4096


class _TokenState:
    loaded: bool = False
    token: SecretStr | None = None


_state = _TokenState()


def current_github_token() -> SecretStr | None:
    """The token loaded at startup, or None if none was configured or startup hasn't run."""
    return _state.token


def _inside_git_worktree(path: Path) -> Path | None:
    """The nearest ancestor holding a `.git` dir or gitfile.

    Work trees whose git dir lives elsewhere (`--git-dir=... --work-tree=$HOME`) carry no
    marker and cannot be detected; the 0600 and ownership checks still apply.
    """
    for parent in path.parents:
        try:
            os.lstat(parent / ".git")
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ConfigError(
                f"cannot check {parent} for a git repository: {exc.strerror}"
            ) from exc
        return parent
    return None


def _wipe_process_env_value(name: str) -> None:
    """Overwrite the value bytes of every `name=` entry in libc's `environ`, in place.

    For variables inherited at exec time, the entries point into the initial environment
    block that `/proc/<pid>/environ` shows, so overwriting them hides the value there; for
    variables set later through `setenv`, it overwrites libc's heap copy. Every entry is
    wiped, not just the first, because a launcher can pass duplicates (and Python keeps the
    last one). The name stays; the caller unsets the variable next.
    """
    libc = ctypes.CDLL(None)
    entries = ctypes.POINTER(ctypes.c_void_p).in_dll(libc, "environ")
    prefix = name.encode() + b"="
    index = 0
    while entries and (address := entries[index]) is not None:
        entry = ctypes.string_at(address)
        if entry.startswith(prefix):
            ctypes.memset(address + len(prefix), 0, len(entry) - len(prefix))
        index += 1


def _read_token_file(path: Path) -> str:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError as exc:
        # ELOOP means the final component is a symlink.
        raise ConfigError(f"cannot open GitHub token file {path}: {exc.strerror}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ConfigError(f"GitHub token file {path} is not a regular file")
        if info.st_uid != os.getuid():
            raise ConfigError(f"GitHub token file {path} is not owned by the current user")
        if info.st_mode & 0o077:
            raise ConfigError(
                f"GitHub token file {path} has mode {stat.S_IMODE(info.st_mode):04o}; "
                "it must be 0600 or stricter"
            )
        if info.st_size > MAX_TOKEN_FILE_BYTES:
            raise ConfigError(f"GitHub token file {path} is too large")
        data = os.read(fd, MAX_TOKEN_FILE_BYTES + 1)
    finally:
        os.close(fd)
    try:
        return data.decode("ascii")
    except UnicodeDecodeError:
        raise ConfigError(f"GitHub token file {path} is not ASCII text") from None


def load_github_token(
    *,
    token_file: Path | None = None,
    environ: MutableMapping[str, str] | None = None,
) -> SecretStr | None:
    """Load the token once and remove it from the environment. Later calls return the cache.

    Call this first thing at process startup (the CLI callback does). The env var is
    scrubbed on every call, so a value set after startup is also removed, never used.
    """
    env = os.environ if environ is None else environ
    if env is os.environ:
        _wipe_process_env_value(GITHUB_TOKEN_ENV)
    raw_env = env.pop(GITHUB_TOKEN_ENV, None)
    if _state.loaded:
        if raw_env is not None:
            logger.warning("ignored and removed %s set after startup", GITHUB_TOKEN_ENV)
        return _state.token

    raw: str | None = None
    if raw_env is not None:
        raw = raw_env
        if token_file is not None:
            logger.warning(
                "both %s and a token file are set; using the environment variable",
                GITHUB_TOKEN_ENV,
            )
    elif token_file is not None:
        path = Path(os.path.abspath(token_file))
        # Resolve the directories (not the file itself, which must not be a symlink).
        repo = _inside_git_worktree(Path(os.path.realpath(path.parent)) / path.name)
        if repo is not None:
            raise ConfigError(
                f"GitHub token file {path} is inside the git working tree {repo}; "
                "keep it outside any repository"
            )
        raw = _read_token_file(path)

    token: SecretStr | None = None
    if raw is not None:
        value = raw.strip()
        if not value or any(c.isspace() for c in value):
            raise ConfigError("the GitHub token is empty or contains whitespace")
        token = SecretStr(value)

    _state.token = token
    _state.loaded = True
    return token


def _reset_for_tests() -> None:
    """Forget the loaded token. Tests only."""
    _state.loaded = False
    _state.token = None
