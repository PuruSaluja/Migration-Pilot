"""P2-05 (D6, SPEC §9.3): a command that writes past the disk limit is killed, and `/tmp` is
capped. Real containers. The limit is lowered on the sandbox (`disk_limit_bytes`) so a test
need not write 20 GB; `disk_poll_s` is lowered so it need not wait long.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import docker
import pytest
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
    started_at,
    workspace,
)

from mpilot.core.types import RunId
from mpilot.sandbox import base as sb
from mpilot.sandbox.base import ContainerRuntime
from mpilot.sandbox.driver import DockerDriver, DockerSandbox

MIB = 1024 * 1024
#: Writes 1 GiB in 1 MiB blocks, far past any limit used here, unless something stops it.
WRITE_A_GIB = "dd if=/dev/zero of=/workspace/big.bin bs=1M count=1024 2>&1"


def _tuned(box: sb.Sandbox, *, limit_mib: int = 20) -> DockerSandbox:
    assert isinstance(box, DockerSandbox)
    box.disk_limit_bytes = limit_mib * MIB
    box.disk_poll_s = 0.3
    return box


@pytest.mark.docker
def test_a_command_writing_past_the_limit_is_killed_as_a_disk_limit(
    client: docker.DockerClient, sandbox: sb.Sandbox, workspace: Path, run: Runner
) -> None:
    box = _tuned(sandbox)
    started = started_at(client, box)
    began = time.monotonic()
    res = run(box.exec(["sh", "-c", WRITE_A_GIB], network=NONE, timeout_s=120))
    assert res.killed_disk_limit is True
    assert res.timed_out is False
    assert res.exit_code == -1
    assert "disk limit" in res.stderr_tail
    assert time.monotonic() - began < 60  # stopped by the poll, not by the 120 s timeout
    # It was stopped soon after the limit: well under the 1 GiB it was told to write.
    written = (workspace / "big.bin").stat().st_size
    assert 20 * MIB < written < 600 * MIB
    # Killed by tag, the container is untouched and usable.
    assert started_at(client, box) == started
    assert alive(box, run) == []


@pytest.mark.docker
def test_the_workspace_can_be_cleaned_up_after_a_disk_kill(
    sandbox: sb.Sandbox, workspace: Path, run: Runner
) -> None:
    """A short command on an over-limit workspace finishes before the first poll, so the
    command that frees the space is not itself killed."""
    box = _tuned(sandbox)
    assert run(box.exec(["sh", "-c", WRITE_A_GIB], network=NONE, timeout_s=120)).killed_disk_limit
    box.disk_poll_s = 1.0
    cleaned = run(box.exec(["rm", "/workspace/big.bin"], network=NONE, timeout_s=30))
    assert (cleaned.exit_code, cleaned.killed_disk_limit) == (0, False)
    assert not (workspace / "big.bin").exists()
    after = run(box.exec(["sh", "-c", "echo fine > /workspace/ok.txt"], network=NONE, timeout_s=30))
    assert after.exit_code == 0


@pytest.mark.docker
def test_a_command_under_the_limit_is_not_touched(
    sandbox: sb.Sandbox, workspace: Path, run: Runner
) -> None:
    box = _tuned(sandbox)
    script = (
        "dd if=/dev/zero of=/workspace/small.bin bs=1M count=5 2>/dev/null; sleep 1.5; echo done"
    )
    res = run(box.exec(["sh", "-c", script], network=NONE, timeout_s=60))
    assert (res.exit_code, res.stdout_tail, res.killed_disk_limit) == (0, "done\n", False)
    assert (workspace / "small.bin").stat().st_size == 5 * MIB


@pytest.mark.docker
def test_many_small_files_count_too(sandbox: sb.Sandbox, workspace: Path, run: Runner) -> None:
    """Disk use is allocated blocks: a swarm of 1-byte files fills the disk as much as one
    big file of the same allocated size."""
    box = _tuned(sandbox, limit_mib=8)
    script = (
        "mkdir -p /workspace/many; i=0; "
        "while [ $i -lt 20000 ]; do echo x > /workspace/many/f$i; i=$((i+1)); done; "
        "sleep 30"
    )
    res = run(box.exec(["sh", "-c", script], network=NONE, timeout_s=120))
    assert res.killed_disk_limit is True


@pytest.mark.docker
def test_the_git_directory_counts(sandbox: sb.Sandbox, workspace: Path, run: Runner) -> None:
    box = _tuned(sandbox)
    script = (
        "mkdir -p /workspace/.git/objects; "
        "dd if=/dev/zero of=/workspace/.git/objects/pack bs=1M count=1024 2>&1"
    )
    assert run(box.exec(["sh", "-c", script], network=NONE, timeout_s=120)).killed_disk_limit


@pytest.mark.docker
def test_the_limit_comes_from_the_spec_in_gigabytes(
    driver: DockerDriver, run_id: RunId, image: str, workspace: Path, run: Runner
) -> None:
    box = run(driver.create(make_spec(run_id, image, workspace)))
    try:
        assert isinstance(box, DockerSandbox)
        assert box.disk_limit_bytes == 1024 * MIB  # `ResourceLimits.disk_gb` is 1 in make_spec
    finally:
        run(box.destroy())


@pytest.mark.docker
def test_a_timeout_is_still_a_timeout_when_the_disk_is_fine(
    sandbox: sb.Sandbox, run: Runner
) -> None:
    box = _tuned(sandbox)
    res = run(box.exec(["sleep", "300"], network=NONE, timeout_s=2))
    assert (res.timed_out, res.killed_disk_limit, res.exit_code) == (True, False, -1)


@pytest.mark.docker
def test_cancelling_a_command_stops_the_disk_monitor(sandbox: sb.Sandbox, run: Runner) -> None:
    box = _tuned(sandbox)

    async def scenario() -> int:
        task = asyncio.create_task(box.exec(["sleep", "314"], network=NONE, timeout_s=600))
        await asyncio.sleep(1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.5)
        return sum(1 for t in asyncio.all_tasks() if "_disk_watch" in repr(t))

    assert run(scenario()) == 0  # no monitor task left behind


@pytest.mark.docker
def test_a_workspace_that_cannot_be_measured_kills_the_command_rather_than_run_unguarded(
    sandbox: sb.Sandbox, run: Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    box = _tuned(sandbox)

    def broken() -> int:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(box, "_disk_usage", broken)
    res = run(box.exec(["sleep", "315"], network=NONE, timeout_s=60))
    assert res.killed_disk_limit is True
    assert "could not be measured" in res.stderr_tail


@pytest.mark.docker
def test_a_tree_too_deep_to_measure_counts_as_over_the_limit(
    sandbox: sb.Sandbox, run: Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mpilot.core.errors import PathEscapeError

    box = _tuned(sandbox)

    def too_deep() -> int:
        raise PathEscapeError("the tree is deeper than 256 levels")

    monkeypatch.setattr(box, "_disk_usage", too_deep)
    res = run(box.exec(["sleep", "316"], network=NONE, timeout_s=60))
    assert res.killed_disk_limit is True
    assert "deeper" in res.stderr_tail


# ---------------------------------------------------------------- /tmp is capped


@pytest.mark.docker
@pytest.mark.parametrize(
    "runtime", [ContainerRuntime.RUNSC, ContainerRuntime.RUNC], ids=["runsc", "runc"]
)
def test_tmp_is_a_capped_tmpfs_that_fails_with_no_space_instead_of_killing_anything(
    client: docker.DockerClient,
    driver: DockerDriver,
    run_id: RunId,
    image: str,
    workspace: Path,
    run: Runner,
    runtime: ContainerRuntime,
) -> None:
    box = run(driver.create(make_spec(run_id, image, workspace, memory_gb=2, prefer=runtime)))
    try:
        assert f"size={1024 * MIB}" in inspect(client, box)["HostConfig"]["Tmpfs"]["/tmp"]
        started = started_at(client, box)
        script = "dd if=/dev/zero of=/tmp/fill bs=1M count=3000 2>&1; echo rc=$?; ls -l /tmp/fill"
        res = run(box.exec(["sh", "-c", script], network=NONE, timeout_s=120))
        assert "No space left on device" in res.stdout_tail
        assert "rc=1" in res.stdout_tail
        listing = run(box.exec(["ls", "-l", "/tmp/fill"], network=NONE, timeout_s=30)).stdout_tail
        written = int(listing.split()[4])
        assert 1000 * MIB <= written <= 1024 * MIB  # capped at 1 GiB
        # Nothing died: same container, still usable, and the host disk was never involved.
        assert started_at(client, box) == started
        assert run(box.exec(["echo", "ok"], network=NONE, timeout_s=30)).stdout_tail == "ok\n"
        assert os.listdir(workspace) == []
    finally:
        run(box.destroy())


@pytest.mark.docker
def test_a_small_memory_limit_shrinks_the_tmp_cap_so_tmpfs_cannot_exhaust_memory(
    client: docker.DockerClient, sandbox: sb.Sandbox, run: Runner
) -> None:
    """`make_spec` has a 1 GiB memory limit: the cap is half of it, and filling /tmp to the
    cap leaves the sandbox alive (a 1 GiB cap would have hit the memory limit first)."""
    assert f"size={512 * MIB}" in inspect(client, sandbox)["HostConfig"]["Tmpfs"]["/tmp"]
    started = started_at(client, sandbox)
    res = run(
        sandbox.exec(
            ["sh", "-c", "dd if=/dev/zero of=/tmp/fill bs=1M count=900 2>&1; echo rc=$?"],
            network=NONE,
            timeout_s=120,
        )
    )
    assert "No space left on device" in res.stdout_tail
    assert started_at(client, sandbox) == started
