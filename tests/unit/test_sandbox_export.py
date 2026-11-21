"""P2-04: `Sandbox.export_workspace` and `SandboxDriver.restore` against real containers
(SPEC §9.5, FR-21; US-6 pause and resume).

The pause flow is: freeze, export, tear down, and later `restore` into a fresh sandbox whose
workspace must hash identically, `.git` included, with links still links.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from pathlib import Path
from typing import Any

import docker
import pytest
from docker.errors import APIError
from docker_helpers import (  # noqa: F401  (fixtures are used by name)
    NONE,
    FaultyClient,
    FaultyContainers,
    Runner,
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

from mpilot.core.errors import ExportIntegrityError, PathEscapeError
from mpilot.core.types import RunId
from mpilot.sandbox import base as sb
from mpilot.sandbox.base import ContainerRuntime
from mpilot.sandbox.driver import LABEL_RUN, DockerDriver, DockerSandbox
from mpilot.sandbox.workspace import HostWorkspace

BUILD_TREE = r"""
set -e
mkdir -p .git/objects/ab .git/refs/tags node_modules/pkg build/empty src bin vendor/lib/.git
echo 'ref: refs/heads/main' > .git/HEAD
printf 'object-bytes' > .git/objects/ab/cdef
echo 'ref: nested' > vendor/lib/.git/HEAD
echo 'print(1)' > src/app.py
printf '#!/bin/sh\necho hi\n' > bin/run.sh; chmod 755 bin/run.sh
echo 'module.exports = 1' > node_modules/pkg/index.js
ln -s /etc/passwd abs-link
ln -s ../outside rel-link
ln -s src/app.py inner-link
ln -s node_modules dir-link
ln -s /no/such/target dangling
: > empty.txt
"""


def _sh(box: sb.Sandbox, run: Runner, script: str) -> sb.ExecResult:
    return run(box.exec(["sh", "-c", script], network=NONE, timeout_s=60))


def _contents(directory: Path) -> bytes:
    return b"".join(
        p.read_bytes() for p in directory.rglob("*") if p.is_file() and not p.is_symlink()
    )


# ---------------------------------------------------------------- the pause flow


@pytest.mark.docker
def test_export_destroy_restore_gives_an_identical_tree_including_git(
    client: docker.DockerClient,
    driver: DockerDriver,
    run_id: RunId,
    image: str,
    tmp_path: Path,
    run: Runner,
) -> None:
    ws1, ws2 = tmp_path / "ws1", tmp_path / "ws2"
    ws1.mkdir()
    box1 = run(driver.create(make_spec(run_id, image, ws1)))
    assert _sh(box1, run, BUILD_TREE).exit_code == 0

    export = tmp_path / "export"
    manifest = run(box1.export_workspace(export))
    assert manifest.export_dir == str(export)
    assert (
        manifest.entries[".git/HEAD"].sha256
        == hashlib.sha256(b"ref: refs/heads/main\n").hexdigest()
    )
    assert manifest.entries["vendor/lib/.git/HEAD"].mode == "100644"
    assert manifest.entries["bin/run.sh"].mode == "100755"
    for link in ("abs-link", "rel-link", "inner-link", "dir-link", "dangling"):
        assert manifest.entries[link].mode == "120000", link
    # The sandbox is running again after the export.
    state = inspect(client, box1)["State"]
    assert (state["Running"], state["Paused"]) == (True, False)
    assert run(box1.exec(["echo", "ok"], network=NONE, timeout_s=30)).stdout_tail == "ok\n"

    digest = manifest.digest()
    run(box1.destroy())
    assert client.containers.list(all=True, filters={"label": f"{LABEL_RUN}={run_id}"}) == []

    # A symlink is exported as a link: neither the host's /etc/passwd nor anything behind
    # the other links ended up in the export.
    assert os.readlink(export / "abs-link") == "/etc/passwd"
    assert b"root:x:0:0" not in _contents(export)

    box2 = run(driver.restore(make_spec(run_id, image, ws2), manifest, expected_digest=digest))
    try:
        assert isinstance(box2, DockerSandbox)
        links = "for l in abs-link rel-link inner-link; do readlink $l; done"
        assert _sh(box2, run, links).stdout_tail == ("/etc/passwd\n../outside\nsrc/app.py\n")
        assert _sh(
            box2, run, "cat .git/HEAD; test -x bin/run.sh && echo exec; ls -d build/empty"
        ).stdout_tail == ("ref: refs/heads/main\nexec\nbuild/empty\n")
        assert _sh(box2, run, "cat dir-link/pkg/index.js").stdout_tail == "module.exports = 1\n"
        owner = _sh(box2, run, "stat -c %u:%g src/app.py .git/HEAD").stdout_tail.split()
        assert owner == [f"{os.getuid()}:{os.getgid()}"] * 2
        # "Identical tree hash": exporting the restored workspace reproduces the manifest.
        again = run(box2.export_workspace(tmp_path / "export2"))
        assert again.entries == manifest.entries
        assert again.digest() == digest
    finally:
        run(box2.destroy())


@pytest.mark.docker
def test_a_process_running_at_export_time_cannot_make_the_manifest_lie(
    client: docker.DockerClient,
    sandbox: sb.Sandbox,
    workspace: Path,
    tmp_path: Path,
    run: Runner,
) -> None:
    spinner = "i=0; while :; do i=$((i+1)); echo $i > count.tmp; mv -f count.tmp count; done"
    assert _sh(sandbox, run, f"sh -c '{spinner}' >/dev/null 2>&1 &").exit_code == 0
    time.sleep(0.5)
    manifest = run(sandbox.export_workspace(tmp_path / "export"))
    exported = (tmp_path / "export" / "count").read_bytes()
    assert manifest.entries["count"].sha256 == hashlib.sha256(exported).hexdigest()
    # The spinner was stopped during the copy and runs again afterwards.
    before = int((workspace / "count").read_text())
    deadline = time.monotonic() + 10
    while int((workspace / "count").read_text()) == before:
        assert time.monotonic() < deadline, "the spinner never resumed"
        time.sleep(0.05)
    assert int(exported) <= int((workspace / "count").read_text())


@pytest.mark.docker
def test_a_cancelled_export_keeps_the_sandbox_frozen_until_the_copy_has_finished(
    client: docker.DockerClient,
    sandbox: sb.Sandbox,
    tmp_path: Path,
    run: Runner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _sh(sandbox, run, "echo data > a.txt").exit_code == 0
    real = HostWorkspace.export_to

    def slow_export(self: HostWorkspace, dest: Path) -> Any:
        time.sleep(1.5)
        return real(self, dest)

    monkeypatch.setattr(HostWorkspace, "export_to", slow_export)
    seen: dict[str, bool] = {}

    async def scenario() -> None:
        task = asyncio.create_task(sandbox.export_workspace(tmp_path / "export"))
        await asyncio.sleep(0.5)  # frozen, copy under way
        task.cancel()
        await asyncio.sleep(0.2)
        seen["paused_after_cancel"] = bool(inspect(client, sandbox)["State"]["Paused"])
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    assert seen["paused_after_cancel"] is True  # not thawed under a thread that is still copying
    assert (tmp_path / "export" / "a.txt").read_text() == "data\n"  # the copy ran to the end
    state = inspect(client, sandbox)["State"]
    assert (state["Running"], state["Paused"]) == (True, False)
    assert run(sandbox.exec(["echo", "ok"], network=NONE, timeout_s=30)).stdout_tail == "ok\n"


@pytest.mark.docker
def test_a_refused_export_leaves_the_sandbox_running(
    client: docker.DockerClient,
    driver: DockerDriver,
    run_id: RunId,
    image: str,
    tmp_path: Path,
    run: Runner,
) -> None:
    """A FIFO only reaches the host under runc (gVisor keeps it inside the sandbox), so the
    refusal is shown there; the sandbox must be running again either way."""
    ws = tmp_path / "ws"
    ws.mkdir()
    box = run(driver.create(make_spec(run_id, image, ws, prefer=ContainerRuntime.RUNC)))
    try:
        assert _sh(box, run, "mkfifo pipe").exit_code == 0
        with pytest.raises(PathEscapeError, match="FIFO"):
            run(box.export_workspace(tmp_path / "export"))
        with pytest.raises(ValueError, match="inside the workspace"):
            run(box.export_workspace(ws / "export"))
        assert list((tmp_path / "export").iterdir()) == []
        state = inspect(client, box)["State"]
        assert (state["Running"], state["Paused"]) == (True, False)
        assert box.is_frozen is False
    finally:
        run(box.destroy())


# ---------------------------------------------------------------- exporting without a container


@pytest.mark.docker
def test_a_destroyed_sandbox_can_still_export_its_workspace(
    driver: DockerDriver, run_id: RunId, image: str, tmp_path: Path, run: Runner
) -> None:
    """No container mounts the workspace once `destroy` has verified it gone, so it can be
    read without a freeze (the route for a sandbox that could not be resumed)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    box = run(driver.create(make_spec(run_id, image, ws)))
    assert _sh(box, run, "echo data > a.txt; ln -s a.txt l").exit_code == 0
    run(box.destroy())
    manifest = run(box.export_workspace(tmp_path / "export"))
    assert sorted(manifest.entries) == ["a.txt", "l"]
    assert manifest.entries["l"].mode == "120000"


@pytest.mark.docker
def test_a_broken_sandbox_refuses_export_until_it_is_destroyed(
    driver: DockerDriver,
    run_id: RunId,
    image: str,
    tmp_path: Path,
    run: Runner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    box = run(driver.create(make_spec(run_id, image, ws)))
    assert isinstance(box, DockerSandbox)
    assert _sh(box, run, "echo data > a.txt").exit_code == 0

    def failing_unpause() -> None:
        raise RuntimeError("unpause failed (fault injected)")

    monkeypatch.setattr(box, "_unpause", failing_unpause)
    with pytest.raises(RuntimeError, match="fault injected"):
        run(
            box.export_workspace(tmp_path / "export1")
        )  # the export itself worked, the thaw did not
    with pytest.raises(RuntimeError, match=r"broken.*destroy"):
        run(box.export_workspace(tmp_path / "export2"))
    run(box.destroy())
    manifest = run(box.export_workspace(tmp_path / "export3"))
    assert manifest.entries["a.txt"].sha256 == hashlib.sha256(b"data\n").hexdigest()


# ---------------------------------------------------------------- restore refusals


def _export_of_a_small_run(
    driver: DockerDriver, run_id: RunId, image: str, tmp_path: Path, run: Runner
) -> tuple[Any, Path]:
    ws = tmp_path / "ws-orig"
    ws.mkdir()
    box = run(driver.create(make_spec(run_id, image, ws)))
    assert _sh(box, run, "echo alpha > a.txt; mkdir sub; echo beta > sub/b.txt").exit_code == 0
    manifest = run(box.export_workspace(tmp_path / "export"))
    run(box.destroy())
    return manifest, tmp_path / "export"


@pytest.mark.docker
def test_restore_refuses_a_digest_that_is_not_the_checkpoints_and_starts_nothing(
    client: docker.DockerClient,
    driver: DockerDriver,
    run_id: RunId,
    image: str,
    tmp_path: Path,
    run: Runner,
) -> None:
    manifest, _ = _export_of_a_small_run(driver, run_id, image, tmp_path, run)
    target = tmp_path / "ws-new"
    with pytest.raises(ExportIntegrityError, match="digest"):
        run(driver.restore(make_spec(run_id, image, target), manifest, expected_digest="0" * 64))
    assert not target.exists()  # refused before anything was created
    assert client.containers.list(all=True, filters={"label": f"{LABEL_RUN}={run_id}"}) == []


@pytest.mark.docker
def test_restore_refuses_a_tampered_export_and_starts_nothing(
    client: docker.DockerClient,
    driver: DockerDriver,
    run_id: RunId,
    image: str,
    tmp_path: Path,
    run: Runner,
) -> None:
    manifest, export = _export_of_a_small_run(driver, run_id, image, tmp_path, run)
    (export / "a.txt").write_text("TAMPERED\n")
    target = tmp_path / "ws-new"
    with pytest.raises(ExportIntegrityError, match="digest"):
        run(
            driver.restore(
                make_spec(run_id, image, target), manifest, expected_digest=manifest.digest()
            )
        )
    assert list(target.iterdir()) == []  # nothing half-restored
    assert client.containers.list(all=True, filters={"label": f"{LABEL_RUN}={run_id}"}) == []


@pytest.mark.docker
def test_restore_never_merges_into_a_workspace_that_has_files(
    client: docker.DockerClient,
    driver: DockerDriver,
    run_id: RunId,
    image: str,
    tmp_path: Path,
    run: Runner,
) -> None:
    manifest, _ = _export_of_a_small_run(driver, run_id, image, tmp_path, run)
    target = tmp_path / "ws-new"
    target.mkdir()
    (target / "leftover.txt").write_text("keep\n")
    with pytest.raises(ValueError, match="not empty"):
        run(
            driver.restore(
                make_spec(run_id, image, target), manifest, expected_digest=manifest.digest()
            )
        )
    assert [p.name for p in target.iterdir()] == ["leftover.txt"]
    assert client.containers.list(all=True, filters={"label": f"{LABEL_RUN}={run_id}"}) == []


@pytest.mark.docker
def test_restore_into_an_existing_empty_directory_works_and_a_link_target_is_refused(
    driver: DockerDriver, run_id: RunId, image: str, tmp_path: Path, run: Runner
) -> None:
    manifest, _ = _export_of_a_small_run(driver, run_id, image, tmp_path, run)
    spec_for = lambda path: make_spec(run_id, image, path)  # noqa: E731
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    link = tmp_path / "ws-link"
    link.symlink_to(real_dir)
    with pytest.raises(ValueError, match="symlink"):
        run(driver.restore(spec_for(link), manifest, expected_digest=manifest.digest()))
    assert list(real_dir.iterdir()) == []
    box = run(driver.restore(spec_for(real_dir), manifest, expected_digest=manifest.digest()))
    try:
        assert _sh(box, run, "cat a.txt sub/b.txt").stdout_tail == "alpha\nbeta\n"
    finally:
        run(box.destroy())


@pytest.mark.docker
def test_a_missing_image_is_refused_before_anything_is_copied(
    driver: DockerDriver, run_id: RunId, image: str, tmp_path: Path, run: Runner
) -> None:
    manifest, _ = _export_of_a_small_run(driver, run_id, image, tmp_path, run)
    target = tmp_path / "ws-new"
    missing = "no-such-image-mpilot@sha256:" + "0" * 64
    with pytest.raises(RuntimeError, match="image"):
        run(
            driver.restore(
                make_spec(run_id, missing, target), manifest, expected_digest=manifest.digest()
            )
        )
    assert not target.exists()


@pytest.mark.docker
def test_a_sandbox_that_fails_to_start_after_restore_leaves_the_workspace_empty_for_a_retry(
    client: docker.DockerClient,
    driver: DockerDriver,
    run_id: RunId,
    image: str,
    tmp_path: Path,
    run: Runner,
) -> None:
    manifest, _ = _export_of_a_small_run(driver, run_id, image, tmp_path, run)
    target = tmp_path / "ws-new"

    class Broken(FaultyContainers):
        def create(self, *args: Any, **kwargs: Any) -> Any:
            raise APIError("No such image: broken (fault injected)")

    faulty = FaultyClient(client, [])
    faulty.containers = Broken(client.containers, [])  # type: ignore[assignment]
    with pytest.raises(APIError, match="fault injected"):
        run(
            DockerDriver(faulty).restore(  # type: ignore[arg-type]
                make_spec(run_id, image, target), manifest, expected_digest=manifest.digest()
            )
        )
    assert list(target.iterdir()) == []
    box = run(
        driver.restore(
            make_spec(run_id, image, target), manifest, expected_digest=manifest.digest()
        )
    )
    try:
        assert _sh(box, run, "cat a.txt").stdout_tail == "alpha\n"
    finally:
        run(box.destroy())
