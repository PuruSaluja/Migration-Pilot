"""P2-02: Docker driver lifecycle (SPEC §9.2, §9.3, NFR-10; D6 is P2-05, freeze is P2-03).

Real Docker, real gVisor: `busybox:1.36` (pulled by `scripts/setup-dev.sh`) stands in for the
per-ecosystem images, which P2-06/P2-07 build. Docker tests fail, never skip, without Docker.
Containers are labelled with a per-test run id and removed by the fixtures.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

import docker
import pytest
from docker.errors import APIError, NotFound
from docker_helpers import (  # noqa: F401  (fixtures are used by name)
    NONE,
    FaultyClient,
    FaultyContainers,
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
    started_at,
    workspace,
)

from mpilot.core.errors import MpilotError
from mpilot.core.types import NetworkMode, RunId, new_run_id
from mpilot.sandbox import base as sb
from mpilot.sandbox.base import ContainerRuntime
from mpilot.sandbox.driver import (
    ALLOWED_EXEC_ENV,
    CONTAINER_HOME,
    LABEL_RUN,
    DockerDriver,
    DockerSandbox,
    env_problems,
)

# ---------------------------------------------------------------- pure helpers


def test_env_problems_enforce_the_allowlist_and_the_credential_floor() -> None:
    assert env_problems({"CI": "1", "LANG": "C.UTF-8"}) == []
    assert any("NPM_CONFIG_REGISTRY" in p for p in env_problems({"NPM_CONFIG_REGISTRY": "x"}))
    assert any("NFR-6" in p for p in env_problems({"GITHUB_TOKEN": "x"}))
    assert any("allowlist" in p for p in env_problems({"PATH": "/evil"}))
    assert any("allowlist" in p for p in env_problems({"HOME": "/"}))
    assert env_problems({"CI": "https://user:tok@host/x"})  # URL credentials in a value
    assert "CI" in ALLOWED_EXEC_ENV
    assert not any(k.startswith(("GITHUB_", "ANTHROPIC_", "PIP_")) for k in ALLOWED_EXEC_ENV)


def test_the_driver_refuses_to_run_containers_as_root(client: docker.DockerClient) -> None:
    with pytest.raises(ValueError, match="root"):
        DockerDriver(client, uid=0)


# ---------------------------------------------------------------- runtime detection


@pytest.mark.docker
def test_detect_runtime_finds_runsc_on_this_host(driver: DockerDriver) -> None:
    info = driver.detect_runtime()
    assert info.runtime is ContainerRuntime.RUNSC
    assert info.warnings == ()
    assert info.version  # recorded for reproducibility (§9.3)


@pytest.mark.docker
def test_a_missing_runsc_falls_back_to_runc_with_a_warning(client: docker.DockerClient) -> None:
    info = DockerDriver(client, runsc_name="runsc-fault-injected").detect_runtime()
    assert info.runtime is ContainerRuntime.RUNC
    assert info.warnings
    assert "runsc" in info.warnings[0]


@pytest.mark.docker
def test_no_usable_runtime_at_all_is_an_error(client: docker.DockerClient) -> None:
    broken = DockerDriver(client, runsc_name="nope-a", runc_name="nope-b")
    with pytest.raises(RuntimeError, match="neither"):
        broken.detect_runtime()


# ---------------------------------------------------------------- create and hardening


@pytest.mark.docker
def test_create_applies_the_hardening_and_limits(
    client: docker.DockerClient,
    driver: DockerDriver,
    run_id: RunId,
    image: str,
    workspace: Path,
    run: Runner,
) -> None:
    box = run(driver.create(make_spec(run_id, image, workspace, pids=64, memory_gb=1)))
    try:
        assert isinstance(box, sb.Sandbox)
        assert box.runtime is ContainerRuntime.RUNSC
        assert list(box.warnings) == []
        assert box.is_frozen is False
        data = inspect(client, box)
        host = data["HostConfig"]
        assert data["State"]["Running"] is True
        assert host["Runtime"] == "runsc"
        assert host["ReadonlyRootfs"] is True
        assert host["CapDrop"] == ["ALL"]
        assert host["CapAdd"] in (None, [])
        assert "no-new-privileges" in " ".join(host["SecurityOpt"])
        assert host["NetworkMode"] == "none"
        assert host["Privileged"] is False
        assert host["PidsLimit"] == 64
        assert host["Memory"] == 1024**3
        assert host["MemorySwap"] == 1024**3  # no swap
        assert host["NanoCpus"] == 1_000_000_000
        assert host["Init"] is True
        assert host["LogConfig"]["Type"] == "none"
        assert data["Config"]["User"] == f"{os.getuid()}:{os.getgid()}"
        assert data["Config"]["WorkingDir"] == "/workspace"
        assert data["Config"]["Labels"][LABEL_RUN] == run_id
        assert data["Image"].startswith("sha256:")
        # The only mount is the workspace; /tmp is a tmpfs.
        assert [(m["Source"], m["Destination"], m["RW"]) for m in data["Mounts"]] == [
            (str(workspace), "/workspace", True)
        ]
        assert CONTAINER_HOME in host["Tmpfs"]
    finally:
        run(box.destroy())


@pytest.mark.docker
def test_the_container_runs_as_the_host_user_without_privileges(
    sandbox: sb.Sandbox, workspace: Path, run: Runner
) -> None:
    uid = run(sandbox.exec(["id", "-u"], network=NONE, timeout_s=30))
    gid = run(sandbox.exec(["id", "-g"], network=NONE, timeout_s=30))
    assert uid.stdout_tail.strip() == str(os.getuid())
    assert gid.stdout_tail.strip() == str(os.getgid())
    status = run(sandbox.exec(["cat", "/proc/self/status"], network=NONE, timeout_s=30))
    cap_eff = next(ln for ln in status.stdout_tail.splitlines() if ln.startswith("CapEff"))
    assert cap_eff.split()[1].strip("0") == ""  # P2-03 asserts this for every container too


@pytest.mark.docker
def test_only_the_workspace_and_tmp_are_writable(
    sandbox: sb.Sandbox, workspace: Path, run: Runner
) -> None:
    ok = run(sandbox.exec(["sh", "-c", "echo hi > /workspace/f.txt"], network=NONE, timeout_s=30))
    assert ok.exit_code == 0
    assert (workspace / "f.txt").read_text() == "hi\n"
    assert (workspace / "f.txt").stat().st_uid == os.getuid()  # files belong to the host user
    (workspace / "from-host.txt").write_text("host\n")
    seen = run(sandbox.exec(["cat", "/workspace/from-host.txt"], network=NONE, timeout_s=30))
    assert seen.stdout_tail == "host\n"
    tmp = run(
        sandbox.exec(["sh", "-c", "echo x > /tmp/t && cat /tmp/t"], network=NONE, timeout_s=30)
    )
    assert (tmp.exit_code, tmp.stdout_tail) == (0, "x\n")
    for target in ("/etc/mpilot-test", "/usr/mpilot-test", "/mpilot-test"):
        denied = run(sandbox.exec(["touch", target], network=NONE, timeout_s=30))
        assert denied.exit_code != 0, target


@pytest.mark.docker
def test_create_records_nothing_but_the_allowlisted_env(
    driver: DockerDriver, run_id: RunId, image: str, workspace: Path, run: Runner
) -> None:
    box = run(driver.create(make_spec(run_id, image, workspace, env={"CI": "1", "TZ": "UTC"})))
    try:
        out = run(box.exec(["env"], network=NONE, timeout_s=30)).stdout_tail
        env = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        assert (env["CI"], env["TZ"], env["HOME"]) == ("1", "UTC", CONTAINER_HOME)
        assert not [k for k in env if k.startswith(("GITHUB", "ANTHROPIC", "CLAUDE", "MPILOT"))]
    finally:
        run(box.destroy())


@pytest.mark.docker
@pytest.mark.parametrize("key", ["PATH", "NPM_CONFIG_REGISTRY", "LD_PRELOAD"])
def test_create_refuses_env_outside_the_allowlist(
    driver: DockerDriver, run_id: RunId, image: str, workspace: Path, run: Runner, key: str
) -> None:
    with pytest.raises(ValueError, match="allowlist"):
        run(driver.create(make_spec(run_id, image, workspace, env={key: "x"})))


@pytest.mark.docker
def test_create_refuses_bad_workspaces_and_images(
    client: docker.DockerClient,
    driver: DockerDriver,
    run_id: RunId,
    image: str,
    workspace: Path,
    tmp_path: Path,
    run: Runner,
) -> None:
    link = tmp_path / "link"
    link.symlink_to(workspace)
    afile = tmp_path / "file"
    afile.write_text("")
    for bad, error in [
        (Path("relative/workspace"), ValueError),
        (tmp_path / "missing", FileNotFoundError),
        (link, ValueError),
        (afile, NotADirectoryError),
    ]:
        with pytest.raises(error):
            run(driver.create(make_spec(run_id, image, bad)))
    missing = "no-such-image-mpilot@sha256:" + "0" * 64
    with pytest.raises(RuntimeError, match="image"):
        run(driver.create(make_spec(run_id, missing, workspace)))
    assert client.containers.list(all=True, filters={"label": f"{LABEL_RUN}={run_id}"}) == []


# ---------------------------------------------------------------- the runc fallback (NFR-10)


@pytest.mark.docker
def test_an_unregistered_runsc_falls_back_to_runc_and_records_a_warning(
    client: docker.DockerClient, run_id: RunId, image: str, workspace: Path, run: Runner
) -> None:
    driver = DockerDriver(client, runsc_name="runsc-fault-injected")
    box = run(driver.create(make_spec(run_id, image, workspace)))
    try:
        assert box.runtime is ContainerRuntime.RUNC
        assert any("runsc" in w for w in box.warnings)
        assert inspect(client, box)["HostConfig"]["Runtime"] == "runc"
        assert run(box.exec(["echo", "ok"], network=NONE, timeout_s=30)).stdout_tail == "ok\n"
    finally:
        run(box.destroy())


@pytest.mark.docker
def test_a_runsc_that_fails_to_start_falls_back_to_runc_with_a_warning(
    client: docker.DockerClient, run_id: RunId, image: str, workspace: Path, run: Runner
) -> None:
    log: list[str] = []
    driver = DockerDriver(FaultyClient(client, log))  # type: ignore[arg-type]
    box = run(driver.create(make_spec(run_id, image, workspace)))
    try:
        assert log == ["runsc", "runc"]
        assert box.runtime is ContainerRuntime.RUNC
        assert any("fault injected" in w for w in box.warnings)
        assert inspect(client, box)["HostConfig"]["Runtime"] == "runc"
        listed = client.containers.list(all=True, filters={"label": f"{LABEL_RUN}={run_id}"})
        assert len(listed) == 1  # the failed attempt left nothing behind
    finally:
        run(box.destroy())


@pytest.mark.docker
def test_asking_for_runc_is_allowed_but_always_warns(
    client: docker.DockerClient,
    driver: DockerDriver,
    run_id: RunId,
    image: str,
    workspace: Path,
    run: Runner,
) -> None:
    box = run(driver.create(make_spec(run_id, image, workspace, prefer=ContainerRuntime.RUNC)))
    try:
        assert box.runtime is ContainerRuntime.RUNC
        assert box.warnings
        assert inspect(client, box)["HostConfig"]["Runtime"] == "runc"
    finally:
        run(box.destroy())


@pytest.mark.docker
def test_a_failure_unrelated_to_the_runtime_is_not_retried_on_runc(
    client: docker.DockerClient, run_id: RunId, image: str, workspace: Path, run: Runner
) -> None:
    class Broken(FaultyContainers):
        def create(self, *args: Any, **kwargs: Any) -> Any:
            self._log.append(str(kwargs.get("runtime")))
            raise APIError("No such image: broken")

    log: list[str] = []
    faulty = FaultyClient(client, log)
    faulty.containers = Broken(client.containers, log)  # type: ignore[assignment]
    with pytest.raises(APIError):
        run(DockerDriver(faulty).create(make_spec(run_id, image, workspace)))  # type: ignore[arg-type]
    assert log == ["runsc"]


# ---------------------------------------------------------------- exec


@pytest.mark.docker
def test_exec_returns_output_exit_code_and_duration(sandbox: sb.Sandbox, run: Runner) -> None:
    res = run(
        sandbox.exec(["sh", "-c", "echo out; echo err >&2; exit 3"], network=NONE, timeout_s=30)
    )
    assert (res.exit_code, res.stdout_tail, res.stderr_tail) == (3, "out\n", "err\n")
    assert res.truncated is False
    assert res.timed_out is False
    assert res.killed_disk_limit is False
    assert 0 <= res.duration_s < 30


@pytest.mark.docker
def test_exec_runs_argv_without_a_shell_in_the_given_cwd(
    sandbox: sb.Sandbox, workspace: Path, run: Runner
) -> None:
    (workspace / "sub dir").mkdir()
    res = run(sandbox.exec(["pwd"], network=NONE, timeout_s=30, cwd="/workspace/sub dir"))
    assert res.stdout_tail == "/workspace/sub dir\n"
    res = run(sandbox.exec(["echo", "$HOME", "a;b", "$(id)"], network=NONE, timeout_s=30))
    assert res.stdout_tail == "$HOME a;b $(id)\n"
    missing = run(sandbox.exec(["/no/such/binary"], network=NONE, timeout_s=30))
    assert missing.exit_code != 0  # a failing command is a result, never an exception
    bad_cwd = run(sandbox.exec(["true"], network=NONE, timeout_s=30, cwd="/no/such/dir"))
    assert bad_cwd.exit_code != 0


@pytest.mark.docker
def test_exec_keeps_only_the_tail_of_long_output(
    driver: DockerDriver, run_id: RunId, image: str, workspace: Path, run: Runner
) -> None:
    box = run(driver.create(make_spec(run_id, image, workspace)))
    try:
        assert isinstance(box, DockerSandbox)
        box.tail_bytes = 1000
        cmd = "i=0; while [ $i -lt 400 ]; do echo line-$i; i=$((i+1)); done; echo done >&2"
        res = run(box.exec(["sh", "-c", cmd], network=NONE, timeout_s=60))
        assert res.truncated is True
        assert res.stdout_tail.endswith("line-399\n")
        assert "line-0\n" not in res.stdout_tail
        assert len(res.stdout_tail.encode()) <= 1000
        assert res.stderr_tail == "done\n"
        short = run(box.exec(["echo", "x"], network=NONE, timeout_s=30))
        assert short.truncated is False
    finally:
        run(box.destroy())


@pytest.mark.docker
def test_exec_decodes_non_utf8_output_without_failing(sandbox: sb.Sandbox, run: Runner) -> None:
    res = run(sandbox.exec(["sh", "-c", "printf 'caf\\351\\n'"], network=NONE, timeout_s=30))
    assert res.exit_code == 0
    assert res.stdout_tail.startswith("caf")


@pytest.mark.docker
def test_exec_gives_commands_no_stdin(sandbox: sb.Sandbox, run: Runner) -> None:
    res = run(sandbox.exec(["sh", "-c", "cat; echo eof"], network=NONE, timeout_s=20))
    assert (res.exit_code, res.stdout_tail, res.timed_out) == (0, "eof\n", False)


@pytest.mark.docker
def test_exec_passes_allowlisted_env_and_refuses_everything_else(
    sandbox: sb.Sandbox, run: Runner
) -> None:
    res = run(sandbox.exec(["sh", "-c", "echo $CI"], network=NONE, timeout_s=30, env={"CI": "1"}))
    assert res.stdout_tail == "1\n"
    for env in ({"GITHUB_TOKEN": "x"}, {"MY_SECRET": "x"}, {"PATH": "/x"}, {"BAD KEY": "x"}):
        with pytest.raises(ValueError, match=r"allowlist|NFR-6|valid"):
            run(sandbox.exec(["true"], network=NONE, timeout_s=30, env=env))


@pytest.mark.docker
def test_exec_refuses_bad_arguments(sandbox: sb.Sandbox, run: Runner) -> None:
    for kwargs in (
        {"argv": []},
        {"argv": ["true", 3]},
        {"argv": ["true"], "timeout_s": 0},
        {"argv": ["true"], "cwd": "relative"},
    ):
        args: dict[str, Any] = {"network": NONE, "timeout_s": 30, **kwargs}
        with pytest.raises(ValueError, match=r"argv|timeout_s|cwd"):
            run(sandbox.exec(**args))


@pytest.mark.docker
def test_registry_mode_arrives_with_the_install_container(sandbox: sb.Sandbox, run: Runner) -> None:
    with pytest.raises(NotImplementedError, match="P2-09"):
        run(sandbox.exec(["true"], network=NetworkMode.REGISTRY, timeout_s=30))


# ---------------------------------------------------------------- timeouts


@pytest.mark.docker
def test_the_stray_process_check_sees_what_a_command_leaves_behind(
    sandbox: sb.Sandbox, run: Runner
) -> None:
    """Positive control for every `alive(...) == []` below."""
    assert alive(sandbox, run) == []
    res = run(
        sandbox.exec(["sh", "-c", "sleep 300 & sleep 301 & echo left"], network=NONE, timeout_s=30)
    )
    assert res.stdout_tail == "left\n"
    assert len(alive(sandbox, run)) >= 2


@pytest.mark.docker
def test_a_timeout_kills_the_command_and_everything_it_started(
    client: docker.DockerClient, sandbox: sb.Sandbox, run: Runner
) -> None:
    started = started_at(client, sandbox)
    began = time.monotonic()
    res = run(sandbox.exec(["sh", "-c", "sleep 300 & sleep 301 & wait"], network=NONE, timeout_s=2))
    assert res.timed_out is True
    assert res.exit_code == -1
    assert res.killed_disk_limit is False
    assert time.monotonic() - began < 30
    assert alive(sandbox, run) == []
    assert started_at(client, sandbox) == started  # killed by tag, not by a restart
    # The container is still usable afterwards.
    assert run(sandbox.exec(["echo", "ok"], network=NONE, timeout_s=30)).stdout_tail == "ok\n"


@pytest.mark.docker
def test_a_timeout_also_kills_processes_that_left_their_session(
    client: docker.DockerClient, sandbox: sb.Sandbox, run: Runner
) -> None:
    """`setsid` escapes process-group kills; the exec's environment tag does not."""
    started = started_at(client, sandbox)
    res = run(
        sandbox.exec(
            ["sh", "-c", "setsid sleep 302 & setsid sleep 303 & sleep 304"],
            network=NONE,
            timeout_s=2,
        )
    )
    assert res.timed_out is True
    assert alive(sandbox, run) == []
    assert started_at(client, sandbox) == started


@pytest.mark.docker
def test_a_timeout_keeps_the_output_written_so_far(sandbox: sb.Sandbox, run: Runner) -> None:
    res = run(
        sandbox.exec(
            ["sh", "-c", "echo started; echo oops >&2; sleep 300"], network=NONE, timeout_s=3
        )
    )
    assert res.timed_out is True
    assert res.stdout_tail == "started\n"
    assert res.stderr_tail == "oops\n"


@pytest.mark.docker
def test_cancelling_an_exec_kills_the_command(
    client: docker.DockerClient, sandbox: sb.Sandbox, run: Runner
) -> None:
    started = started_at(client, sandbox)

    async def scenario() -> None:
        task = asyncio.create_task(sandbox.exec(["sleep", "305"], network=NONE, timeout_s=600))
        await asyncio.sleep(2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    assert alive(sandbox, run) == []
    assert started_at(client, sandbox) == started


@pytest.mark.docker
def test_timeouts_are_not_confused_with_a_command_that_fits(
    sandbox: sb.Sandbox, run: Runner
) -> None:
    res = run(sandbox.exec(["sh", "-c", "sleep 1; echo done"], network=NONE, timeout_s=20))
    assert (res.timed_out, res.exit_code, res.stdout_tail) == (False, 0, "done\n")


# ---------------------------------------------------------------- limits hold (§9.3)

#: `exec` so the exit status is awk's own; doubling a string reaches any limit in a second.
MEMORY_HOG = "exec awk 'BEGIN{s=\"x\"; while(1) s=s s}'"
FORK_BOMB = "n=0; while [ $n -lt 300 ]; do sleep 301 & n=$((n+1)); done; echo spawned=$n; wait"


@pytest.mark.docker
@pytest.mark.parametrize(
    "runtime", [ContainerRuntime.RUNSC, ContainerRuntime.RUNC], ids=["runsc", "runc"]
)
@pytest.mark.parametrize("hog", ["memory", "pids"])
def test_the_memory_and_pids_limits_hold(
    driver: DockerDriver,
    run_id: RunId,
    image: str,
    workspace: Path,
    run: Runner,
    runtime: ContainerRuntime,
    hog: str,
) -> None:
    """Under runc a command is refused or killed; under gVisor the whole sandbox dies. Either
    way the command does not succeed, the host is untouched, and the container comes back."""
    box = run(driver.create(make_spec(run_id, image, workspace, prefer=runtime)))
    try:
        script = MEMORY_HOG if hog == "memory" else FORK_BOMB
        res = run(box.exec(["sh", "-c", script], network=NONE, timeout_s=120))
        assert res.exit_code != 0
        assert "spawned=300" not in res.stdout_tail
        assert res.timed_out is False
        if hog == "memory":
            assert res.exit_code == 137  # runc: the OOM killer; runsc: the sandbox, restarted
        if runtime is ContainerRuntime.RUNSC:
            assert res.exit_code == 137
            assert "killed" in res.stderr_tail
            assert "restarted" in res.stderr_tail
        after = run(box.exec(["echo", "alive"], network=NONE, timeout_s=60))
        assert (after.exit_code, after.stdout_tail) == (0, "alive\n")
        assert alive(box, run) == []  # the restart left no stray processes
    finally:
        run(box.destroy())


@pytest.mark.docker
def test_a_timeout_restarts_the_container_when_the_kill_command_cannot_finish(
    client: docker.DockerClient, sandbox: sb.Sandbox, run: Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The last resort: if the tag kill fails (here forced), restarting kills everything."""
    assert isinstance(sandbox, DockerSandbox)
    monkeypatch.setattr(sandbox, "_run_killer", lambda tag: False)
    started = started_at(client, sandbox)
    res = run(sandbox.exec(["sh", "-c", "sleep 308 & sleep 309 & wait"], network=NONE, timeout_s=2))
    assert res.timed_out is True
    assert started_at(client, sandbox) != started
    assert alive(sandbox, run) == []


@pytest.mark.docker
def test_a_timeout_still_kills_when_the_pids_limit_is_exhausted(
    driver: DockerDriver, run_id: RunId, image: str, workspace: Path, run: Runner
) -> None:
    """Under runc the kill command itself cannot fork once the limit is reached; the driver
    then restarts the container instead."""
    box = run(
        driver.create(make_spec(run_id, image, workspace, pids=64, prefer=ContainerRuntime.RUNC))
    )
    try:
        script = "while :; do (sleep 306 &) 2>/dev/null; done"
        res = run(box.exec(["sh", "-c", script], network=NONE, timeout_s=3))
        assert res.timed_out is True
        assert alive(box, run) == []
    finally:
        run(box.destroy())


# ---------------------------------------------------------------- destroy and reap


@pytest.mark.docker
def test_destroy_removes_the_container_and_is_idempotent(
    client: docker.DockerClient,
    driver: DockerDriver,
    run_id: RunId,
    image: str,
    workspace: Path,
    run: Runner,
) -> None:
    box = run(driver.create(make_spec(run_id, image, workspace)))
    assert isinstance(box, DockerSandbox)
    cid = box.container_id
    run(box.destroy())
    run(box.destroy())
    with pytest.raises(NotFound):
        client.containers.get(cid)
    with pytest.raises(RuntimeError, match="destroyed"):
        run(box.exec(["true"], network=NONE, timeout_s=10))


@pytest.mark.docker
def test_destroy_copes_with_a_container_removed_behind_its_back(
    client: docker.DockerClient,
    driver: DockerDriver,
    run_id: RunId,
    image: str,
    workspace: Path,
    run: Runner,
) -> None:
    box = run(driver.create(make_spec(run_id, image, workspace)))
    assert isinstance(box, DockerSandbox)
    client.containers.get(box.container_id).remove(force=True)
    with pytest.raises(RuntimeError, match=r"gone|broken|exist"):
        run(box.exec(["true"], network=NONE, timeout_s=10))
    run(box.destroy())


@pytest.mark.docker
def test_reap_removes_every_container_and_network_of_one_run(
    client: docker.DockerClient,
    driver: DockerDriver,
    run_id: RunId,
    image: str,
    tmp_path: Path,
    run: Runner,
) -> None:
    other = new_run_id()
    ws = [tmp_path / n for n in ("a", "b", "c")]
    for path in ws:
        path.mkdir()
    boxes = [
        run(driver.create(make_spec(run_id, image, ws[0]))),
        run(driver.create(make_spec(run_id, image, ws[1]))),
        run(driver.create(make_spec(other, image, ws[2]))),
    ]
    net = client.networks.create(f"mpilot-test-{run_id}", labels={LABEL_RUN: run_id}, internal=True)
    try:
        assert run(driver.reap(run_id)) == 3  # two containers and the network
        assert client.containers.list(all=True, filters={"label": f"{LABEL_RUN}={run_id}"}) == []
        assert client.networks.list(filters={"label": f"{LABEL_RUN}={run_id}"}) == []
        survivors = client.containers.list(all=True, filters={"label": f"{LABEL_RUN}={other}"})
        assert len(survivors) == 1
        assert run(driver.reap(run_id)) == 0  # idempotent
        assert run(boxes[2].exec(["echo", "ok"], network=NONE, timeout_s=30)).stdout_tail == "ok\n"
        del net
    finally:
        run(driver.reap(other))


@pytest.mark.docker
def test_reap_removes_a_running_container_that_a_command_is_using(
    client: docker.DockerClient,
    driver: DockerDriver,
    run_id: RunId,
    image: str,
    workspace: Path,
    run: Runner,
) -> None:
    box = run(driver.create(make_spec(run_id, image, workspace)))

    async def scenario() -> int:
        task = asyncio.create_task(box.exec(["sleep", "307"], network=NONE, timeout_s=600))
        await asyncio.sleep(2)
        removed = await driver.reap(run_id)
        with pytest.raises((RuntimeError, MpilotError)):
            await asyncio.wait_for(task, 60)
        return removed

    assert run(scenario()) == 1
    assert client.containers.list(all=True, filters={"label": f"{LABEL_RUN}={run_id}"}) == []


# ---------------------------------------------------------------- not yet built
