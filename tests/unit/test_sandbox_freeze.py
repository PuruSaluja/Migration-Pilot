"""P2-03: sandbox hardening verified from inside, `frozen()`, uid mapping (SPEC §9.3, §9.5,
NFR-6).

The host touches workspace files only while every container mounting them is stopped:
`docker pause` if it works under the runtime (probed at create), else every process but init
and the main process is killed and verified gone. Real Docker, real gVisor.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import textwrap
import time
from pathlib import Path
from typing import Any

import docker
import pytest
from docker.errors import APIError
from docker_helpers import (  # noqa: F401  (fixtures are used by name)
    NONE,
    Runner,
    alive,
    client,
    driver,
    image,
    inspect,
    make_spec,
    run,
    run_id,
    sandbox,
    workspace,
)

from mpilot.core.errors import PathEscapeError
from mpilot.core.types import RunId
from mpilot.models.artifacts import ArtifactRules
from mpilot.sandbox import base as sb
from mpilot.sandbox.base import ContainerRuntime, FreezeMechanism
from mpilot.sandbox.driver import (
    CONTAINER_HOME,
    LABEL_ROLE,
    LABEL_RUN,
    ROLE_INSTALL,
    DockerDriver,
    DockerSandbox,
)
from mpilot.sandbox.workspace import HostWorkspace

RUNTIMES = pytest.mark.parametrize(
    "runtime", [ContainerRuntime.RUNSC, ContainerRuntime.RUNC], ids=["runsc", "runc"]
)


class _NoPauseApi:
    """The low-level API with `pause` failing, as it would under a runtime without it."""

    def __init__(self, real: Any) -> None:
        self._real = real

    def pause(self, container: str) -> None:
        raise APIError("pause is not supported by this runtime (fault injected)")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class _NoPauseClient:
    def __init__(self, real: docker.DockerClient) -> None:
        self._real = real
        self.api = _NoPauseApi(real.api)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def _box(
    client: docker.DockerClient,
    run_id: RunId,
    image: str,
    workspace: Path,
    run: Runner,
    *,
    mechanism: FreezeMechanism,
    runtime: ContainerRuntime = ContainerRuntime.RUNSC,
) -> DockerSandbox:
    real_or_faulty = client if mechanism is FreezeMechanism.PAUSE else _NoPauseClient(client)
    box = run(
        DockerDriver(real_or_faulty).create(  # type: ignore[arg-type]
            make_spec(run_id, image, workspace, prefer=runtime)
        )
    )
    assert isinstance(box, DockerSandbox)
    assert box.freeze_mechanism is mechanism
    return box


MECHANISMS = pytest.mark.parametrize(
    "mechanism", [FreezeMechanism.PAUSE, FreezeMechanism.KILL_ALL], ids=["pause", "kill-all"]
)


# ---------------------------------------------------------------- the mechanism is recorded


@pytest.mark.docker
@RUNTIMES
def test_pause_works_under_both_runtimes_on_this_host_and_is_recorded(
    driver: DockerDriver,
    run_id: RunId,
    image: str,
    workspace: Path,
    run: Runner,
    runtime: ContainerRuntime,
) -> None:
    box = run(driver.create(make_spec(run_id, image, workspace, prefer=runtime)))
    try:
        assert box.freeze_mechanism is FreezeMechanism.PAUSE
        assert not [w for w in box.warnings if "pause" in w]
    finally:
        run(box.destroy())


@pytest.mark.docker
def test_a_runtime_without_pause_falls_back_to_kill_all_with_a_warning(
    client: docker.DockerClient, run_id: RunId, image: str, workspace: Path, run: Runner
) -> None:
    box = _box(client, run_id, image, workspace, run, mechanism=FreezeMechanism.KILL_ALL)
    try:
        assert any("pause" in w and "kills every process" in w for w in box.warnings)
        state = inspect(client, box)["State"]
        assert state["Running"] is True
        assert state["Paused"] is False  # the failed probe left it running
    finally:
        run(box.destroy())


# ---------------------------------------------------------------- frozen() stops the sandbox

SPINNER = (
    "i=0; while :; do i=$((i+1)); echo $i > /workspace/count.tmp; "
    "mv -f /workspace/count.tmp /workspace/count; done"
)


def _start_spinner(box: sb.Sandbox, workspace: Path, run: Runner) -> None:
    """A process that keeps running after its exec has returned, bumping a counter in the
    workspace as fast as it can."""
    started = run(
        box.exec(["sh", "-c", f"sh -c '{SPINNER}' >/dev/null 2>&1 &"], network=NONE, timeout_s=30)
    )
    assert started.exit_code == 0
    _wait_advancing(workspace)


def _count(workspace: Path) -> int:
    for _ in range(50):
        try:
            text = (workspace / "count").read_text().strip()
        except FileNotFoundError:
            text = ""
        if text:
            return int(text)
        time.sleep(0.02)
    raise AssertionError("the counter file never appeared")


def _wait_advancing(workspace: Path) -> None:
    first = _count(workspace)
    deadline = time.monotonic() + 10
    while _count(workspace) == first:
        assert time.monotonic() < deadline, "the spinner never advanced"
        time.sleep(0.05)


@pytest.mark.docker
@MECHANISMS
def test_while_frozen_no_sandbox_process_runs(
    client: docker.DockerClient,
    run_id: RunId,
    image: str,
    workspace: Path,
    run: Runner,
    mechanism: FreezeMechanism,
) -> None:
    box = _box(client, run_id, image, workspace, run, mechanism=mechanism)
    try:
        _start_spinner(box, workspace, run)

        async def frozen_counts() -> tuple[int, int, bool]:
            async with box.frozen():
                assert box.is_frozen
                time.sleep(0.3)  # let any in-flight write land
                first = _count(workspace)
                time.sleep(1.0)
                return first, _count(workspace), bool(inspect(client, box)["State"]["Paused"])

        first, second, paused = run(frozen_counts())
        assert first == second, "a sandbox process ran while frozen"
        assert paused is (mechanism is FreezeMechanism.PAUSE)
        assert box.is_frozen is False
        state = inspect(client, box)["State"]
        assert (state["Running"], state["Paused"]) == (True, False)
        if mechanism is FreezeMechanism.PAUSE:
            _wait_advancing(workspace)  # paused processes resume
        else:
            assert alive(box, run) == []  # killed and gone; init and the main process remain
            assert _count(workspace) == second
        assert run(box.exec(["echo", "ok"], network=NONE, timeout_s=30)).stdout_tail == "ok\n"
    finally:
        run(box.destroy())


@pytest.mark.docker
def test_kill_all_keeps_the_container_alive_after_a_restart(
    client: docker.DockerClient, run_id: RunId, image: str, workspace: Path, run: Runner
) -> None:
    """A restart starts a new main process; kill-all must spare the new one, not the old pid."""
    box = _box(client, run_id, image, workspace, run, mechanism=FreezeMechanism.KILL_ALL)
    try:
        before = inspect(client, box)["State"]["StartedAt"]
        box._main_pid = "999999"  # stale: only re-recording after the restart can fix it
        run(box._restart())
        assert inspect(client, box)["State"]["StartedAt"] != before

        async def freeze_once() -> None:
            async with box.frozen():
                pass

        run(freeze_once())
        assert inspect(client, box)["State"]["Running"] is True
        assert run(box.exec(["echo", "ok"], network=NONE, timeout_s=30)).stdout_tail == "ok\n"
    finally:
        run(box.destroy())


@pytest.mark.docker
def test_kill_all_refuses_host_access_if_it_cannot_prove_the_sandbox_stopped(
    client: docker.DockerClient,
    run_id: RunId,
    image: str,
    workspace: Path,
    run: Runner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    box = _box(client, run_id, image, workspace, run, mechanism=FreezeMechanism.KILL_ALL)
    try:
        monkeypatch.setattr(box, "_kill_all", lambda: False)

        async def scenario() -> None:
            async with box.frozen():
                raise AssertionError("entered frozen() without stopping the sandbox")

        with pytest.raises(RuntimeError, match="could not stop"):
            run(scenario())
        assert box.is_frozen is False
    finally:
        run(box.destroy())


# ---------------------------------------------------------------- frozen() and exec never overlap


@pytest.mark.docker
def test_exec_refuses_while_frozen(sandbox: sb.Sandbox, run: Runner) -> None:
    async def scenario() -> None:
        async with sandbox.frozen():
            with pytest.raises(RuntimeError, match="frozen"):
                await sandbox.exec(["true"], network=NONE, timeout_s=10)

    run(scenario())
    assert run(sandbox.exec(["echo", "ok"], network=NONE, timeout_s=30)).stdout_tail == "ok\n"


@pytest.mark.docker
def test_frozen_refuses_while_a_command_runs(sandbox: sb.Sandbox, run: Runner) -> None:
    async def scenario() -> None:
        task = asyncio.create_task(sandbox.exec(["sleep", "3"], network=NONE, timeout_s=30))
        await asyncio.sleep(1)
        with pytest.raises(RuntimeError, match="running"):
            async with sandbox.frozen():
                pass
        res = await task
        assert res.exit_code == 0  # the command was not disturbed
        async with sandbox.frozen():  # and afterwards freezing works
            assert sandbox.is_frozen

    run(scenario())


@pytest.mark.docker
def test_frozen_is_not_reentrant_and_unfreezes_on_error(
    client: docker.DockerClient, sandbox: sb.Sandbox, run: Runner
) -> None:
    async def nested() -> None:
        async with sandbox.frozen():  # noqa: SIM117  (nesting is the point)
            async with sandbox.frozen():
                pass

    with pytest.raises(RuntimeError, match="already frozen"):
        run(nested())

    async def failing() -> None:
        async with sandbox.frozen():
            raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        run(failing())
    assert sandbox.is_frozen is False
    assert inspect(client, sandbox)["State"]["Paused"] is False


@pytest.mark.docker
def test_frozen_refuses_while_an_install_container_exists(
    client: docker.DockerClient, sandbox: sb.Sandbox, run_id: RunId, image: str, run: Runner
) -> None:
    """§9.5: install containers are gone before host access. P2-09 builds them; here one is
    faked with the labels they will carry."""
    install = client.containers.run(
        image,
        ["sleep", "300"],
        detach=True,
        labels={LABEL_RUN: run_id, LABEL_ROLE: ROLE_INSTALL},
        network_mode="none",
    )

    async def scenario() -> None:
        async with sandbox.frozen():
            pass

    try:
        with pytest.raises(RuntimeError, match="install container"):
            run(scenario())
    finally:
        install.remove(force=True)
    run(scenario())


@pytest.mark.docker
def test_frozen_refuses_after_destroy(
    driver: DockerDriver, run_id: RunId, image: str, workspace: Path, run: Runner
) -> None:
    box = run(driver.create(make_spec(run_id, image, workspace)))
    run(box.destroy())

    async def scenario() -> None:
        async with box.frozen():
            pass

    with pytest.raises(RuntimeError, match="destroyed"):
        run(scenario())


# ---------------------------------------------------------------- the host workspace, frozen


@pytest.mark.docker
@MECHANISMS
def test_host_and_sandbox_see_each_others_writes_immediately(
    client: docker.DockerClient,
    run_id: RunId,
    image: str,
    workspace: Path,
    run: Runner,
    mechanism: FreezeMechanism,
) -> None:
    box = _box(client, run_id, image, workspace, run, mechanism=mechanism)
    ws = HostWorkspace(workspace, sandbox=box)
    try:
        with pytest.raises(RuntimeError, match="frozen"):
            ws.write_text("a.txt", "x\n", create=True)

        async def host_writes(edits: list[tuple[str, str, bool]]) -> None:
            async with box.frozen():
                for rel, content, create in edits:
                    ws.write_text(rel, content, create=create)

        def cat(rel: str) -> str:
            res = run(box.exec(["cat", f"/workspace/{rel}"], network=NONE, timeout_s=30))
            assert res.exit_code == 0, res.stderr_tail
            return res.stdout_tail

        run(host_writes([("a.txt", "one\n", True), ("deep/b.txt", "b\n", True)]))
        assert cat("a.txt") == "one\n"
        assert cat("deep/b.txt") == "b\n"
        # Rewrites (a new inode each time) of a file the sandbox has already read.
        run(host_writes([("a.txt", "two, and longer\n", False)]))
        assert cat("a.txt") == "two, and longer\n"
        run(host_writes([("a.txt", "3\n", False)]))
        assert cat("a.txt") == "3\n"

        # The other way: sandbox writes, the host reads at once, under freeze.
        script = "echo from-sandbox > /workspace/c.txt; printf more >> /workspace/a.txt"
        assert run(box.exec(["sh", "-c", script], network=NONE, timeout_s=30)).exit_code == 0

        async def host_reads() -> tuple[str, str, list[str]]:
            async with box.frozen():
                snap = ws.walk_tree(ArtifactRules())
                return (
                    ws.read_text("c.txt", max_bytes=100),
                    ws.read_text("a.txt", max_bytes=100),
                    sorted(snap.entries),
                )

        c_txt, a_txt, paths = run(host_reads())
        assert (c_txt, a_txt) == ("from-sandbox\n", "3\nmore")
        assert paths == ["a.txt", "c.txt", "deep/b.txt"]
    finally:
        run(box.destroy())


@pytest.mark.docker
def test_a_symlink_planted_by_the_sandbox_is_refused_by_the_host(
    sandbox: sb.Sandbox, workspace: Path, tmp_path: Path, run: Runner
) -> None:
    """End to end: the sandbox plants links to host paths; the frozen host refuses them."""
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("SECRET\n")
    plant = f"ln -s {outside} /workspace/leak && ln -s {tmp_path} /workspace/dir"
    assert run(sandbox.exec(["sh", "-c", plant], network=NONE, timeout_s=30)).exit_code == 0
    ws = HostWorkspace(workspace, sandbox=sandbox)

    async def scenario() -> None:
        async with sandbox.frozen():
            with pytest.raises(PathEscapeError):
                ws.read_text("leak", max_bytes=100)
            with pytest.raises(PathEscapeError):
                ws.read_text("dir/outside-secret.txt", max_bytes=100)
            with pytest.raises(PathEscapeError):
                ws.write_text("dir/planted.txt", "x", create=True)

    run(scenario())
    assert outside.read_text() == "SECRET\n"
    assert not (tmp_path / "planted.txt").exists()


# ---------------------------------------------------------------- hardening, from inside


def _status(box: sb.Sandbox, run: Runner) -> dict[str, str]:
    res = run(box.exec(["cat", "/proc/self/status"], network=NONE, timeout_s=30))
    rows = (ln.partition(":") for ln in res.stdout_tail.splitlines())
    return {k.strip(): v.strip() for k, _, v in rows}


@pytest.mark.docker
@RUNTIMES
def test_the_hardening_holds_inside_the_container(
    driver: DockerDriver,
    run_id: RunId,
    image: str,
    workspace: Path,
    run: Runner,
    runtime: ContainerRuntime,
) -> None:
    box = run(driver.create(make_spec(run_id, image, workspace, prefer=runtime)))
    try:
        status = _status(box, run)
        uid, gid = str(os.getuid()), str(os.getgid())
        assert uid != "0"
        assert status["Uid"].split() == [uid] * 4  # real, effective, saved, filesystem
        assert status["Gid"].split() == [gid] * 4
        for cap in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"):
            assert int(status[cap], 16) == 0, cap
        assert status["NoNewPrivs"] == "1"

        def sh(script: str) -> sb.ExecResult:
            return run(box.exec(["sh", "-c", script], network=NONE, timeout_s=30))

        # Read-only root: every top-level directory but the writable mounts refuses writes.
        probe = (
            "for d in / /bin /etc /usr /var /root /home /opt /srv; do "
            '[ -d "$d" ] || continue; touch "$d/.mpilot-w" 2>/dev/null && echo "WRITABLE $d"; '
            "done; echo done"
        )
        assert sh(probe).stdout_tail == "done\n"
        mounts = sh("cat /proc/mounts").stdout_tail.splitlines()
        root = next(ln.split() for ln in mounts if ln.split()[1] == "/")
        assert "ro" in root[3].split(",")
        tmp = next(ln.split() for ln in mounts if ln.split()[1] == CONTAINER_HOME)
        assert tmp[2] == "tmpfs"
        assert "nosuid" in tmp[3].split(",")
        if runtime is ContainerRuntime.RUNC:  # gVisor's /proc/mounts omits nodev (no mknod there)
            assert "nodev" in tmp[3].split(",")
        # uid mapping: workspace files are the host user's on both sides.
        (workspace / "host.txt").write_text("h\n")
        owners = sh("stat -c %u:%g /workspace /workspace/host.txt").stdout_tail.split()
        assert owners == [f"{uid}:{gid}"] * 2
        assert sh("touch /workspace/c.txt").exit_code == 0
        made = (workspace / "c.txt").stat()
        assert (made.st_uid, made.st_gid) == (os.getuid(), os.getgid())
        # No way to root: setuid binaries can't gain privileges (no-new-privileges, nosuid).
        su = sh("su -c id root </dev/null; echo rc=$?").stdout_tail
        assert "uid=0" not in su
    finally:
        run(box.destroy())


# ---------------------------------------------------------------- review fixes (P2-03 pass 1)
#
# `is_frozen` must match the container's real state through every failure and cancellation,
# and KILL_ALL must not be fooled by a zombie thread-group leader whose threads still run.


def _frozen_body(box: DockerSandbox, entered: asyncio.Event | None = None) -> Any:
    async def body() -> None:
        async with box.frozen():
            if entered is not None:
                entered.set()

    return body()


@pytest.mark.docker
def test_a_cancelled_thaw_never_leaves_the_sandbox_marked_frozen(
    client: docker.DockerClient,
    sandbox: sb.Sandbox,
    workspace: Path,
    run: Runner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert isinstance(sandbox, DockerSandbox)
    real_unpause = sandbox._unpause

    def slow_unpause() -> None:
        time.sleep(1.5)
        real_unpause()

    monkeypatch.setattr(sandbox, "_unpause", slow_unpause)
    ws = HostWorkspace(workspace, sandbox=sandbox)

    async def scenario() -> str:
        entered = asyncio.Event()
        task = asyncio.create_task(_frozen_body(sandbox, entered))
        await entered.wait()
        await asyncio.sleep(0.3)  # the body is done; the slow thaw is under way
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert sandbox.is_frozen is False  # at once, while the container is still paused
        with pytest.raises(RuntimeError, match="frozen"):
            ws.write_text("x.txt", "x", create=True)
        res = await sandbox.exec(["echo", "ok"], network=NONE, timeout_s=30)  # waits for thaw
        return res.stdout_tail

    assert run(scenario()) == "ok\n"
    state = inspect(client, sandbox)["State"]
    assert (state["Running"], state["Paused"]) == (True, False)


@pytest.mark.docker
def test_a_failed_unpause_leaves_the_sandbox_broken_not_frozen(
    client: docker.DockerClient,
    sandbox: sb.Sandbox,
    workspace: Path,
    run: Runner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert isinstance(sandbox, DockerSandbox)

    def failing_unpause() -> None:
        raise RuntimeError("unpause failed (fault injected)")

    monkeypatch.setattr(sandbox, "_unpause", failing_unpause)
    with pytest.raises(RuntimeError, match="fault injected"):
        run(_frozen_body(sandbox))
    assert sandbox.is_frozen is False
    with pytest.raises(RuntimeError, match="broken"):
        run(sandbox.exec(["true"], network=NONE, timeout_s=10))
    with pytest.raises(RuntimeError, match="broken"):
        run(_frozen_body(sandbox))
    with pytest.raises(RuntimeError, match="frozen"):
        HostWorkspace(workspace, sandbox=sandbox).read_text("a", max_bytes=1)


@pytest.mark.docker
def test_a_freeze_cancelled_while_pausing_is_undone(
    client: docker.DockerClient,
    sandbox: sb.Sandbox,
    run: Runner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert isinstance(sandbox, DockerSandbox)
    real_pause = sandbox._pause

    def pause_then_stall() -> None:
        real_pause()
        time.sleep(1.5)

    monkeypatch.setattr(sandbox, "_pause", pause_then_stall)

    async def scenario() -> str:
        task = asyncio.create_task(_frozen_body(sandbox))
        await asyncio.sleep(0.5)  # paused, entry not finished
        assert sandbox.is_frozen is False
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        res = await sandbox.exec(["echo", "ok"], network=NONE, timeout_s=30)
        return res.stdout_tail

    assert run(scenario()) == "ok\n"
    assert inspect(client, sandbox)["State"]["Paused"] is False


@pytest.mark.docker
def test_no_exec_can_start_while_a_freeze_is_being_entered(
    sandbox: sb.Sandbox, run: Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exec asked for during freeze entry waits for the entry step, then finds the sandbox
    frozen and raises: it never runs while freezing or frozen. Afterwards exec works."""
    assert isinstance(sandbox, DockerSandbox)
    real_pause, real_once = sandbox._pause, sandbox._exec_once
    seen: list[str] = []

    def slow_pause() -> None:
        time.sleep(1.0)
        real_pause()

    async def spy_once(*args: Any) -> sb.ExecResult:
        seen.append(sandbox._state.value)
        return await real_once(*args)

    monkeypatch.setattr(sandbox, "_pause", slow_pause)
    monkeypatch.setattr(sandbox, "_exec_once", spy_once)

    async def scenario() -> str:
        async def freeze_and_check() -> None:
            async with sandbox.frozen():
                with pytest.raises(RuntimeError, match="frozen"):
                    await sandbox.exec(["true"], network=NONE, timeout_s=10)
                await asyncio.sleep(0.5)

        task = asyncio.create_task(freeze_and_check())
        await asyncio.sleep(0.3)  # freezing, not yet paused
        with pytest.raises(RuntimeError, match="frozen"):
            await sandbox.exec(["echo", "early"], network=NONE, timeout_s=30)
        await task
        assert seen == []  # nothing ran while freezing or frozen
        res = await sandbox.exec(["echo", "ok"], network=NONE, timeout_s=30)
        return res.stdout_tail

    assert run(scenario()) == "ok\n"
    assert seen == ["idle"]


@pytest.mark.docker
def test_a_twice_cancelled_exec_finishes_its_kill_before_any_freeze(
    sandbox: sb.Sandbox, run: Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert isinstance(sandbox, DockerSandbox)
    real_killer = sandbox._run_killer
    marks: dict[str, float] = {}

    def slow_killer(tag: str) -> bool:
        time.sleep(1.5)
        ok = real_killer(tag)
        marks["killed"] = time.monotonic()
        return ok

    monkeypatch.setattr(sandbox, "_run_killer", slow_killer)

    async def scenario() -> None:
        task = asyncio.create_task(
            sandbox.exec(["sh", "-c", "sleep 310 & sleep 311 & wait"], network=NONE, timeout_s=600)
        )
        await asyncio.sleep(1.5)
        task.cancel()
        await asyncio.sleep(0.2)  # now inside the shielded kill
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with sandbox.frozen():  # must wait for the kill, not overlap it
            marks["frozen"] = time.monotonic()

    run(scenario())
    assert marks["frozen"] >= marks["killed"]
    assert alive(sandbox, run) == []


@pytest.fixture(scope="session")
def zombie_leader(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A static binary whose main thread exits alone (`SYS_exit`) while another thread keeps
    bumping `/workspace/count`: its pid shows as a zombie, yet it runs. Busybox can't make one."""
    gcc = shutil.which("gcc")
    assert gcc is not None, "gcc (build-essential) is needed to build the zombie-leader helper"
    src_dir = tmp_path_factory.mktemp("zleader")
    source = src_dir / "zleader.c"
    source.write_text(
        textwrap.dedent(
            """
            #include <pthread.h>
            #include <stdio.h>
            #include <sys/syscall.h>
            #include <unistd.h>
            static void *spin(void *arg) {
              (void)arg;
              for (unsigned long i = 1;; i++) {
                FILE *f = fopen("/workspace/count.tmp", "w");
                if (f) {
                  fprintf(f, "%lu\\n", i);
                  fclose(f);
                  rename("/workspace/count.tmp", "/workspace/count");
                }
                usleep(1000);
              }
              return NULL;
            }
            int main(void) {
              pthread_t t;
              if (pthread_create(&t, NULL, spin, NULL) != 0) return 1;
              usleep(100000);
              syscall(SYS_exit, 0);
              return 0;
            }
            """
        )
    )
    binary = src_dir / "zleader"
    subprocess.run(  # noqa: S603  (fixed argv, test only)
        [gcc, "-static", "-pthread", "-O2", "-o", str(binary), str(source)],
        check=True,
        capture_output=True,
        timeout=120,
    )
    return binary


@pytest.mark.docker
@MECHANISMS
def test_a_zombie_leader_with_live_threads_is_stopped_too(
    client: docker.DockerClient,
    run_id: RunId,
    image: str,
    workspace: Path,
    run: Runner,
    zombie_leader: Path,
    mechanism: FreezeMechanism,
) -> None:
    shutil.copy2(zombie_leader, workspace / "zleader")
    box = _box(client, run_id, image, workspace, run, mechanism=mechanism)
    try:
        started = run(
            box.exec(
                ["sh", "-c", "/workspace/zleader >/dev/null 2>&1 &"], network=NONE, timeout_s=30
            )
        )
        assert started.exit_code == 0
        _wait_advancing(workspace)

        async def frozen_counts() -> tuple[int, int]:
            async with box.frozen():
                time.sleep(0.3)
                first = _count(workspace)
                time.sleep(1.0)
                return first, _count(workspace)

        first, second = run(frozen_counts())
        assert first == second, "a thread of a zombie leader ran while frozen"
        if mechanism is FreezeMechanism.KILL_ALL:
            time.sleep(0.5)
            assert _count(workspace) == second  # the whole thread group is gone
            assert alive(box, run) == []
    finally:
        run(box.destroy())


# ---------------------------------------------------------------- review suggestions (pass 2)


@pytest.mark.docker
def test_a_pause_landing_after_the_freeze_was_cancelled_is_still_undone(
    client: docker.DockerClient,
    sandbox: sb.Sandbox,
    run: Runner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cancellation arrives before `docker pause` does: recovery must wait for it."""
    assert isinstance(sandbox, DockerSandbox)
    real_pause = sandbox._pause

    def stall_then_pause() -> None:
        time.sleep(1.0)
        real_pause()

    monkeypatch.setattr(sandbox, "_pause", stall_then_pause)

    async def scenario() -> str:
        task = asyncio.create_task(_frozen_body(sandbox))
        await asyncio.sleep(0.3)  # cancelled before the pause has landed
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        res = await sandbox.exec(["echo", "ok"], network=NONE, timeout_s=30)
        return res.stdout_tail

    assert run(scenario()) == "ok\n"
    state = inspect(client, sandbox)["State"]
    assert (state["Running"], state["Paused"]) == (True, False)


@pytest.mark.docker
def test_destroy_while_frozen_clears_the_flag_first(
    client: docker.DockerClient,
    driver: DockerDriver,
    run_id: RunId,
    image: str,
    workspace: Path,
    run: Runner,
) -> None:
    box = run(driver.create(make_spec(run_id, image, workspace)))
    assert isinstance(box, DockerSandbox)

    async def scenario() -> None:
        async with box.frozen():
            await box.destroy()
            assert box.is_frozen is False

    run(scenario())  # the thaw sees the sandbox destroyed and does nothing
    assert box.is_frozen is False
    assert client.containers.list(all=True, filters={"label": f"{LABEL_RUN}={run_id}"}) == []


@pytest.mark.docker
def test_a_freeze_waits_for_a_restart_whose_exec_was_cancelled(
    sandbox: sb.Sandbox, run: Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert isinstance(sandbox, DockerSandbox)
    monkeypatch.setattr(sandbox, "_run_killer", lambda tag: False)  # forces the restart path
    real_find = sandbox._find_main_pid
    marks: dict[str, float] = {}

    def slow_find() -> str:  # runs inside the restart, after the container started again
        time.sleep(1.5)
        pid = real_find()
        marks["restarted"] = time.monotonic()
        return pid

    monkeypatch.setattr(sandbox, "_find_main_pid", slow_find)

    async def scenario() -> None:
        task = asyncio.create_task(sandbox.exec(["sleep", "312"], network=NONE, timeout_s=1))
        await asyncio.sleep(1.8)  # timed out; the restart is under way
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with sandbox.frozen():
            marks["frozen"] = time.monotonic()

    run(scenario())
    assert marks["frozen"] >= marks["restarted"]


@pytest.mark.docker
def test_a_freeze_waits_for_a_restart_after_the_sandbox_died_under_a_cancelled_exec(
    client: docker.DockerClient,
    sandbox: sb.Sandbox,
    run: Runner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The container dies under a command (as gVisor does on a limit), `exec` restarts it, and
    the exec is cancelled during that restart: a freeze must not overlap the restart."""
    assert isinstance(sandbox, DockerSandbox)
    real_find = sandbox._find_main_pid
    marks: dict[str, float] = {}

    def slow_find() -> str:
        time.sleep(1.5)
        pid = real_find()
        marks["restarted"] = time.monotonic()
        return pid

    monkeypatch.setattr(sandbox, "_find_main_pid", slow_find)

    async def scenario() -> None:
        task = asyncio.create_task(sandbox.exec(["sleep", "313"], network=NONE, timeout_s=600))
        await asyncio.sleep(1.0)
        await asyncio.to_thread(client.api.kill, sandbox.container_id)  # the sandbox dies
        await asyncio.sleep(1.0)  # exec noticed and is restarting (slow main-pid lookup)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with sandbox.frozen():
            marks["frozen"] = time.monotonic()

    run(scenario())
    assert marks["frozen"] >= marks["restarted"]
