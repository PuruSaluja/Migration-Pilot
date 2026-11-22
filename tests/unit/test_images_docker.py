"""P2-07 (SPEC §9.3, NFR-5): every image builds, runs offline as a non-root user, carries the
tools the lock records, and works as a sandbox. Real Docker; layers are cached after the first
build, so a rerun is fast (the first one builds all eight images, a few minutes).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import docker
import pytest
from docker_helpers import NONE, client, make_spec, run_id  # noqa: F401  (fixtures by name)

from mpilot.core.types import RunId
from mpilot.sandbox import images as im
from mpilot.sandbox.driver import DockerDriver
from mpilot.sandbox.images import ImageSpec

pytestmark = pytest.mark.docker

SPECS = im.image_specs()
SANDBOX = [s for s in SPECS if s.ecosystem.sandbox]  # the proxy image is tested in test_network*
LOCK = im.read_lock(im.default_images_dir() / im.LOCK_FILENAME)


def _ids(specs: list[ImageSpec] = SPECS) -> list[str]:
    return [s.key for s in specs]


@pytest.fixture(scope="module")
def built() -> dict[str, ImageSpec]:
    for spec in SPECS:
        im.build_image(spec)
    return {s.key: s for s in SPECS}


def in_image(
    client: docker.DockerClient, tag: str, argv: list[str], *, user: str | None = None
) -> tuple[int, str, str]:
    """Run one command offline in a throwaway container; return (exit code, stdout, stderr)."""
    c = client.containers.create(
        tag,
        argv,
        network_mode="none",
        cap_drop=["ALL"],
        security_opt=["no-new-privileges"],
        read_only=True,
        tmpfs={"/tmp": "size=16m,mode=1777"},
        user=user,
    )
    try:
        c.start()
        code = int(c.wait()["StatusCode"])
        out = c.logs(stdout=True, stderr=False).decode("utf-8", "replace")
        err = c.logs(stdout=False, stderr=True).decode("utf-8", "replace")
        return code, out, err
    finally:
        c.remove(force=True, v=True)


@pytest.mark.parametrize("spec", SPECS, ids=_ids())
def test_every_image_builds_and_carries_the_locked_tools(
    spec: ImageSpec,
    built: dict[str, ImageSpec],
    client: docker.DockerClient,
) -> None:
    tools = im.probe_tools(client, spec)
    assert tools == LOCK.images[spec.key].tools


@pytest.mark.parametrize("spec", SANDBOX, ids=_ids(SANDBOX))
def test_rg_and_git_run_offline_as_the_sandbox_user_with_a_quiet_stderr(
    spec: ImageSpec,
    built: dict[str, ImageSpec],
    client: docker.DockerClient,
) -> None:
    for argv, expect in (
        (["id", "-u"], "1000"),
        (["rg", "--version"], "ripgrep"),
        (["git", "--version"], "git version"),
        (["tree-sitter", "--version"], "tree-sitter"),
    ):
        code, out, err = in_image(client, spec.tag, argv)
        assert code == 0, (spec.key, argv, err)
        assert expect in out, (spec.key, argv, out)
        # tree-sitter v0.27 printed "weak version GLIBC_2.39 not found" here (bookworm is 2.36)
        assert err == "", (spec.key, argv, err)


@pytest.mark.parametrize("spec", SANDBOX, ids=_ids(SANDBOX))
def test_rg_and_git_do_real_work(
    spec: ImageSpec,
    built: dict[str, ImageSpec],
    client: docker.DockerClient,
) -> None:
    script = (
        "set -e; cd /tmp; git init -q repo; cd repo; echo needle > a.txt; "
        "git -c user.name=t -c user.email=t@example.invalid add a.txt; "
        "git -c user.name=t -c user.email=t@example.invalid commit -q -m x; "
        "rg -c needle a.txt; git log --oneline | wc -l"
    )
    code, out, err = in_image(client, spec.tag, ["sh", "-c", script])
    assert code == 0, err
    assert out.split() == ["1", "1"]


@pytest.mark.parametrize("spec", SANDBOX, ids=_ids(SANDBOX))
def test_the_image_does_not_run_as_root_even_without_a_user_flag(
    spec: ImageSpec,
    built: dict[str, ImageSpec],
    client: docker.DockerClient,
) -> None:
    attrs = client.images.get(spec.tag).attrs
    assert attrs["Config"]["User"] == "sandbox"
    assert attrs["Config"]["WorkingDir"] == "/workspace"
    code, out, _ = in_image(client, spec.tag, ["sh", "-c", "id -un; id -u; id -g"])
    assert (code, out.split()) == (0, ["sandbox", "1000", "1000"])


@pytest.mark.parametrize("spec", SANDBOX, ids=_ids(SANDBOX))
def test_curl_and_the_apt_lists_are_gone(
    spec: ImageSpec,
    built: dict[str, ImageSpec],
    client: docker.DockerClient,
) -> None:
    code, out, _ = in_image(
        client,
        spec.tag,
        ["sh", "-c", "command -v curl || echo no-curl; find /var/lib/apt/lists -type f | wc -l"],
    )
    assert code == 0
    assert out.split() == ["no-curl", "0"], "no apt list files are left in the image"


def test_node_images_run_the_requested_major(
    built: dict[str, ImageSpec], client: docker.DockerClient
) -> None:
    for spec in SPECS:
        if spec.ecosystem.name == "node":
            _, out, _ = in_image(client, spec.tag, ["node", "--version"])
            assert out.startswith(f"v{spec.version}."), (spec.key, out)


def test_python_images_run_the_requested_version(
    built: dict[str, ImageSpec], client: docker.DockerClient
) -> None:
    for spec in SPECS:
        if spec.ecosystem.name == "python":
            _, out, _ = in_image(client, spec.tag, ["python", "--version"])
            assert out.startswith(f"Python {spec.version}."), (spec.key, out)


def test_poetry_and_pdm_do_not_share_the_repos_python_packages(
    built: dict[str, ImageSpec],
    client: docker.DockerClient,
) -> None:
    """Their dependencies live in /opt/tools venvs, so the repo's environment starts clean."""
    code, out, err = in_image(client, "mpilot/python:3.12", ["pip", "list", "--format=freeze"])
    assert code == 0, err
    names = {line.split("==")[0].lower() for line in out.splitlines()}
    assert not names & {"poetry", "pdm", "poetry-core", "virtualenv", "cleo", "installer"}


def test_pnpm_does_not_try_to_download_another_version_offline(
    built: dict[str, ImageSpec],
    client: docker.DockerClient,
) -> None:
    """A repo whose package.json names another pnpm must not make `pnpm` fail offline."""
    script = (
        "set -e; mkdir /tmp/p; cd /tmp/p; "
        'echo \'{"name":"x","version":"1.0.0","packageManager":"pnpm@8.0.0"}\' > package.json; '
        "pnpm --version"
    )
    code, out, err = in_image(client, "mpilot/node:22", ["sh", "-c", script])
    assert code == 0, err
    assert out.strip() == "10.34.9"


# --- the lock against the images, and the refs the driver needs ---------------------------


def test_verify_images_agrees_with_the_committed_lock(
    built: dict[str, ImageSpec],
    client: docker.DockerClient,
) -> None:
    assert im.verify_images(client, SPECS, LOCK) == []


def test_verify_images_reports_a_version_that_differs_from_the_lock(
    built: dict[str, ImageSpec],
    client: docker.DockerClient,
) -> None:
    entry = LOCK.images["node:22"]
    tools = {**entry.tools, "ripgrep": "ripgrep 0.0.1"}
    lock = LOCK.model_copy(
        update={"images": {**LOCK.images, "node:22": entry.model_copy(update={"tools": tools})}}
    )
    problems = im.verify_images(client, SPECS, lock)
    assert len(problems) == 1
    assert all(word in problems[0] for word in ("node:22", "ripgrep", "0.0.1"))


def test_verify_images_reports_an_image_that_is_not_built(client: docker.DockerClient) -> None:
    spec = SPECS[0]
    ghost = ImageSpec(
        key=spec.key,
        ecosystem=spec.ecosystem,
        version=spec.version,
        tag="mpilot/never-built:1",
        context=spec.context,
        base=spec.base,
        dockerfile_sha256=spec.dockerfile_sha256,
    )
    problems = im.verify_images(
        client, [ghost], LOCK.model_copy(update={"images": {ghost.key: LOCK.images[ghost.key]}})
    )
    assert any("probe failed" in p or "not built" in p for p in problems), problems


@pytest.mark.parametrize("spec", SPECS, ids=_ids())
def test_every_image_has_a_digest_ref_the_driver_accepts(
    spec: ImageSpec,
    built: dict[str, ImageSpec],
    client: docker.DockerClient,
) -> None:
    ref = im.resolve_image_ref(client, spec.tag)
    assert ref.startswith(f"mpilot/{spec.ecosystem.name}@sha256:")
    assert client.images.get(ref).id == client.images.get(spec.tag).id


def test_a_failing_build_raises_with_the_end_of_the_log(tmp_path: Path) -> None:
    context = tmp_path / "ctx"
    context.mkdir()
    (context / "Dockerfile").write_text("FROM busybox:1.36\nRUN echo marker-before && exit 7\n")
    spec = ImageSpec(
        key="node:20",
        ecosystem=SPECS[0].ecosystem,
        version="20",
        tag="mpilot/test-failing:1",
        context=context,
        base=SPECS[0].base,
        dockerfile_sha256="0" * 64,
    )
    with pytest.raises(im.ImageError) as raised:
        im.build_image(spec, no_cache=True)
    message = str(raised.value)
    assert "node:20" in message
    assert "exit code: 7" in message, "the end of docker's log is in the error"
    assert "RUN echo marker-before" in message


# --- as a real sandbox --------------------------------------------------------------------


@pytest.mark.parametrize("key", ["node:22", "python:3.12"])
def test_an_image_works_as_a_hardened_work_container(
    key: str,
    client: docker.DockerClient,
    run_id: RunId,
    tmp_path: Path,
    built: dict[str, ImageSpec],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ref = im.resolve_image_ref(client, built[key].tag)

    async def scenario() -> None:
        box = await DockerDriver(client).create(make_spec(run_id, ref, workspace))
        try:
            for argv in (["rg", "--version"], ["git", "--version"], ["tree-sitter", "--version"]):
                res = await box.exec(argv, network=NONE, timeout_s=60)
                assert res.exit_code == 0, (argv, res.stderr_tail)
                assert res.stderr_tail == "", (argv, res.stderr_tail)
            script = "echo hello > /workspace/f.txt && rg -c hello /workspace/f.txt"
            res = await box.exec(["sh", "-c", script], network=NONE, timeout_s=60)
            assert res.exit_code == 0, res
            assert res.stdout_tail.strip() == "1", res
            res = await box.exec(["sh", "-c", "touch /usr/x"], network=NONE, timeout_s=60)
            assert res.exit_code != 0, "the root filesystem is read-only"
        finally:
            await box.destroy()

    asyncio.run(scenario())
