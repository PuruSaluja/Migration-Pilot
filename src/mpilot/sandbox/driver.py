"""Docker driver for the sandbox: the work container's lifecycle and `exec` (SPEC §9.2, §9.3,
NFR-6, NFR-10).

Scope of P2-02. Built here: runtime detection (gVisor `runsc`, else `runc` with a recorded
warning), `create` with the §9.3 hardening and limits, `exec` with output tails, timeouts and
cancellation, `destroy`, `reap`. Owned by later tasks, and refused with `NotImplementedError`
until then: `frozen()` (P2-03), `export_workspace`/`restore` (P2-04), disk polling (P2-05),
package-cache mounts (P2-07), `NetworkMode.REGISTRY` and the install container (P2-09).

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
from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any, Final

import docker
from docker.errors import APIError, DockerException, ImageNotFound, NotFound
from docker.types import LogConfig

from mpilot.core import spawn
from mpilot.core.types import NetworkMode, RunId
from mpilot.models.artifacts import ExportManifest
from mpilot.sandbox.base import (
    ContainerRuntime,
    ExecResult,
    FreezeMechanism,
    RuntimeInfo,
    Sandbox,
    SandboxDriver,
    SandboxSpec,
    sandbox_env_problems,
)

LABEL_RUN: Final = "mpilot.run_id"
LABEL_ROLE: Final = "mpilot.role"
ROLE_WORK: Final = "work"

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
#: Provisional /tmp cap. The spec gives no number; P2-05 owns the final value and its test.
TMPFS_TMP: Final = "rw,nosuid,nodev,size=1g,mode=1777"
_EXEC_TAG_VAR: Final = "_MPILOT_EXEC"
#: Per-stream bytes of command output kept (the end of it), unless the sandbox is given another.
DEFAULT_TAIL_BYTES: Final = 32 * 1024
#: After the kill, how long to wait for the output stream to end before abandoning it.
KILL_GRACE_S: Final = 5.0
_KILL_EXEC_TIMEOUT_S: Final = 20.0
_RESTART_PAUSE_S: Final = 0.2
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


class DockerSandbox:
    """One work container (`Sandbox`). Created by `DockerDriver.create`."""

    def __init__(
        self,
        client: docker.DockerClient,
        container_id: str,
        *,
        name: str,
        runtime: ContainerRuntime,
        warnings: Sequence[str],
        tail_bytes: int = DEFAULT_TAIL_BYTES,
    ) -> None:
        self._client = client
        self._container_id = container_id
        self._name = name
        self._runtime = runtime
        self._warnings = list(warnings)
        self._destroyed = False
        self.tail_bytes = tail_bytes

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
        """Provisional: P2-03 decides and records which mechanism works under runsc."""
        return FreezeMechanism.PAUSE

    @property
    def is_frozen(self) -> bool:
        return False

    @property
    def warnings(self) -> Sequence[str]:
        return tuple(self._warnings)

    def frozen(self) -> AbstractAsyncContextManager[None]:
        raise NotImplementedError("Sandbox.frozen() is P2-03")

    async def export_workspace(self, dest: Path) -> ExportManifest:
        raise NotImplementedError("Sandbox.export_workspace() is P2-04")

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

        result = await self._exec_once(list(argv), timeout_s, cwd, env or {})
        if _never_started(result):
            # The command did not run: processes left behind by an earlier command have used
            # up the pids limit, so not even `exec` can fork. Restarting clears them, and a
            # command that never ran is safe to run again.
            await self._restart()
            retry = await self._exec_once(list(argv), timeout_s, cwd, env or {})
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
        try:
            await asyncio.wait({stream.finished}, timeout=timeout_s)
            if not stream.finished.done():
                timed_out = True
                await self._kill(tag)
                await asyncio.wait({stream.finished}, timeout=KILL_GRACE_S)
        except asyncio.CancelledError:
            await asyncio.shield(self._kill(tag))
            raise
        return await self._result(stream, began, timed_out=timed_out)

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

    async def _result(self, stream: _Stream, began: float, *, timed_out: bool) -> ExecResult:
        out, err = stream.stdout, stream.stderr
        truncated = out.truncated or err.truncated
        if timed_out:
            return _result(-1, out.text(), err.text(), began, truncated, timed_out=True)
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

        await asyncio.to_thread(restart)

    async def _kill(self, tag: str) -> None:
        """Kill every process the exec started. If the kill command cannot run or cannot
        finish (the pids limit is used up), restart the container: that kills everything."""
        try:
            clean = await asyncio.wait_for(
                asyncio.to_thread(self._run_killer, tag), _KILL_EXEC_TIMEOUT_S
            )
        except (TimeoutError, APIError, OSError):
            clean = False
        if not clean:
            await self._restart()

    def _run_killer(self, tag: str) -> bool:
        api = self._client.api
        created = api.exec_create(
            self._container_id,
            ["sh", "-c", _KILL_SCRIPT, "sh", tag],
            stdout=False,
            stderr=False,
            stdin=False,
            tty=False,
        )
        api.exec_start(created["Id"], detach=False, stream=False)
        # With no output attached, `exec_start` can return before the command has finished.
        deadline = time.monotonic() + _KILL_EXEC_TIMEOUT_S
        while True:
            info = api.exec_inspect(created["Id"])
            if not info.get("Running") and info.get("ExitCode") is not None:
                return bool(info["ExitCode"] == 0)
            if time.monotonic() > deadline:
                return False
            time.sleep(0.05)

    # -- destroy

    async def destroy(self) -> None:
        if self._destroyed:
            return

        def remove() -> None:
            api = self._client.api
            with contextlib.suppress(NotFound):
                api.remove_container(self._container_id, v=True, force=True)
            try:
                api.inspect_container(self._container_id)
            except NotFound:
                return
            raise RuntimeError(f"container {self._name} is still there after removal")

        await asyncio.to_thread(remove)
        self._destroyed = True


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
) -> ExecResult:
    return ExecResult(
        exit_code=exit_code,
        stdout_tail=stdout,
        stderr_tail=stderr,
        duration_s=max(0.0, time.monotonic() - began),
        truncated=truncated,
        timed_out=timed_out,
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

    def _create(self, spec: SandboxSpec) -> DockerSandbox:
        problems = env_problems(spec.env)
        if problems:
            raise ValueError("; ".join(problems))
        _check_workspace(spec.workspace_host_path)
        try:
            self._client.images.get(spec.image)
        except ImageNotFound:
            raise RuntimeError(
                f"image {spec.image} is not available locally "
                "(build it with the image script, P2-07)"
            ) from None

        warnings: list[str] = []
        if spec.prefer_runtime is ContainerRuntime.RUNC:
            warnings.append("runc was requested: the container shares the host kernel (NFR-10)")
            container = self._start(spec, self._runc)
            runtime = ContainerRuntime.RUNC
        else:
            info = self.detect_runtime()
            warnings.extend(info.warnings)
            if info.runtime is ContainerRuntime.RUNSC:
                try:
                    container = self._start(spec, self._runsc)
                    runtime = ContainerRuntime.RUNSC
                except APIError as exc:
                    if not _RUNTIME_FAILURE.search(str(exc)):
                        raise
                    warnings.append(
                        f"gVisor ({self._runsc}) failed to start the container, falling back "
                        f"to runc, which shares the host kernel (NFR-10): {exc.explanation or exc}"
                    )
                    container = self._start(spec, self._runc)
                    runtime = ContainerRuntime.RUNC
            else:
                container = self._start(spec, self._runc)
                runtime = ContainerRuntime.RUNC
        return DockerSandbox(
            self._client,
            str(container.id),
            name=str(container.name),
            runtime=runtime,
            warnings=warnings,
            tail_bytes=self._tail_bytes,
        )

    def _start(self, spec: SandboxSpec, runtime_name: str) -> Any:
        """Create and start the work container; on failure leave nothing behind."""
        name = f"mpilot-work-{secrets.token_hex(6)}"
        limits = spec.limits
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
                environment={**spec.env, "HOME": CONTAINER_HOME},
                read_only=True,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges"],
                privileged=False,
                network_mode="none",
                pids_limit=limits.pids,
                mem_limit=limits.memory_gb * 1024**3,
                memswap_limit=limits.memory_gb * 1024**3,
                nano_cpus=int(limits.cpus * 1_000_000_000),
                tmpfs={CONTAINER_HOME: TMPFS_TMP},
                volumes={str(spec.workspace_host_path): {"bind": "/workspace", "mode": "rw"}},
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
        raise NotImplementedError("SandboxDriver.restore() is P2-04")

    async def reap(self, run_id: RunId) -> int:
        return await asyncio.to_thread(self._reap, run_id)

    def _reap(self, run_id: RunId) -> int:
        api = self._client.api
        flt = {"label": [f"{LABEL_RUN}={run_id}"]}
        removed = 0
        for found in api.containers(all=True, filters=flt, quiet=True):
            with contextlib.suppress(NotFound):
                api.remove_container(found["Id"], v=True, force=True)
            removed += 1
        for net in api.networks(filters=flt):
            with contextlib.suppress(NotFound):
                api.remove_network(net["Id"])
            removed += 1
        leftovers = api.containers(all=True, filters=flt, quiet=True) + api.networks(filters=flt)
        if leftovers:
            raise RuntimeError(f"{len(leftovers)} object(s) of run {run_id} survived reap")
        return removed


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
