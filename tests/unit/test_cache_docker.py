"""P2-07b (SPEC §9.3, NFR-5): package caches mounted into real containers.

The work container sees the cache read-only; a write from it fails. The install container is
P2-09, so the read-write side is shown with a stand-in: a plain container started with the same
`cache_volume(..., CacheRole.INSTALL)` the install container will use.
"""

from __future__ import annotations

import os
from pathlib import Path

import docker
import pytest
from docker_helpers import (  # noqa: F401  (fixtures are used by name)
    NONE,
    Runner,
    client,
    image,
    inspect,
    make_spec,
    run,
    run_id,
    workspace,
)

from mpilot.core.types import RunId
from mpilot.sandbox import images as im
from mpilot.sandbox.base import CacheMode, ContainerRuntime, Sandbox
from mpilot.sandbox.cache import (
    CACHE_ENV,
    CacheRole,
    CacheStore,
    cache_env,
    cache_key,
    cache_volume,
)
from mpilot.sandbox.driver import DockerDriver, DockerSandbox

pytestmark = pytest.mark.docker

COMMIT = "a" * 40
KEY = cache_key("node", {"package-lock.json": b"{}"})
OTHER_KEY = cache_key("node", {"package-lock.json": b'{"changed": true}'})
MIB = 1024 * 1024
RUNTIMES = [ContainerRuntime.RUNSC, ContainerRuntime.RUNC]


@pytest.fixture
def cache_root(tmp_path: Path) -> Path:
    return tmp_path / "cache"


@pytest.fixture
def driver(client: docker.DockerClient, cache_root: Path) -> DockerDriver:
    return DockerDriver(client, cache_root=cache_root)


def make(
    driver: DockerDriver,
    run: Runner,
    rid: RunId,
    image: str,
    workspace: Path,
    **kw: object,
) -> DockerSandbox:
    box = run(driver.create(make_spec(rid, image, workspace, **kw)))  # type: ignore[arg-type]
    assert isinstance(box, DockerSandbox)
    return box


def sh(run: Runner, box: Sandbox, script: str, timeout_s: int = 60) -> tuple[int, str, str]:
    res = run(box.exec(["sh", "-c", script], network=NONE, timeout_s=timeout_s))
    return res.exit_code, res.stdout_tail, res.stderr_tail


def fresh_spec_args() -> dict[str, object]:
    return {"cache": CacheMode.FRESH, "from_commit": COMMIT}


def fill(path: Path, size: int, name: str = "blob") -> None:
    """Real, non-sparse bytes, so the allocated size is what the test says."""
    (path / name).write_bytes(os.urandom(size))


# --- the work container's mount ------------------------------------------------------------


@pytest.mark.parametrize("runtime", RUNTIMES, ids=lambda r: r.value)
def test_the_cache_is_mounted_read_only_and_a_write_from_the_work_container_fails(
    runtime: ContainerRuntime,
    driver: DockerDriver,
    client: docker.DockerClient,
    run_id: RunId,
    image: str,
    workspace: Path,
    run: Runner,
) -> None:
    box = make(driver, run, run_id, image, workspace, cache_key=KEY, prefer=runtime)
    cache = box.cache_dir
    assert cache is not None
    (cache / "from-host.txt").write_text("hello")
    assert sh(run, box, "cat /cache/from-host.txt")[1] == "hello"  # the host's writes are visible

    code, _, err = sh(run, box, "echo x > /cache/from-container.txt")
    assert code != 0
    assert "read-only" in err.lower()
    for argv in (
        "touch /cache/t",
        "mkdir /cache/d",
        "rm /cache/from-host.txt",
        "mv /cache/from-host.txt /cache/m",
    ):
        assert sh(run, box, argv)[0] != 0, argv
    # a remount to read-write needs CAP_SYS_ADMIN, which is dropped; and it fails regardless
    assert sh(run, box, "mount -o remount,rw /cache")[0] != 0
    assert sorted(p.name for p in cache.iterdir()) == ["from-host.txt"], "nothing reached the host"
    assert (cache / "from-host.txt").read_text() == "hello"


def test_the_mount_is_read_only_and_the_workspace_and_cache_are_the_only_host_binds(
    driver: DockerDriver,
    client: docker.DockerClient,
    cache_root: Path,
    run_id: RunId,
    image: str,
    workspace: Path,
    run: Runner,
) -> None:
    box = make(driver, run, run_id, image, workspace, cache_key=KEY)
    binds = {m["Destination"]: m for m in inspect(client, box)["Mounts"] if m["Type"] == "bind"}
    assert set(binds) == {"/workspace", "/cache"}
    assert binds["/cache"]["RW"] is False
    assert binds["/cache"]["Source"] == str(cache_root / KEY)
    assert binds["/workspace"]["RW"] is True
    assert (cache_root / KEY).is_dir()


def test_the_package_managers_are_pointed_at_the_cache_by_the_container_environment(
    driver: DockerDriver,
    client: docker.DockerClient,
    run_id: RunId,
    image: str,
    workspace: Path,
    run: Runner,
) -> None:
    box = make(driver, run, run_id, image, workspace, cache_key=KEY)
    env = dict(e.split("=", 1) for e in inspect(client, box)["Config"]["Env"])
    for name, value in cache_env(CacheRole.WORK).items():
        assert env[name] == value
    assert "YARN_CACHE_FOLDER" not in env, "yarn classic fails on a read-only cache folder"
    assert len(cache_env(CacheRole.WORK)) == len(CACHE_ENV) - 1
    assert env["HOME"] == "/tmp"
    code, out, _ = sh(run, box, "echo $npm_config_cache $PIP_CACHE_DIR")
    assert (code, out.strip()) == (0, "/cache/npm /cache/pip")


def test_a_command_cannot_move_the_cache_with_its_own_environment(
    driver: DockerDriver, run_id: RunId, image: str, workspace: Path, run: Runner
) -> None:
    box = make(driver, run, run_id, image, workspace, cache_key=KEY)
    with pytest.raises(ValueError, match="npm_config_cache"):
        run(
            box.exec(["true"], network=NONE, timeout_s=10, env={"npm_config_cache": "/workspace/c"})
        )


def test_the_same_key_shares_a_cache_and_another_key_does_not(
    driver: DockerDriver,
    cache_root: Path,
    client: docker.DockerClient,
    run_id: RunId,
    image: str,
    tmp_path: Path,
    run: Runner,
) -> None:
    ws = [tmp_path / f"w{i}" for i in range(3)]
    for w in ws:
        w.mkdir()
    first = make(driver, run, run_id, image, ws[0], cache_key=KEY)
    assert first.cache_dir is not None
    (first.cache_dir / "marker").write_text("1")
    second = make(driver, run, run_id, image, ws[1], cache_key=KEY)
    third = make(driver, run, run_id, image, ws[2], cache_key=OTHER_KEY)
    assert second.cache_dir == first.cache_dir == cache_root / KEY
    assert sh(run, second, "cat /cache/marker")[1] == "1"
    assert sh(run, third, "ls /cache | wc -l")[1].strip() == "0"
    assert third.cache_dir == cache_root / OTHER_KEY


def test_the_cache_key_names_the_directory_so_a_new_lockfile_gets_a_new_cache(
    cache_root: Path,
) -> None:
    old = cache_key("node", {"package-lock.json": b"v1"})
    new = cache_key("node", {"package-lock.json": b"v2"})
    store = CacheStore(cache_root)
    assert store.run_dir(old) != store.run_dir(new)


def test_a_malformed_key_is_refused_before_anything_is_created(
    driver: DockerDriver,
    client: docker.DockerClient,
    cache_root: Path,
    run_id: RunId,
    image: str,
    workspace: Path,
    run: Runner,
) -> None:
    with pytest.raises(ValueError, match="not a cache key"):
        make(driver, run, run_id, image, workspace, cache_key="../../etc")
    assert not [
        c for c in client.containers.list(all=True) if c.labels.get("mpilot.run_id") == str(run_id)
    ]
    assert list(cache_root.iterdir()) == []


def test_a_driver_without_a_cache_root_mounts_no_cache(
    client: docker.DockerClient, run_id: RunId, image: str, workspace: Path, run: Runner
) -> None:
    box = run(DockerDriver(client).create(make_spec(run_id, image, workspace)))
    assert isinstance(box, DockerSandbox)
    assert box.cache_dir is None
    mounts = inspect(client, box)["Mounts"]
    assert [m["Destination"] for m in mounts if m["Type"] == "bind"] == ["/workspace"]
    env = dict(e.split("=", 1) for e in inspect(client, box)["Config"]["Env"])
    assert not set(CACHE_ENV) & set(env)


def test_a_hash_verified_cache_is_not_built_yet_and_nothing_is_created(
    driver: DockerDriver,
    client: docker.DockerClient,
    cache_root: Path,
    run_id: RunId,
    image: str,
    workspace: Path,
    run: Runner,
) -> None:
    spec = make_spec(run_id, image, workspace, **fresh_spec_args())  # type: ignore[arg-type]
    spec = spec.model_copy(update={"cache": CacheMode.HASH_VERIFIED})
    with pytest.raises(NotImplementedError, match="P3-13"):
        run(driver.create(spec))
    assert list(cache_root.iterdir()) == []


# --- fresh caches -----------------------------------------------------------------------------


def test_a_fresh_cache_is_empty_unshared_and_removed_with_the_sandbox(
    driver: DockerDriver,
    cache_root: Path,
    run_id: RunId,
    image: str,
    tmp_path: Path,
    run: Runner,
) -> None:
    ws = [tmp_path / "w1", tmp_path / "w2"]
    for w in ws:
        w.mkdir()
    a = make(driver, run, run_id, image, ws[0], **fresh_spec_args())
    b = make(driver, run, run_id, image, ws[1], **fresh_spec_args())
    assert a.cache_dir is not None
    assert b.cache_dir is not None
    assert a.cache_dir != b.cache_dir
    assert a.cache_dir.parent == cache_root / "fresh"
    assert sh(run, a, "ls -A /cache | wc -l")[1].strip() == "0"
    (a.cache_dir / "x").write_text("x")
    assert sh(run, b, "ls -A /cache | wc -l")[1].strip() == "0"
    assert sh(run, a, "echo y > /cache/y")[0] != 0, (
        "a fresh cache is read-only in the work container too"
    )

    run(a.destroy())
    assert not a.cache_dir.exists()
    assert b.cache_dir.exists(), "the other sandbox's fresh cache is untouched"
    run(b.destroy())
    assert not b.cache_dir.exists()
    run(b.destroy())  # idempotent


def test_a_fresh_cache_does_not_touch_a_run_cache_of_the_same_run(
    driver: DockerDriver, cache_root: Path, run_id: RunId, image: str, tmp_path: Path, run: Runner
) -> None:
    (tmp_path / "w1").mkdir()
    (tmp_path / "w2").mkdir()
    runbox = make(driver, run, run_id, image, tmp_path / "w1", cache_key=KEY)
    assert runbox.cache_dir is not None
    (runbox.cache_dir / "kept").write_text("x")
    final = make(driver, run, run_id, image, tmp_path / "w2", **fresh_spec_args())
    run(final.destroy())
    assert (cache_root / KEY / "kept").read_text() == "x"


def test_a_fresh_cache_is_removed_when_the_container_cannot_be_started(
    client: docker.DockerClient,
    cache_root: Path,
    run_id: RunId,
    image: str,
    workspace: Path,
    run: Runner,
) -> None:
    driver = DockerDriver(
        client, runsc_name="no-such-runtime-a", runc_name="no-such-runtime-b", cache_root=cache_root
    )
    with pytest.raises(RuntimeError, match="neither"):
        run(driver.create(make_spec(run_id, image, workspace, **fresh_spec_args())))  # type: ignore[arg-type]
    fresh = cache_root / "fresh"
    assert not fresh.exists() or list(fresh.iterdir()) == []


def test_reap_removes_the_fresh_caches_of_a_crashed_run(
    driver: DockerDriver,
    client: docker.DockerClient,
    cache_root: Path,
    run_id: RunId,
    image: str,
    tmp_path: Path,
    run: Runner,
) -> None:
    (tmp_path / "w1").mkdir()
    (tmp_path / "w2").mkdir()
    final = make(driver, run, run_id, image, tmp_path / "w1", **fresh_spec_args())
    kept = make(driver, run, run_id, image, tmp_path / "w2", cache_key=KEY)
    assert final.cache_dir is not None
    assert kept.cache_dir is not None
    other = CacheStore(cache_root).fresh_dir("some-other-run")
    # the orchestrator died: no destroy(); the resume reaps the run first (§10.1)
    assert run(driver.reap(run_id)) >= 2
    assert not final.cache_dir.exists()
    assert kept.cache_dir.is_dir(), "run caches are shared, not the run's to remove"
    assert other.is_dir(), "another run's fresh cache is not touched"


# --- the disk limit ---------------------------------------------------------------------------


def test_the_cache_counts_toward_the_disk_limit(
    driver: DockerDriver, run_id: RunId, image: str, workspace: Path, run: Runner
) -> None:
    box = make(driver, run, run_id, image, workspace, cache_key=KEY)
    assert box.cache_dir is not None
    box.disk_poll_s = 0.2
    box.disk_limit_bytes = 3 * MIB
    fill(workspace, 1 * MIB)
    fill(box.cache_dir, 1 * MIB)
    ok = run(box.exec(["sleep", "1"], network=NONE, timeout_s=30))
    assert not ok.killed_disk_limit, "2 MiB in all is under 3 MiB"

    fill(box.cache_dir, 2 * MIB, "more")  # the cache alone is 3 MiB; the total 4 MiB
    res = run(box.exec(["sleep", "30"], network=NONE, timeout_s=60))
    assert res.killed_disk_limit
    assert res.exit_code == -1
    assert "workspace and package cache" in res.stderr_tail


def test_neither_the_workspace_nor_the_cache_alone_has_to_pass_the_limit(
    driver: DockerDriver, run_id: RunId, image: str, workspace: Path, run: Runner
) -> None:
    box = make(driver, run, run_id, image, workspace, cache_key=KEY)
    assert box.cache_dir is not None
    box.disk_poll_s = 0.2
    box.disk_limit_bytes = 3 * MIB
    fill(workspace, 2 * MIB)
    fill(box.cache_dir, 2 * MIB)  # each is under 3 MiB; together they are not
    res = run(box.exec(["sleep", "30"], network=NONE, timeout_s=60))
    assert res.killed_disk_limit


def test_a_cache_already_over_the_limit_refuses_the_sandbox(
    driver: DockerDriver,
    client: docker.DockerClient,
    cache_root: Path,
    run_id: RunId,
    image: str,
    workspace: Path,
    run: Runner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A command would be killed at its first poll, so say so up front. (The limit is 1 GiB;
    the test shrinks the GiB the driver multiplies by, rather than write a real gigabyte.)"""
    monkeypatch.setattr("mpilot.sandbox.driver.GIB", MIB)
    fill(CacheStore(cache_root).run_dir(KEY), 2 * MIB)
    with pytest.raises(RuntimeError, match="already over the disk limit"):
        run(driver.create(make_spec(run_id, image, workspace, cache_key=KEY, disk_gb=1)))
    assert not [
        c for c in client.containers.list(all=True) if c.labels.get("mpilot.run_id") == str(run_id)
    ]


# --- the read-write side, with a stand-in for the install container (P2-09) -------------------


def test_the_install_mount_can_write_the_cache_and_the_work_container_then_reads_it(
    driver: DockerDriver,
    client: docker.DockerClient,
    cache_root: Path,
    run_id: RunId,
    image: str,
    workspace: Path,
    run: Runner,
) -> None:
    """STAND-IN: P2-09 builds the real install container. This one is a bare container with the
    mount `cache_volume(..., INSTALL)` gives it, which is the only part P2-07b owns."""
    path = CacheStore(cache_root).run_dir(KEY)
    container = client.containers.run(
        image,
        ["sh", "-c", "echo downloaded > /cache/npm-package.tgz"],
        volumes=cache_volume(path, CacheRole.INSTALL),
        user=f"{os.getuid()}:{os.getgid()}",
        network_mode="none",
        read_only=True,
        cap_drop=["ALL"],
        detach=True,
        labels={"mpilot.run_id": str(run_id)},
    )
    try:
        assert container.wait()["StatusCode"] == 0, container.logs().decode()
    finally:
        container.remove(force=True)
    assert (path / "npm-package.tgz").read_text() == "downloaded\n"

    box = make(driver, run, run_id, image, workspace, cache_key=KEY)
    assert sh(run, box, "cat /cache/npm-package.tgz")[1] == "downloaded\n"
    assert sh(run, box, "echo more >> /cache/npm-package.tgz")[0] != 0


# --- real package managers on the real images ----------------------------------------------


@pytest.fixture(scope="module")
def built() -> None:
    for spec in im.image_specs():
        if spec.key in ("node:22", "python:3.12"):
            im.build_image(spec)


def tool_dirs(client: docker.DockerClient, tag: str, script: str, cache: Path) -> dict[str, str]:
    """Run `script` in an image with the cache mounted read-write (so the tools do not complain)
    and the driver's cache environment; return its `name=value` lines."""
    out = client.containers.run(
        tag,
        ["sh", "-c", script],
        environment={**CACHE_ENV, "HOME": "/tmp"},
        volumes=cache_volume(cache, CacheRole.INSTALL),
        user=f"{os.getuid()}:{os.getgid()}",
        network_mode="none",
        read_only=True,
        tmpfs={"/tmp": "size=16m,mode=1777"},
        cap_drop=["ALL"],
        remove=True,
        stderr=False,
    )
    return dict(line.split("=", 1) for line in bytes(out).decode().splitlines() if "=" in line)


def test_every_package_manager_in_the_images_honours_its_cache_variable(
    built: None, client: docker.DockerClient, cache_root: Path
) -> None:
    path = CacheStore(cache_root).run_dir(KEY)
    node = tool_dirs(
        client,
        "mpilot/node:22",
        "echo npm=$(npm config get cache); echo pnpm=$(pnpm store path); "
        "echo yarn=$(yarn cache dir)",
        path,
    )
    assert node["npm"] == "/cache/npm"
    assert node["pnpm"].startswith("/cache/pnpm-store")
    assert node["yarn"].startswith("/cache/yarn")
    py = tool_dirs(
        client,
        "mpilot/python:3.12",
        "echo pip=$(pip cache dir); echo uv=$(uv cache dir); "
        "echo poetry=$(poetry config cache-dir); echo pdm=$(pdm config cache_dir)",
        path,
    )
    assert py == {
        "pip": "/cache/pip",
        "uv": "/cache/uv",
        "poetry": "/cache/poetry",
        "pdm": "/cache/pdm",
    }


@pytest.mark.parametrize("key", ["node:22", "python:3.12"])
def test_the_real_work_container_runs_the_projects_tests_with_a_read_only_cache(
    key: str,
    built: None,
    client: docker.DockerClient,
    cache_root: Path,
    run_id: RunId,
    workspace: Path,
    run: Runner,
) -> None:
    ref = im.resolve_image_ref(client, f"mpilot/{key}")
    driver = DockerDriver(client, cache_root=cache_root)
    box = make(driver, run, run_id, ref, workspace, cache_key=KEY)
    if key.startswith("node"):
        (workspace / "package.json").write_text(
            '{"name":"x","version":"1.0.0","scripts":{"test":"echo ok-test"}}'
        )
        scripts = ["npm test", "pnpm test", "yarn test"]
    else:
        (workspace / "test_x.py").write_text(
            "import unittest\nclass T(unittest.TestCase):\n    def test(self): pass\n"
        )
        scripts = [
            "python -m unittest discover -q",
            "uv --version",
            "poetry --version",
            "pdm --version",
        ]
    for script in scripts:
        code, out, err = sh(run, box, script, timeout_s=120)
        assert code == 0, (script, out, err)
    assert os.listdir(box.cache_dir or ".") == [], "the work container wrote nothing to the cache"
