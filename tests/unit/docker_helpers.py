"""Shared fixtures for the real-Docker sandbox tests (P2-02, P2-03).

`busybox:1.36` (pulled by `scripts/setup-dev.sh`) stands in for the per-ecosystem images
until P2-06/P2-07 build them. Containers are labelled with a per-test run id and removed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import docker
import pytest
from docker.errors import APIError

from mpilot.core.types import NetworkMode, RunId, new_run_id
from mpilot.sandbox import base as sb
from mpilot.sandbox.base import CacheMode, ContainerRuntime, ResourceLimits, SandboxSpec
from mpilot.sandbox.driver import LABEL_RUN, DockerDriver, DockerSandbox

IMAGE_REF = "busybox:1.36"
NONE = NetworkMode.NONE


@pytest.fixture(scope="session")
def client() -> Iterator[docker.DockerClient]:
    c = docker.from_env(timeout=120)
    yield c
    c.close()


@pytest.fixture(scope="session")
def image(client: docker.DockerClient) -> str:
    digests = client.images.get(IMAGE_REF).attrs["RepoDigests"]
    assert digests, f"{IMAGE_REF} has no digest: run scripts/setup-dev.sh"
    return str(digests[0])


@pytest.fixture
def run_id(client: docker.DockerClient) -> Iterator[RunId]:
    rid = new_run_id()
    yield rid
    for c in client.containers.list(all=True, filters={"label": f"{LABEL_RUN}={rid}"}):
        c.remove(force=True, v=True)
    for n in client.networks.list(filters={"label": f"{LABEL_RUN}={rid}"}):
        n.remove()


def make_spec(
    rid: RunId,
    image: str,
    workspace: Path,
    *,
    pids: int = 64,
    memory_gb: int = 1,
    prefer: ContainerRuntime = ContainerRuntime.RUNSC,
    env: dict[str, str] | None = None,
    cache_key: str = "lock-hash",
    cache: CacheMode = CacheMode.RUN,
    from_commit: str | None = None,
    disk_gb: int = 1,
) -> SandboxSpec:
    return SandboxSpec(
        run_id=rid,
        image=image,
        limits=ResourceLimits(cpus=1, memory_gb=memory_gb, disk_gb=disk_gb, pids=pids),
        workspace_host_path=workspace,
        env=env or {},
        cache_key=cache_key,
        cache=cache,
        from_commit=from_commit,
        prefer_runtime=prefer,
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "workspace"
    path.mkdir()
    return path


@pytest.fixture
def driver(client: docker.DockerClient) -> DockerDriver:
    return DockerDriver(client)


def inspect(client: docker.DockerClient, sandbox: sb.Sandbox) -> dict[str, Any]:
    assert isinstance(sandbox, DockerSandbox)
    return dict(client.api.inspect_container(sandbox.container_id))


@pytest.fixture
def sandbox(
    driver: DockerDriver, run_id: RunId, image: str, workspace: Path
) -> Iterator[sb.Sandbox]:
    box = asyncio.run(driver.create(make_spec(run_id, image, workspace)))
    yield box
    asyncio.run(box.destroy())


class Runner:
    """Run coroutines of one sandbox on one event loop (a sandbox is bound to its loop)."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()

    def __call__(self, coro: Any) -> Any:
        return self.loop.run_until_complete(coro)


@pytest.fixture
def run() -> Iterator[Runner]:
    r = Runner()
    yield r
    r.loop.close()


class FaultyContainers:
    """`client.containers` whose runsc creation fails, as a broken gVisor install does."""

    def __init__(self, real: Any, log: list[str]) -> None:
        self._real, self._log = real, log

    def create(self, *args: Any, **kwargs: Any) -> Any:
        self._log.append(str(kwargs.get("runtime")))
        if kwargs.get("runtime") == "runsc":
            raise APIError("OCI runtime create failed: runsc: fault injected")
        return self._real.create(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class FaultyClient:
    def __init__(self, real: docker.DockerClient, log: list[str]) -> None:
        self._real = real
        self.containers = FaultyContainers(real.containers, log)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


STRAY_PIDS = (
    'echo "$$ $PPID"; '
    'for p in /proc/[0-9]*; do echo "${p#/proc/} $(tr "\\0" " " < $p/cmdline)"; done'
)
_RESIDENT = ("/sbin/docker-init", "sleep 2147483647")  # init and the container's main process


def alive(sandbox: sb.Sandbox, run: Runner) -> list[str]:
    """Pids of processes in the container besides init, its main process and the probe (and
    the probe's parent: gVisor runs an exec through one). Busybox runs a backgrounded `sleep`
    inside a forked `sh`, so names and arguments tell nothing; only the pids are compared."""
    res = run(sandbox.exec(["sh", "-c", STRAY_PIDS], network=NONE, timeout_s=30))
    first, *rows = res.stdout_tail.splitlines()
    own, parent = first.split()
    stray = []
    for row in rows:
        pid, _, cmd = row.partition(" ")
        if pid not in (own, parent) and not cmd.startswith(_RESIDENT):
            stray.append(pid)
    return stray


def started_at(client: docker.DockerClient, sandbox: sb.Sandbox) -> str:
    """When the container last started: it changes if the container was restarted."""
    return str(inspect(client, sandbox)["State"]["StartedAt"])
