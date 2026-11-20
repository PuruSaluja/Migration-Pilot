"""The only place in `mpilot` that starts child processes (A10, SPEC §9.3, NFR-6).

Every child (git, tree-sitter, docker CLI if ever needed, test helpers) is started through
`run` or `start`. Both require an explicit `env`: nothing is inherited from the orchestrator's
own environment. Use `inherit("PATH", ...)` to copy named variables on purpose.

Defense in depth on top of the explicit env:
- argv is a list, never a shell string; there is no `shell=True`.
- The GitHub token never appears in argv (always refused) and appears in `env` only when the
  caller sets `pass_github_token=True` (the push path, via `GIT_ASKPASS`, §9.5).
- stdin defaults to /dev/null, so a child cannot block on a credential prompt.
- Each child gets its own session; a timeout kills the whole process group.

A lint test (`tests/unit/test_spawn_lint.py`) forbids every process-starting form anywhere
in `src/mpilot` except this module: `subprocess`, the `os` exec/spawn/fork/system/popen
family, `pty`, `multiprocessing`, process pools, asyncio subprocesses, `webbrowser`,
third-party spawners and dynamic imports. The lint's parametrized cases are the full list.

Leftover background grandchildren are killed only on timeout or error, never after a normal
exit (the child is reaped by then, so its group id could be reused). Callers start trusted
host tools only (git, tree-sitter), never repo code (NFR-6).

Note: the Claude Agent SDK starts its own CLI subprocess through its transport; the env it
passes is locked down separately in P4-08 (§8.1 item 8).
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from mpilot.core.errors import MpilotError
from mpilot.core.github_token import GITHUB_TOKEN_ENV, current_github_token

logger = logging.getLogger(__name__)


class SpawnError(MpilotError):
    """A child process could not be started safely (bad argv or env, or a secret in them)."""


@dataclass(frozen=True)
class SpawnResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes
    duration_s: float
    timed_out: bool


def inherit(*names: str) -> dict[str, str]:
    """Copy the named variables from the orchestrator's environment, if set.

    The GitHub token variable can never be inherited.
    """
    if GITHUB_TOKEN_ENV in names:
        raise SpawnError(f"{GITHUB_TOKEN_ENV} can never be inherited by a child process")
    return {name: os.environ[name] for name in names if name in os.environ}


def _validate(
    argv: Sequence[str], env: Mapping[str, str], cwd: Path | None, *, pass_github_token: bool
) -> None:
    if isinstance(argv, (str, bytes)) or not argv:
        raise SpawnError("argv must be a non-empty sequence of strings, never a shell string")
    for arg in argv:
        if not isinstance(arg, str) or "\0" in arg:
            raise SpawnError("every argv item must be a string without NUL bytes")
    # An absolute executable can't be resolved against cwd (a workspace or mirror) or a
    # relative PATH entry, so a binary planted in a repo never runs on the host (NFR-6).
    if not os.path.isabs(argv[0]):
        raise SpawnError(f"argv[0] must be an absolute path, got {argv[0]!r}")
    for key, value in env.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise SpawnError("env keys and values must be strings")
        if not key or "=" in key or "\0" in key or "\0" in value:
            raise SpawnError(f"invalid env entry {key!r}")
    if GITHUB_TOKEN_ENV in env:
        raise SpawnError(f"{GITHUB_TOKEN_ENV} is never passed to a child process")
    if "PATH" in env and not all(os.path.isabs(entry) for entry in env["PATH"].split(os.pathsep)):
        raise SpawnError("every PATH entry must be an absolute directory (no empty or . entries)")

    token = current_github_token()
    if token is None:
        return
    secret = token.get_secret_value()
    if any(secret in arg for arg in argv):
        raise SpawnError("the GitHub token must never appear in argv")
    if any(secret in key for key in env) or (cwd is not None and secret in str(cwd)):
        raise SpawnError("the GitHub token must never appear in env keys or cwd")
    if not pass_github_token and any(secret in value for value in env.values()):
        raise SpawnError("the GitHub token is in env but pass_github_token is not set")


def start(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: Path | None = None,
    stdin: int | IO[bytes] | None = subprocess.DEVNULL,
    stdout: int | IO[bytes] | None = subprocess.PIPE,
    stderr: int | IO[bytes] | None = subprocess.PIPE,
    pass_github_token: bool = False,
) -> subprocess.Popen[bytes]:
    """Start a child with exactly `env` as its environment, in its own session."""
    _validate(argv, env, cwd, pass_github_token=pass_github_token)
    logger.debug(
        "spawn",
        extra={"executable": argv[0], "env_keys": sorted(env), "cwd": str(cwd) if cwd else None},
    )
    try:
        return subprocess.Popen(  # noqa: S603  (argv list, explicit env, validated above)
            list(argv),
            env=dict(env),
            cwd=cwd,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            close_fds=True,
            start_new_session=True,
        )
    except OSError as exc:
        raise SpawnError(f"could not start {argv[0]!r}: {exc.strerror or exc}") from exc


#: After a timeout kill, how long to wait for the pipes to close before giving up on them
#: (a process that left the group with `setsid` can hold them open).
KILL_GRACE_S = 2.0


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    """SIGKILL the child's process group.

    Only called while the child is not yet reaped, so its pid (= the group id) cannot have
    been reused by an unrelated process.
    """
    with contextlib.suppress(OSError):
        os.killpg(proc.pid, signal.SIGKILL)


def run(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: Path | None = None,
    input_bytes: bytes | None = None,
    timeout_s: float | None = None,
    pass_github_token: bool = False,
) -> SpawnResult:
    """Run a child to completion and capture its output.

    On timeout the child's whole process group is killed and the result has
    `timed_out=True`. The call then returns within `KILL_GRACE_S`, with whatever output was
    read, even if an escaped grandchild still holds the pipes.
    """
    began = time.monotonic()
    proc = start(
        argv,
        env=env,
        cwd=cwd,
        stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
        pass_github_token=pass_github_token,
    )
    timed_out = False
    try:
        out, err = proc.communicate(input=input_bytes, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(proc)
        try:
            out, err = proc.communicate(timeout=KILL_GRACE_S)
        except subprocess.TimeoutExpired as exc:
            # Partial output read so far; the pipes are abandoned.
            out, err = exc.output or b"", exc.stderr or b""
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                if pipe is not None:
                    pipe.close()
        finally:
            proc.wait()  # the group was killed; reap even if interrupted during the grace
    except BaseException:
        _kill_group(proc)
        proc.wait()
        raise
    return SpawnResult(
        argv=tuple(argv),
        returncode=proc.returncode,
        stdout=out,
        stderr=err,
        duration_s=time.monotonic() - began,
        timed_out=timed_out,
    )
