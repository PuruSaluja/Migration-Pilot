"""Docker driver for the sandbox: the work container's lifecycle and `exec` (SPEC §9.2, §9.3,
NFR-6, NFR-10).

Scope (P2-02, P2-03). Built here: runtime detection (gVisor `runsc`, else `runc` with a recorded
warning), `create` with the §9.3 hardening and limits, `exec` with output tails, timeouts and
cancellation, `frozen()` (P2-03), `export_workspace`/`restore` (P2-04), the disk limit and the
`/tmp` cap (P2-05, D6), `destroy`, `reap`. Owned by later tasks, and refused with
`NotImplementedError` until then: `NetworkMode.REGISTRY` and the install container (P2-09),
and `CacheMode.HASH_VERIFIED` (P3-13, final verification).

Package caches (P2-07b, `sandbox/cache.py`): with a `cache_root`, a work container gets its
cache at `/cache`, read-only, with the package managers pointed at it; a fresh cache is created
for `CacheMode.FRESH` and removed with the sandbox; the cache counts toward the disk limit.

How the container behaves, learned on this host (WSL2, Docker 29, runsc, systrap platform):
- Under gVisor a command that passes the memory or pids limit does not fail alone: the whole
  sandbox is killed (exit 137). `exec` notices, restarts the container and reports exit code
  137 with a note on stderr. Under runc the kernel refuses the fork or kills the process.
- A small pids limit is a hard floor: runsc's own host threads count against it, and 32 is
  too low for the container to start at all. The configured default is 1024.
- Busybox `ash` has no job control without a tty, so process groups cannot be used to kill a
  timed-out command. Every exec carries a unique environment tag instead, and the kill scans
  `/proc/*/environ` for it. That also reaches processes that left their session (`setsid`).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import secrets
import stat
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, TypeVar

import docker
from docker.errors import APIError, DockerException, ImageNotFound, NotFound
from docker.types import LogConfig

from mpilot.core import spawn
from mpilot.core.errors import ExportIntegrityError, MpilotError, PathEscapeError
from mpilot.core.types import NetworkMode, RunId
from mpilot.models.artifacts import ExportManifest
from mpilot.sandbox.base import (
    CacheMode,
    ContainerRuntime,
    ExecResult,
    FreezeMechanism,
    RuntimeInfo,
    Sandbox,
    SandboxDriver,
    SandboxSpec,
    sandbox_env_problems,
)
from mpilot.sandbox.cache import (
    CacheError,
    CacheRole,
    CacheStore,
    cache_env,
    cache_volume,
    check_key,
)
from mpilot.sandbox.disk import DISK_POLL_S, GIB, human_bytes, tmpfs_options
from mpilot.sandbox.workspace import HostWorkspace

_T = TypeVar("_T")

LABEL_RUN: Final = "mpilot.run_id"
LABEL_ROLE: Final = "mpilot.role"
ROLE_WORK: Final = "work"
ROLE_INSTALL: Final = "install"  # P2-09's install containers carry this role

#: The only variables a caller (or a `SandboxSpec`) may put in a container's environment
#: (§9.3 Secrets: "Env vars are passed into the sandbox only from an allowlist, for every
#: command"). Minimal on purpose: none of them configures a package manager, which H8's
#: policy hook (P4-13) inspects, and none can carry a credential. Later tasks extend the list
#: with a stated reason. `HOME` and the exec tag are the driver's and are never caller-set.
ALLOWED_EXEC_ENV: Final = frozenset(
    {
        "CI",
        "LANG",
        "LC_ALL",
        "TZ",
        "NO_COLOR",
        "NODE_ENV",
        "PYTHONHASHSEED",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONUNBUFFERED",
    }
)

#: Every container's `HOME`: the root is read-only, so the home directory is the tmpfs.
CONTAINER_HOME: Final = "/tmp"  # noqa: S108  (inside the container, not a host path)
_EXEC_TAG_VAR: Final = "_MPILOT_EXEC"
#: Per-stream bytes of command output kept (the end of it), unless the sandbox is given another.
DEFAULT_TAIL_BYTES: Final = 32 * 1024
#: After the kill, how long to wait for the output stream to end before abandoning it.
KILL_GRACE_S: Final = 5.0
_KILL_EXEC_TIMEOUT_S: Final = 20.0
_RESTART_PAUSE_S: Final = 0.2
#: Consecutive failed disk measurements after which the command is stopped.
_DISK_MEASURE_FAILURES: Final = 3
_SETTLE_S: Final = 3.0
#: Exit codes that may mean the sandbox itself died: gVisor's lost-sandbox 128, and SIGKILL.
_DEATH_CODES: Final = frozenset({128, 137})

#: Kills every process whose environment carries the tag, repeating until none is left (a
#: dying process may have forked once more). Exit status 0 only if the last pass found none.
_KILL_SCRIPT: Final = r"""
tag="$1"; pass=0
while [ "$pass" -lt 6 ]; do
  found=0
  for d in /proc/[0-9]*; do
    p=${d#/proc/}
    [ "$p" = "$$" ] && continue
    if tr '\0' '\n' < "$d/environ" 2>/dev/null | grep -qxF "$1_VAR=$tag"; then
      kill -KILL "$p" 2>/dev/null && found=1
    fi
  done
  [ "$found" = 0 ] && exit 0
  pass=$((pass+1))
done
exit 1
""".replace("$1_VAR", _EXEC_TAG_VAR)

#: KILL_ALL freeze (§9.5): kill every process but init, the container's main process and this
#: script, and repeat until no other pid exists at all. Zombies are killed too: a thread-group
#: leader that exited alone shows as a zombie while its other threads run (gVisor does not
#: even list those threads under it), and SIGKILL to it ends the whole group. A real zombie's
#: parent is dead, so init reaps it and it disappears. Exit status 0 only if a pass found no
#: pid outside the keep list. `sleep` is the only fork, and it has exited before each scan.
_FREEZE_KILL_SCRIPT: Final = r"""
keep=" 1 $1 $$ "
pass=0
while [ "$pass" -lt 100 ]; do
  found=0
  for d in /proc/[0-9]*; do
    p=${d#/proc/}
    case "$keep" in *" $p "*) continue;; esac
    [ -d "$d" ] || continue
    kill -KILL "$p" 2>/dev/null
    found=1
  done
  [ "$found" = 0 ] && exit 0
  pass=$((pass+1))
  sleep 0.05
done
exit 1
"""

#: Prints the pid of every process whose parent is init. Run right after the container starts,
#: before any command, the only one is the container's main process.
_MAIN_PID_SCRIPT: Final = r"""
for d in /proc/[0-9]*; do
  p=${d#/proc/}
  [ "$p" = "$$" ] && continue
  while read -r k v rest; do
    if [ "$k" = PPid: ]; then [ "$v" = 1 ] && echo "$p"; break; fi
  done 2>/dev/null < "$d/status"
done
"""

_RUNTIME_FAILURE = re.compile(r"runtime|runsc|gvisor|oci", re.I)


def env_problems(env: Mapping[str, str]) -> list[str]:
    """Why `env` may not enter a container: the credential floor of `sandbox_env_problems`,
    then the driver's allowlist."""
    problems = sandbox_env_problems(env)
    flagged = {p.split(":", 1)[0] for p in problems}
    problems.extend(
        f"{key}: not on the sandbox environment allowlist (§9.3)"
        for key in env
        if key not in ALLOWED_EXEC_ENV and key not in flagged and key.isidentifier()
    )
    return problems


class _Tail:
    """The last `limit` bytes written to it, and whether anything was dropped."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._buf = bytearray()
        self.truncated = False

    def write(self, chunk: bytes) -> None:
        self._buf += chunk
        if len(self._buf) > self._limit:
            del self._buf[: len(self._buf) - self._limit]
            self.truncated = True

    def text(self) -> str:
        return bytes(self._buf).decode("utf-8", errors="replace")


class _Stream:
    """Reads one exec's output on a thread, so the event loop never blocks on the socket."""

    def __init__(self, client: docker.DockerClient, exec_id: str, tail_bytes: int) -> None:
        self.stdout = _Tail(tail_bytes)
        self.stderr = _Tail(tail_bytes)
        self.error: BaseException | None = None
        self.exit_code: int | None = None
        self._client = client
        self._exec_id = exec_id
        self._done = threading.Event()
        self._loop = asyncio.get_running_loop()
        self.finished: asyncio.Future[None] = self._loop.create_future()
        threading.Thread(target=self._read, name="mpilot-exec-reader", daemon=True).start()

    def _read(self) -> None:
        try:
            api = self._client.api
            for out, err in api.exec_start(self._exec_id, stream=True, demux=True):
                if out:
                    self.stdout.write(out)
                if err:
                    self.stderr.write(err)
            deadline = time.monotonic() + 5
            while True:
                info = api.exec_inspect(self._exec_id)
                if not info.get("Running") and info.get("ExitCode") is not None:
                    self.exit_code = int(info["ExitCode"])
                    break
                if time.monotonic() > deadline:
                    break
                time.sleep(0.05)
        except BaseException as exc:
            self.error = exc
        finally:
            self._done.set()
            with contextlib.suppress(RuntimeError):  # the loop is closed: nobody is waiting
                self._loop.call_soon_threadsafe(self._set_finished)

    def _set_finished(self) -> None:
        if not self.finished.done():
            self.finished.set_result(None)


@dataclass(frozen=True)
class _CachePlan:
    """The cache directory one sandbox mounts, and whether it owns (and removes) it."""

    path: Path
    fresh: bool


class DockerSandbox:
    """One work container (`Sandbox`). Created by `DockerDriver.create`."""

    def __init__(
        self,
        client: docker.DockerClient,
        container_id: str,
        *,
        name: str,
        run_id: RunId,
        workspace: Path,
        disk_limit_bytes: int,
        runtime: ContainerRuntime,
        warnings: Sequence[str],
        tail_bytes: int = DEFAULT_TAIL_BYTES,
        cache: _CachePlan | None = None,
        cache_store: CacheStore | None = None,
    ) -> None:
        self._client = client
        self._container_id = container_id
        self._name = name
        self._run_id = run_id
        self._workspace = workspace
        #: D6: the workspace is measured while a command runs and the command is killed past
        #: this many bytes. Public so tests can adjust them.
        self.disk_limit_bytes = disk_limit_bytes
        self.disk_poll_s = DISK_POLL_S
        self._meter = HostWorkspace(workspace)  # metadata only: no sandbox bound, no freeze
        self._cache = cache
        self._cache_store = cache_store
        self._cache_meter = HostWorkspace(cache.path) if cache is not None else None
        self._runtime = runtime
        self._warnings = list(warnings)
        self._destroyed = False
        self._state = _State.IDLE
        self._active = 0  # commands in flight; frozen() refuses while any runs
        #: Work that outlives the call that started it (the kill of a cancelled exec, a thaw
        #: whose caller was cancelled). `exec` and `frozen()` wait for it before starting.
        self._pending: set[asyncio.Task[Any]] = set()
        self._mechanism = FreezeMechanism.PAUSE
        self._main_pid = ""
        self.tail_bytes = tail_bytes

    def _setup(self) -> None:
        """Right after start: record the main process and find out how to freeze."""
        self._main_pid = self._find_main_pid()
        if not self._pause_works():
            self._mechanism = FreezeMechanism.KILL_ALL
            self._warnings.append(
                f"docker pause does not work under {self._runtime.value}: freezing kills every "
                "process in the container except init and its main process (§9.5)"
            )

    # -- Sandbox protocol

    @property
    def id(self) -> str:
        return self._name

    @property
    def container_id(self) -> str:
        return self._container_id

    @property
    def runtime(self) -> ContainerRuntime:
        return self._runtime

    @property
    def freeze_mechanism(self) -> FreezeMechanism:
        """PAUSE if `docker pause` worked when the container started (probed then), else
        KILL_ALL, with a warning recorded."""
        return self._mechanism

    @property
    def is_frozen(self) -> bool:
        """True only while every container process is stopped: never while freezing, thawing
        or broken, so `Workspace` refuses in all of those."""
        return self._state is _State.FROZEN

    @property
    def warnings(self) -> Sequence[str]:
        return tuple(self._warnings)

    def frozen(self) -> AbstractAsyncContextManager[None]:
        return self._frozen_cm()

    @asynccontextmanager
    async def _frozen_cm(self) -> AsyncIterator[None]:
        """One state machine with `exec`: IDLE -> FREEZING -> FROZEN -> THAWING -> IDLE, or
        BROKEN if the container could not be brought back. The state is claimed before the
        first await, so no `exec` can start while freezing; `is_frozen` is true only once the
        container is stopped and false from the first moment of thawing; the thaw runs as a
        shielded task, so a cancelled caller can neither leave the flag set nor stop it."""
        await self._drain_pending()
        if self._destroyed:
            raise RuntimeError("the sandbox was destroyed")
        if self._state is _State.BROKEN:
            raise RuntimeError(_BROKEN)
        if self._state is not _State.IDLE:
            raise RuntimeError("the sandbox is already frozen (or freezing or thawing)")
        if self._active:
            raise RuntimeError("cannot freeze while a command is running (§9.5)")
        self._state = _State.FREEZING  # no await since the checks: nothing slipped in
        step = self._background(asyncio.to_thread(self._enter_freeze))
        try:
            await asyncio.shield(step)
        except BaseException:
            # Cancelled or failed: the blocking step may still be running in its thread (a
            # pause about to land). Recover only once it has finished, in one tracked task.
            await asyncio.shield(self._background(self._recover_failed_freeze(step)))
            raise
        self._state = _State.FROZEN
        try:
            yield
        finally:
            self._state = _State.THAWING
            await asyncio.shield(self._background(self._thaw()))

    def _enter_freeze(self) -> None:
        """The blocking part of freezing: no install container, then pause or kill-all."""
        self._require_no_install_containers()
        if self._mechanism is FreezeMechanism.PAUSE:
            self._pause()
        elif not self._kill_all():
            raise RuntimeError(
                "could not stop every process in the sandbox: refusing host file access"
            )

    def _background(self, work: Awaitable[_T]) -> asyncio.Task[_T]:
        task = asyncio.ensure_future(work)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)
        return task

    async def _drain_pending(self) -> None:
        while self._pending:
            await asyncio.wait(set(self._pending))

    async def _thaw(self) -> None:
        if self._destroyed:
            return  # destroyed inside frozen(): there is nothing left to resume
        if self._mechanism is FreezeMechanism.KILL_ALL:
            self._state = _State.IDLE  # nothing to resume: the processes are gone
            return
        try:
            await asyncio.to_thread(self._unpause)
        except BaseException:
            self._state = _State.BROKEN
            raise
        self._state = _State.IDLE

    async def _recover_failed_freeze(self, step: asyncio.Future[None]) -> None:
        """Freezing failed or was cancelled part-way (a pause may have happened): wait for the
        blocking step to finish, undo a pause, and go back to IDLE only if the container is
        running and not paused."""
        with contextlib.suppress(BaseException):
            await step

        def recover() -> bool:
            api = self._client.api
            with contextlib.suppress(APIError):
                if api.inspect_container(self._container_id)["State"].get("Paused"):
                    api.unpause(self._container_id)
            try:
                state = api.inspect_container(self._container_id)["State"]
            except APIError:
                return False
            return bool(state.get("Running")) and not state.get("Paused")

        ok = await asyncio.to_thread(recover)
        self._state = _State.IDLE if ok else _State.BROKEN

    def _require_no_install_containers(self) -> None:
        flt = {"label": [f"{LABEL_RUN}={self._run_id}", f"{LABEL_ROLE}={ROLE_INSTALL}"]}
        found = self._client.api.containers(all=True, filters=flt, quiet=True)
        if found:
            raise RuntimeError(
                f"{len(found)} install container(s) of this run still exist: they must be gone "
                "before the host touches the workspace (§9.5)"
            )

    def _pause(self) -> None:
        api = self._client.api
        api.pause(self._container_id)
        if not api.inspect_container(self._container_id)["State"].get("Paused"):
            with contextlib.suppress(APIError):
                api.unpause(self._container_id)
            raise RuntimeError("docker pause returned but the container is not paused")

    def _unpause(self) -> None:
        api = self._client.api
        api.unpause(self._container_id)
        state = api.inspect_container(self._container_id)["State"]
        if state.get("Paused") or not state.get("Running"):
            raise RuntimeError("the sandbox container is broken: it did not resume after unpause")

    def _pause_works(self) -> bool:
        api = self._client.api
        try:
            api.pause(self._container_id)
            paused = bool(api.inspect_container(self._container_id)["State"].get("Paused"))
        except APIError:
            paused = False
        with contextlib.suppress(APIError):
            api.unpause(self._container_id)
        state = api.inspect_container(self._container_id)["State"]
        if state.get("Paused") or not state.get("Running"):
            raise RuntimeError("the sandbox container is broken: it did not resume after pause")
        return paused

    def _find_main_pid(self) -> str:
        pids = self._run_script(_MAIN_PID_SCRIPT, output=True).split()
        if len(pids) != 1:
            raise RuntimeError(f"expected one main process under init, found {pids}")
        return pids[0]

    def _kill_all(self) -> bool:
        try:
            self._run_script(_FREEZE_KILL_SCRIPT, self._main_pid, output=False)
        except _ScriptFailedError:
            return False
        return True

    def _run_script(self, script: str, *args: str, output: bool) -> str:
        """Run a driver-owned shell script in the container and wait for it; raises
        _ScriptFailedError unless it exits 0."""
        api = self._client.api
        created = api.exec_create(
            self._container_id,
            ["sh", "-c", script, "sh", *args],
            stdout=output,
            stderr=False,
            stdin=False,
            tty=False,
        )
        raw = api.exec_start(created["Id"], detach=False, stream=False)
        deadline = time.monotonic() + _KILL_EXEC_TIMEOUT_S
        while True:
            info = api.exec_inspect(created["Id"])
            if not info.get("Running") and info.get("ExitCode") is not None:
                break
            if time.monotonic() > deadline:
                raise _ScriptFailedError("timed out")
            time.sleep(0.05)
        if info["ExitCode"] != 0:
            raise _ScriptFailedError(f"exit code {info['ExitCode']}")
        return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else ""

    async def export_workspace(self, dest: Path) -> ExportManifest:
        """Copy the workspace, `.git` included, to `dest` and return the hash manifest (FR-21).

        Runs frozen, so the copy describes one instant; the sandbox is running again when this
        returns, and stays frozen until the copy thread has finished even if the caller is
        cancelled. After `destroy()` no container mounts the workspace (it was verified gone),
        so it is read without a freeze: that is how the workspace of a sandbox that could not
        be resumed (BROKEN) is saved, by destroying it first."""
        if self._destroyed:
            return await asyncio.to_thread(HostWorkspace(self._workspace).export_to, dest)
        if self._state is _State.BROKEN:
            raise RuntimeError(f"{_BROKEN}; to save its workspace, destroy it and export after")
        async with self.frozen():
            work = self._background(
                asyncio.to_thread(HostWorkspace(self._workspace, sandbox=self).export_to, dest)
            )
            try:
                return await asyncio.shield(work)
            finally:
                await _finished(work)  # never thaw under a thread that is still copying

    # -- exec

    async def exec(
        self,
        argv: Sequence[str],
        *,
        network: NetworkMode,
        timeout_s: int,
        cwd: str = "/workspace",
        env: Mapping[str, str] | None = None,
    ) -> ExecResult:
        if self._destroyed:
            raise RuntimeError("the sandbox was destroyed")
        if network is NetworkMode.REGISTRY:
            raise NotImplementedError("registry-mode exec needs the install container (P2-09)")
        if not argv or not all(isinstance(a, str) and a for a in argv):
            raise ValueError("argv must be a non-empty list of non-empty strings")
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if not cwd.startswith("/"):
            raise ValueError(f"cwd must be an absolute path in the container: {cwd!r}")
        problems = env_problems(env or {})
        if problems:
            raise ValueError("; ".join(problems))

        # Wait out work still finishing (a thaw whose caller was cancelled, the kill of an
        # exec that was cancelled twice), then check: no await from the check to the count.
        await self._drain_pending()
        self._require_exec_allowed()
        self._active += 1
        try:
            return await self._exec_counted(list(argv), timeout_s, cwd, env or {})
        finally:
            self._active -= 1

    def _require_exec_allowed(self) -> None:
        if self._destroyed:
            raise RuntimeError("the sandbox was destroyed")
        if self._state is _State.BROKEN:
            raise RuntimeError(_BROKEN)
        if self._state is not _State.IDLE:
            raise RuntimeError(
                "exec while the sandbox is frozen (or freezing or thawing): the host is "
                "touching files (§9.5)"
            )

    async def _exec_counted(
        self, argv: list[str], timeout_s: int, cwd: str, env: Mapping[str, str]
    ) -> ExecResult:
        result = await self._exec_once(argv, timeout_s, cwd, env)
        if _never_started(result):
            # The command did not run: processes left behind by an earlier command have used
            # up the pids limit, so not even `exec` can fork. Restarting clears them, and a
            # command that never ran is safe to run again.
            await self._restart()
            retry = await self._exec_once(argv, timeout_s, cwd, env)
            note = "[mpilot] the container had reached its pids limit and was restarted first.\n"
            return retry.model_copy(update={"stderr_tail": note + retry.stderr_tail})
        return result

    async def _exec_once(
        self, argv: list[str], timeout_s: int, cwd: str, env: Mapping[str, str]
    ) -> ExecResult:
        tag = secrets.token_hex(12)
        environment = [f"{k}={v}" for k, v in env.items()] + [f"{_EXEC_TAG_VAR}={tag}"]
        began = time.monotonic()
        try:
            exec_id = await asyncio.to_thread(self._exec_create, argv, environment, cwd)
            stream = _Stream(self._client, exec_id, self.tail_bytes)
        except APIError as exc:
            return await self._exec_refused(exc, began)

        timed_out = False
        disk_note: str | None = None
        disk_watch = asyncio.ensure_future(self._disk_watch())
        try:
            watched: set[asyncio.Future[Any]] = {stream.finished, disk_watch}
            done, _ = await asyncio.wait(
                watched,
                timeout=timeout_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stream.finished not in done:
                if disk_watch in done:
                    disk_note = disk_watch.result()
                else:
                    timed_out = True
                await self._kill(tag)
                await asyncio.wait({stream.finished}, timeout=KILL_GRACE_S)
        except asyncio.CancelledError:
            # `_kill` is a tracked task: a second cancellation cannot stop it half-way, and
            # `_pending` keeps `frozen()` and the next `exec` waiting until it has finished.
            await self._kill(tag)
            raise
        finally:
            disk_watch.cancel()
            await _finished(disk_watch)
        return await self._result(stream, began, timed_out=timed_out, disk_note=disk_note)

    # -- disk limit (D6)

    @property
    def cache_dir(self) -> Path | None:
        """The host directory mounted at `/cache`, or None when this driver has no cache root."""
        return self._cache.path if self._cache is not None else None

    def _disk_usage(self) -> int:
        """Workspace plus package cache: one 20 GB budget (§9.3). The cache is measured with
        what is left of the limit, so the walk stops early once the total is over it."""
        used = self._meter.disk_usage_bytes(self.disk_limit_bytes)
        if self._cache_meter is not None and used <= self.disk_limit_bytes:
            used += self._cache_meter.disk_usage_bytes(self.disk_limit_bytes - used)
        return used

    async def _disk_watch(self) -> str:
        """Poll the workspace while a command runs; return why, once it must be stopped. Never
        returns while all is well (the caller cancels it when the command ends). A workspace
        that cannot be measured stops the command too: running unguarded is the worse failure."""
        failures = 0
        while True:
            await asyncio.sleep(self.disk_poll_s)
            try:
                used = await asyncio.to_thread(self._disk_usage)
            except PathEscapeError as exc:
                return f"the workspace tree cannot be measured ({exc}); treated as over the limit"
            except OSError:
                failures += 1
                if failures >= _DISK_MEASURE_FAILURES:
                    return (
                        "the workspace's disk use could not be measured; the command was "
                        "stopped rather than run unguarded"
                    )
                continue
            failures = 0
            if used > self.disk_limit_bytes:
                what = "the workspace" if self._cache is None else "the workspace and package cache"
                return (
                    f"{what} passed the disk limit of {human_bytes(self.disk_limit_bytes)} "
                    f"({human_bytes(used)} used)"
                )

    def _exec_create(self, argv: list[str], environment: list[str], cwd: str) -> str:
        created = self._client.api.exec_create(
            self._container_id,
            argv,
            stdout=True,
            stderr=True,
            stdin=False,
            tty=False,
            environment=environment,
            workdir=cwd,
        )
        return str(created["Id"])

    async def _exec_refused(self, exc: APIError, began: float) -> ExecResult:
        """Docker would not start the command (runc under pids pressure, for example), or the
        container is gone."""
        if not await self._is_running():
            return await self._after_container_death(began, [str(exc)])
        return _result(
            126,
            "",
            f"[mpilot] the command could not be started: {exc.explanation or exc}\n",
            began,
        )

    async def _result(
        self, stream: _Stream, began: float, *, timed_out: bool, disk_note: str | None
    ) -> ExecResult:
        out, err = stream.stdout, stream.stderr
        truncated = out.truncated or err.truncated
        if timed_out:
            return _result(-1, out.text(), err.text(), began, truncated, timed_out=True)
        if disk_note is not None:
            note = f"[mpilot] the command was killed: {disk_note}.\n"
            return _result(
                -1, out.text(), err.text() + note, began, truncated, killed_disk_limit=True
            )
        if stream.error is not None and not isinstance(stream.error, APIError | OSError):
            raise stream.error
        if stream.exit_code is not None and await self._is_running(settle=stream.exit_code):
            return _result(stream.exit_code, out.text(), err.text(), began, truncated)
        # No exit code, or the container is not running: the sandbox died under the command.
        return await self._after_container_death(
            began, [err.text()], stdout=out.text(), truncated=truncated
        )

    async def _after_container_death(
        self, began: float, stderr: list[str], *, stdout: str = "", truncated: bool = False
    ) -> ExecResult:
        """gVisor kills the whole sandbox when a command passes the memory or pids limit.
        Bring the container back (its workspace is a bind mount, so nothing on disk is lost)
        and report the command as killed, exit code 137 as the kernel's OOM killer would."""
        await self._restart()
        note = (
            "[mpilot] the sandbox container was killed while the command ran (memory or pids "
            "limit) and has been restarted; every process in it was lost.\n"
        )
        err = "".join(s for s in stderr if s) + ("" if not any(stderr) else "\n") + note
        return _result(137, stdout, err, began, truncated)

    # -- killing

    async def _is_running(self, *, settle: int | None = None) -> bool:
        """Whether the container is running. After exit code 128 or 137 (what a dying gVisor
        sandbox or a killed command reports) Docker's own state can lag behind by a moment, so
        wait up to `_SETTLE_S` for it to show the container stopped before saying "running"."""

        def check() -> bool:
            deadline = time.monotonic() + (_SETTLE_S if settle in _DEATH_CODES else 0.0)
            while True:
                try:
                    state = self._client.api.inspect_container(self._container_id)["State"]
                except NotFound:
                    raise RuntimeError(
                        "the sandbox container is gone (removed behind our back)"
                    ) from None
                if not state.get("Running") or time.monotonic() >= deadline:
                    return bool(state.get("Running"))
                time.sleep(0.1)

        return await asyncio.to_thread(check)

    async def _restart(self) -> None:
        def restart() -> None:
            api = self._client.api
            with contextlib.suppress(APIError):
                api.kill(self._container_id)
            for _ in range(3):
                try:
                    api.start(self._container_id)
                    break
                except NotFound:
                    raise RuntimeError(
                        "the sandbox container is gone (removed behind our back)"
                    ) from None
                except APIError:
                    time.sleep(_RESTART_PAUSE_S)
            else:
                raise RuntimeError("the sandbox container is broken: it would not restart")
            if not api.inspect_container(self._container_id)["State"].get("Running"):
                raise RuntimeError("the sandbox container is broken: it would not restart")
            self._main_pid = self._find_main_pid()  # a restart starts a new main process

        # Tracked, so a cancelled caller can't let a later freeze overlap the kill + start.
        await asyncio.shield(self._background(asyncio.to_thread(restart)))

    async def _kill(self, tag: str) -> None:
        """Kill every process the exec started. If the kill command cannot run or cannot
        finish (the pids limit is used up), restart the container: that kills everything.
        Tracked in `_pending`, so no freeze can start until it has finished."""
        await asyncio.shield(self._background(self._kill_now(tag)))

    async def _kill_now(self, tag: str) -> None:
        try:
            clean = await asyncio.wait_for(
                asyncio.to_thread(self._run_killer, tag), _KILL_EXEC_TIMEOUT_S
            )
        except (TimeoutError, APIError, OSError):
            clean = False
        if not clean:
            await self._restart()

    def _run_killer(self, tag: str) -> bool:
        # `_run_script` waits on exec_inspect: with no output attached, `exec_start` can
        # return before the command has finished.
        try:
            self._run_script(_KILL_SCRIPT, tag, output=False)
        except _ScriptFailedError:
            return False
        return True

    # -- destroy

    async def destroy(self) -> None:
        if self._destroyed:
            return
        # Off FROZEN first: forced removal may unpause the container on its way out, and
        # `is_frozen` must never be true while a container process can run.
        self._state = _State.BROKEN

        def remove() -> None:
            api = self._client.api
            _force_remove(api, self._container_id)
            try:
                api.inspect_container(self._container_id)
            except NotFound:
                return
            raise RuntimeError(f"container {self._name} is still there after removal")

        await asyncio.to_thread(remove)
        self._destroyed = True
        if self._cache is not None and self._cache.fresh and self._cache_store is not None:
            # After the container is verified gone: nothing can be writing to it any more.
            await asyncio.to_thread(self._cache_store.remove_fresh, self._cache.path)


class _State(StrEnum):
    IDLE = "idle"
    FREEZING = "freezing"
    FROZEN = "frozen"
    THAWING = "thawing"
    BROKEN = "broken"


_BROKEN: Final = (
    "the sandbox is broken: it could not be resumed after a freeze; destroy it and restore "
    "the workspace into a new one"
)


class _ScriptFailedError(MpilotError):
    """A driver-owned script in the container did not exit 0 in time."""


async def _finished(task: asyncio.Task[Any]) -> None:
    """Wait for `task` to end even if this coroutine is cancelled meanwhile, and mark its
    exception as retrieved (the caller already has it, or is being cancelled)."""
    while not task.done():
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait({task})
    if not task.cancelled():
        task.exception()


def _never_started(result: ExecResult) -> bool:
    """Docker reports "the process could not be created" as exit code 128 plus the runtime's
    message as output; only the resource-exhaustion kind is worth a restart and a retry."""
    if result.exit_code != 128 or result.timed_out:
        return False
    text = result.stdout_tail + result.stderr_tail
    return "OCI runtime exec failed" in text and bool(
        re.search(r"temporarily unavailable|cannot allocate memory", text, re.I)
    )


def _result(
    exit_code: int,
    stdout: str,
    stderr: str,
    began: float,
    truncated: bool = False,
    *,
    timed_out: bool = False,
    killed_disk_limit: bool = False,
) -> ExecResult:
    return ExecResult(
        exit_code=exit_code,
        stdout_tail=stdout,
        stderr_tail=stderr,
        duration_s=max(0.0, time.monotonic() - began),
        truncated=truncated,
        timed_out=timed_out,
        killed_disk_limit=killed_disk_limit,
    )


class DockerDriver:
    """`SandboxDriver` over the Docker daemon. `client` defaults to `docker.from_env()`;
    `runsc_name`/`runc_name` are the runtime names registered with the daemon (tests inject
    faults through them)."""

    def __init__(
        self,
        client: docker.DockerClient | None = None,
        *,
        runsc_name: str = "runsc",
        runc_name: str = "runc",
        uid: int | None = None,
        gid: int | None = None,
        tail_bytes: int = DEFAULT_TAIL_BYTES,
        cache_root: Path | None = None,
    ) -> None:
        self._uid = os.getuid() if uid is None else uid
        self._gid = os.getgid() if gid is None else gid
        if self._uid == 0 or self._gid == 0:
            raise ValueError(
                "refusing to run the sandbox as root: the container user is the host user"
            )
        self._client = client if client is not None else docker.from_env(timeout=120)
        self._runsc = runsc_name
        self._runc = runc_name
        self._tail_bytes = tail_bytes
        #: `runs/cache/`. Without it no cache is mounted (the lifecycle tests run that way).
        self._cache_store = CacheStore(cache_root) if cache_root is not None else None

    # -- runtime detection

    def detect_runtime(self) -> RuntimeInfo:
        runtimes: Mapping[str, Mapping[str, Any]] = self._client.info().get("Runtimes") or {}
        if self._runsc in runtimes:
            return RuntimeInfo(
                runtime=ContainerRuntime.RUNSC,
                version=_runsc_version(str(runtimes[self._runsc].get("path", ""))),
            )
        if self._runc not in runtimes:
            raise RuntimeError(
                f"Docker has neither {self._runsc!r} nor {self._runc!r} registered as a runtime"
            )
        return RuntimeInfo(
            runtime=ContainerRuntime.RUNC,
            warnings=(
                f"gVisor ({self._runsc}) is not registered with Docker: running on runc, "
                "which shares the host kernel (NFR-10)",
            ),
        )

    # -- create

    async def create(self, spec: SandboxSpec) -> Sandbox:
        return await asyncio.to_thread(self._create, spec)

    def _preflight(self, spec: SandboxSpec) -> None:
        """What can be refused before touching the workspace: the env and the image."""
        problems = env_problems(spec.env)
        if problems:
            raise ValueError("; ".join(problems))
        try:
            self._client.images.get(spec.image)
        except ImageNotFound:
            raise RuntimeError(
                f"image {spec.image} is not available locally "
                "(build it with scripts/build-images.sh)"
            ) from None
        if self._cache_store is not None:
            if spec.cache is CacheMode.HASH_VERIFIED:
                raise NotImplementedError(
                    "a hash-verified cache is built with final verification (P3-13); "
                    "use CacheMode.FRESH until then"
                )
            if spec.cache is CacheMode.RUN:
                check_key(spec.cache_key)

    def _cache_plan(self, spec: SandboxSpec) -> _CachePlan | None:
        """The cache directory this sandbox mounts (created here), or None without a root."""
        store = self._cache_store
        if store is None:
            return None
        if spec.cache is CacheMode.FRESH:
            return _CachePlan(store.fresh_dir(str(spec.run_id)), fresh=True)
        path = store.run_dir(spec.cache_key)
        if HostWorkspace(path).disk_usage_bytes() > spec.limits.disk_gb * GIB:
            raise RuntimeError(
                f"the package cache {path.name[:12]}... is already over the disk limit of "
                f"{spec.limits.disk_gb} GiB; remove it (a command would be killed at once)"
            )
        return _CachePlan(path, fresh=False)

    def _create(self, spec: SandboxSpec) -> DockerSandbox:
        self._preflight(spec)
        _check_workspace(spec.workspace_host_path)
        plan = self._cache_plan(spec)
        try:
            return self._create_with(spec, plan)
        except BaseException:
            if plan is not None and plan.fresh and self._cache_store is not None:
                with contextlib.suppress(OSError, CacheError):
                    self._cache_store.remove_fresh(plan.path)
            raise

    def _create_with(self, spec: SandboxSpec, plan: _CachePlan | None) -> DockerSandbox:

        warnings: list[str] = []
        if spec.prefer_runtime is ContainerRuntime.RUNC:
            warnings.append("runc was requested: the container shares the host kernel (NFR-10)")
            container = self._start(spec, self._runc, plan)
            runtime = ContainerRuntime.RUNC
        else:
            info = self.detect_runtime()
            warnings.extend(info.warnings)
            if info.runtime is ContainerRuntime.RUNSC:
                try:
                    container = self._start(spec, self._runsc, plan)
                    runtime = ContainerRuntime.RUNSC
                except APIError as exc:
                    if not _RUNTIME_FAILURE.search(str(exc)):
                        raise
                    warnings.append(
                        f"gVisor ({self._runsc}) failed to start the container, falling back "
                        f"to runc, which shares the host kernel (NFR-10): {exc.explanation or exc}"
                    )
                    container = self._start(spec, self._runc, plan)
                    runtime = ContainerRuntime.RUNC
            else:
                container = self._start(spec, self._runc, plan)
                runtime = ContainerRuntime.RUNC
        sandbox = DockerSandbox(
            self._client,
            str(container.id),
            name=str(container.name),
            run_id=spec.run_id,
            workspace=spec.workspace_host_path,
            disk_limit_bytes=spec.limits.disk_gb * GIB,
            runtime=runtime,
            warnings=warnings,
            tail_bytes=self._tail_bytes,
            cache=plan,
            cache_store=self._cache_store,
        )
        try:
            sandbox._setup()
        except BaseException:
            with contextlib.suppress(DockerException):
                self._client.api.remove_container(str(container.id), v=True, force=True)
            raise
        return sandbox

    def _start(self, spec: SandboxSpec, runtime_name: str, plan: _CachePlan | None) -> Any:
        """Create and start the work container; on failure leave nothing behind."""
        name = f"mpilot-work-{secrets.token_hex(6)}"
        limits = spec.limits
        volumes = {str(spec.workspace_host_path): {"bind": "/workspace", "mode": "rw"}}
        environment = {**spec.env, "HOME": CONTAINER_HOME}
        if plan is not None:
            # Read-only here; only an install container mounts it read-write (§9.3).
            volumes.update(cache_volume(plan.path, CacheRole.WORK))
            environment.update(cache_env(CacheRole.WORK))
        container = None
        try:
            container = self._client.containers.create(
                spec.image,
                command=["sleep", "2147483647"],
                name=name,
                runtime=runtime_name,
                init=True,
                user=f"{self._uid}:{self._gid}",
                working_dir="/workspace",
                environment=environment,
                read_only=True,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges"],
                privileged=False,
                network_mode="none",
                pids_limit=limits.pids,
                mem_limit=limits.memory_gb * 1024**3,
                memswap_limit=limits.memory_gb * 1024**3,
                nano_cpus=int(limits.cpus * 1_000_000_000),
                tmpfs={CONTAINER_HOME: tmpfs_options(limits.memory_gb)},
                volumes=volumes,
                log_config=LogConfig(type=LogConfig.types.NONE),
                labels={LABEL_RUN: str(spec.run_id), LABEL_ROLE: ROLE_WORK},
                detach=True,
            )
            container.start()
            container.reload()
            if container.status != "running":
                raise APIError(f"container {name} did not stay running ({container.status})")
            return container
        except BaseException:
            with contextlib.suppress(DockerException):
                self._client.api.remove_container(name, v=True, force=True)
            raise

    # -- restore and reap

    async def restore(
        self, spec: SandboxSpec, manifest: ExportManifest, *, expected_digest: str
    ) -> Sandbox:
        return await asyncio.to_thread(self._restore, spec, manifest, expected_digest)

    def _restore(
        self, spec: SandboxSpec, manifest: ExportManifest, expected_digest: str
    ) -> DockerSandbox:
        """Rebuild `spec.workspace_host_path` (absent or empty; never merged into) from the
        export, refusing unless it hashes to the checkpoint's `expected_digest`, then start a
        sandbox on it. Nothing is written if the digest, env or image is refused; if the
        copy or the start fails, the workspace is emptied again so a retry is possible."""
        if manifest.digest() != expected_digest:
            raise ExportIntegrityError(
                "the export's manifest does not match the checkpoint's digest"
            )
        self._preflight(spec)
        path = spec.workspace_host_path
        if not path.is_absolute():
            raise ValueError(f"workspace path must be absolute: {path}")
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            path.parent.lstat()  # FileNotFoundError if the parent is missing too
            path.mkdir(mode=0o755)
        else:
            if stat.S_ISLNK(mode):
                raise ValueError(f"workspace path is a symlink: {path}")
            if not stat.S_ISDIR(mode):
                raise NotADirectoryError(f"workspace path is not a directory: {path}")
        workspace = HostWorkspace(path)
        workspace.restore_from(manifest, expected_digest)  # empties it again if it fails
        try:
            return self._create(spec)
        except BaseException:
            workspace.clear()
            raise

    async def reap(self, run_id: RunId) -> int:
        return await asyncio.to_thread(self._reap, run_id)

    def _reap(self, run_id: RunId) -> int:
        api = self._client.api
        flt = {"label": [f"{LABEL_RUN}={run_id}"]}
        removed = 0
        for found in api.containers(all=True, filters=flt, quiet=True):
            _force_remove(api, found["Id"])
            removed += 1
        for net in api.networks(filters=flt):
            with contextlib.suppress(NotFound):
                api.remove_network(net["Id"])
            removed += 1
        if self._cache_store is not None:
            self._cache_store.sweep_fresh(str(run_id))  # a crashed run's fresh caches
        leftovers = api.containers(all=True, filters=flt, quiet=True) + api.networks(filters=flt)
        if leftovers:
            raise RuntimeError(f"{len(leftovers)} object(s) of run {run_id} survived reap")
        return removed


def _force_remove(api: Any, container_id: str) -> None:
    """Remove a container whatever state it is in. A paused gVisor container can refuse a
    forced removal ("PID ... is zombie and can not be killed"): unpause and kill it first."""
    for attempt in range(3):
        try:
            api.remove_container(container_id, v=True, force=True)
            return
        except NotFound:
            return
        except APIError:
            if attempt == 2:
                raise
        with contextlib.suppress(APIError):
            api.unpause(container_id)
        with contextlib.suppress(APIError):
            api.kill(container_id)
        time.sleep(_RESTART_PAUSE_S)


def _check_workspace(path: Path) -> None:
    if not path.is_absolute():
        raise ValueError(f"workspace path must be absolute: {path}")
    mode = path.lstat().st_mode  # FileNotFoundError if missing
    if stat.S_ISLNK(mode):
        raise ValueError(f"workspace path is a symlink: {path}")
    if not stat.S_ISDIR(mode):
        raise NotADirectoryError(f"workspace path is not a directory: {path}")


def _runsc_version(path: str) -> str:
    """`runsc --version`'s first line, or "" if it can't be read. The binary is the one the
    daemon is registered with, started through the single process helper."""
    if not os.path.isabs(path):
        return ""
    try:
        res = spawn.run([path, "--version"], env={}, timeout_s=10)
    except (spawn.SpawnError, OSError):
        return ""
    lines = res.stdout.decode("utf-8", errors="replace").strip().splitlines()
    return lines[0] if res.returncode == 0 and lines else ""


def _static_conformance() -> None:
    """mypy proves `DockerDriver` and `DockerSandbox` satisfy the frozen protocols."""
    driver: SandboxDriver = DockerDriver.__new__(DockerDriver)
    sandbox: Sandbox = DockerSandbox.__new__(DockerSandbox)
    del driver, sandbox
