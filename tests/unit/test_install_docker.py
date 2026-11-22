"""P2-09 (SPEC §9.3 Network, NFR-6, NFR-9, A7): the install container on a per-run internal
network next to the work container with no network, through `Sandbox.exec(network=...)`.

The offline tests use a fake resolver (public-looking answers that no denied tunnel ever uses).
The tests that reach real registries are marked `network` (MPILOT_NETWORK_TESTS=1).
"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import docker
import pytest
from docker.errors import NotFound
from docker_helpers import NONE, Runner, client, make_spec, run, run_id  # noqa: F401

from mpilot.config import SandboxSettings
from mpilot.core import spawn
from mpilot.core.types import NetworkMode, RunId, new_run_id
from mpilot.sandbox import images as im
from mpilot.sandbox.cache import cache_key
from mpilot.sandbox.driver import LABEL_RUN, DockerDriver, DockerSandbox
from mpilot.sandbox.labels import LABEL_ROLE
from mpilot.sandbox.network import (
    EgressProxyManager,
    EgressSetup,
    NetworkSetupError,
    system_resolve,
)

pytestmark = pytest.mark.docker

REGISTRY = NetworkMode.REGISTRY
FAKE_ADDRESS = "104.16.0.35"
KEY = cache_key("python", {"requirements.txt": b"six==1.16.0\n"})

#: Runs inside the sandbox (python images). argv[1] is a JSON list of probes; prints one JSON
#: result per probe.
PROBE = r"""
import json, os, socket, sys, urllib.request
results = []
for probe in json.loads(sys.argv[1]):
    kind = probe["kind"]
    try:
        if kind == "dns":
            results.append({"dns": socket.getaddrinfo(probe["host"], 443)[0][4][0]})
        elif kind == "tcp":
            socket.create_connection((probe["ip"], probe["port"]), timeout=4).close()
            results.append({"tcp": "connected"})
        elif kind == "env":
            results.append({k: os.environ.get(k) for k in probe["names"]})
        elif kind == "connect":
            proxy = os.environ["HTTPS_PROXY"].split("//", 1)[1]
            host, port = proxy.rsplit(":", 1)
            s = socket.create_connection((host, int(port)), timeout=10)
            s.sendall(("CONNECT %s HTTP/1.1\r\n\r\n" % probe["target"]).encode())
            results.append({"status": s.makefile("rb").readline().decode().strip()})
        elif kind == "https":
            with urllib.request.urlopen(probe["url"], timeout=30) as resp:
                results.append({"status": resp.status, "bytes": len(resp.read())})
        elif kind == "write":
            with open(probe["path"], "w") as f:
                f.write("x")
            results.append({"write": "ok"})
    except Exception as exc:
        results.append({"error": "%s: %s" % (type(exc).__name__, exc)})
print(json.dumps(results))
"""


def host_ipv4_addresses() -> list[str]:
    """Every IPv4 address of this host (all interfaces), through the process helper."""
    ip = shutil.which("ip")
    assert ip is not None
    res = spawn.run([ip, "-4", "-o", "addr"], env={}, timeout_s=10)
    out = []
    for line in res.stdout.decode().splitlines():
        parts = line.split()
        out.append(parts[parts.index("inet") + 1].split("/")[0])
    return [a for a in out if not a.startswith("127.")]


@pytest.fixture(scope="module")
def refs() -> dict[str, str]:
    c = docker.from_env(timeout=120)
    try:
        out = {}
        for spec in im.image_specs():
            if spec.key in ("proxy:1", "python:3.12", "node:22"):
                im.build_image(spec)
                out[spec.key] = im.resolve_image_ref(c, spec.tag)
        return out
    finally:
        c.close()


def fake_resolve(host: str) -> list[str]:
    return [FAKE_ADDRESS]


def driver_for(
    client: docker.DockerClient,
    refs: dict[str, str],
    tmp_path: Path,
    *,
    resolve: Any = fake_resolve,
) -> DockerDriver:
    egress = EgressSetup(
        manager=EgressProxyManager(client, resolve=resolve),
        settings=SandboxSettings(),
        proxy_image=refs["proxy:1"],
    )
    return DockerDriver(client, cache_root=tmp_path / "cache", egress=egress)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "workspace"
    path.mkdir()
    return path


@pytest.fixture
def box(
    client: docker.DockerClient,
    refs: dict[str, str],
    tmp_path: Path,
    run_id: RunId,
    workspace: Path,
    run: Runner,
) -> Iterator[DockerSandbox]:
    driver = driver_for(client, refs, tmp_path)
    sandbox = run(driver.create(make_spec(run_id, refs["python:3.12"], workspace, cache_key=KEY)))
    assert isinstance(sandbox, DockerSandbox)
    yield sandbox
    run(sandbox.destroy())


def probe(
    run: Runner, box: DockerSandbox, network: NetworkMode, *probes: dict[str, Any]
) -> list[dict[str, Any]]:
    res = run(
        box.exec(["python", "-c", PROBE, json.dumps(list(probes))], network=network, timeout_s=120)
    )
    assert res.exit_code == 0, res
    return list(json.loads(res.stdout_tail.strip().splitlines()[-1]))


def of_run(client: docker.DockerClient, rid: RunId, role: str | None = None) -> list[Any]:
    labels = [f"{LABEL_RUN}={rid}"] + ([f"{LABEL_ROLE}={role}"] if role else [])
    return client.containers.list(all=True, filters={"label": labels})


def networks_of(client: docker.DockerClient, rid: RunId) -> list[Any]:
    return client.networks.list(filters={"label": f"{LABEL_RUN}={rid}"})


def unreachable_everywhere(results: list[dict[str, Any]]) -> bool:
    return all("error" in r for r in results)


# --- the work container --------------------------------------------------------------------------


def test_the_work_container_reaches_nothing(box: DockerSandbox, run: Runner) -> None:
    """Registry, 1.1.1.1, host gateway and DNS all fail (network_mode none)."""
    targets = [{"kind": "dns", "host": "registry.npmjs.org"}]
    targets += [{"kind": "tcp", "ip": ip, "port": 443} for ip in ("104.16.24.35", "1.1.1.1")]
    targets += [
        {"kind": "tcp", "ip": ip, "port": p}
        for ip in ["172.17.0.1", *host_ipv4_addresses()]
        for p in (22, 53, 443)
    ]
    results = probe(run, box, NONE, *targets)
    assert unreachable_everywhere(results), [r for r in results if "error" not in r]
    assert box.last_egress is None, "no proxy was involved"


# --- the install container -----------------------------------------------------------------------


def test_an_install_runs_in_a_fresh_container_on_the_run_network_and_leaves_nothing(
    client: docker.DockerClient, box: DockerSandbox, run_id: RunId, run: Runner
) -> None:
    assert networks_of(client, run_id) == []
    [env] = probe(
        run,
        box,
        REGISTRY,
        {
            "kind": "env",
            "names": ["HTTPS_PROXY", "npm_config_https_proxy", "PIP_CACHE_DIR", "NO_PROXY"],
        },
    )
    record = box.last_egress
    assert record is not None
    assert env["HTTPS_PROXY"] == record.proxy.url
    assert env["npm_config_https_proxy"] == record.proxy.url
    assert env["PIP_CACHE_DIR"] == "/cache/pip"
    assert env["NO_PROXY"] == ""
    assert not record.log_truncated
    # gone: the install container and the proxy; kept until destroy: the run's network
    assert of_run(client, run_id, "install") == []
    assert of_run(client, run_id, "proxy") == []
    [net] = networks_of(client, run_id)
    assert net.attrs["Internal"] is True
    assert net.attrs["Options"]["com.docker.network.bridge.gateway_mode_ipv4"] == "isolated"
    assert net.attrs["Options"]["com.docker.network.bridge.gateway_mode_ipv6"] == "isolated"
    assert record.proxy.internal_network == net.name


def test_the_install_container_is_hardened_like_the_work_container(
    client: docker.DockerClient, box: DockerSandbox, run: Runner
) -> None:
    seen: dict[str, Any] = {}

    async def scenario() -> None:
        task = asyncio.ensure_future(box.exec(["sleep", "5"], network=REGISTRY, timeout_s=60))
        for _ in range(200):
            found = of_run(client, box._run_id, "install")
            if found:
                seen["install"] = client.api.inspect_container(found[0].id)
                break
            await asyncio.sleep(0.05)
        await task

    run(scenario())
    attrs = seen["install"]
    work = client.api.inspect_container(box.container_id)
    host = attrs["HostConfig"]
    assert host["Runtime"] == work["HostConfig"]["Runtime"] == "runsc"
    for key in ("ReadonlyRootfs", "CapDrop", "SecurityOpt", "PidsLimit", "Memory", "NanoCpus"):
        assert host[key] == work["HostConfig"][key], key
    assert attrs["Config"]["User"] == work["Config"]["User"]
    assert attrs["Config"]["Image"] == work["Config"]["Image"]
    binds = {m["Destination"]: m for m in attrs["Mounts"] if m["Type"] == "bind"}
    assert set(binds) == {"/workspace", "/cache"}
    assert binds["/cache"]["RW"] is True, "only the install container writes the cache"
    assert next(m for m in work["Mounts"] if m["Destination"] == "/cache")["RW"] is False
    assert list(attrs["NetworkSettings"]["Networks"]) == [box.last_egress.proxy.internal_network]  # type: ignore[union-attr]


def test_others_are_denied_dns_fails_and_nothing_but_the_proxy_answers(
    box: DockerSandbox, run: Runner
) -> None:
    targets: list[dict[str, Any]] = [
        {"kind": "connect", "target": "example.com:443"},
        {"kind": "connect", "target": "1.1.1.1:443"},
        {"kind": "dns", "host": "registry.npmjs.org"},  # Docker DNS lookups fail
        {"kind": "tcp", "ip": "1.1.1.1", "port": 443},  # no way out but the proxy
        {"kind": "tcp", "ip": "104.16.24.35", "port": 443},
    ]
    targets += [
        {"kind": "tcp", "ip": ip, "port": p}
        for ip in ["172.17.0.1", *host_ipv4_addresses()]
        for p in (22, 53, 443)
    ]
    results = probe(run, box, REGISTRY, *targets)
    assert results[0]["status"].startswith("HTTP/1.1 403"), results[0]
    assert results[1]["status"].startswith("HTTP/1.1 403"), results[1]
    assert unreachable_everywhere(results[2:]), [r for r in results[2:] if "error" not in r]
    record = box.last_egress
    assert record is not None
    assert [d.reason for d in record.denials] == ["host_not_allowlisted", "ip_literal"]
    assert [e for e in record.events if e.event == "allow"] == []


def test_the_install_container_writes_the_workspace_and_the_cache_the_work_container_only_reads(
    box: DockerSandbox, workspace: Path, run: Runner
) -> None:
    results = probe(
        run,
        box,
        REGISTRY,
        {"kind": "write", "path": "/workspace/from-install.txt"},
        {"kind": "write", "path": "/cache/from-install.txt"},
    )
    assert results == [{"write": "ok"}, {"write": "ok"}]
    assert (workspace / "from-install.txt").read_text() == "x"
    assert box.cache_dir is not None
    assert (box.cache_dir / "from-install.txt").read_text() == "x"
    work = probe(run, box, NONE, {"kind": "write", "path": "/cache/from-work.txt"})
    assert "error" in work[0], "the work container cannot write the cache"


def test_frozen_refuses_while_an_install_runs(box: DockerSandbox, run: Runner) -> None:
    async def scenario() -> None:
        task = asyncio.ensure_future(box.exec(["sleep", "3"], network=REGISTRY, timeout_s=60))
        await asyncio.sleep(1.5)
        with pytest.raises(RuntimeError, match="command is running"):
            async with box.frozen():
                pass
        await task

    run(scenario())


def test_an_install_that_times_out_is_killed_and_cleaned_up(
    client: docker.DockerClient, box: DockerSandbox, run_id: RunId, run: Runner
) -> None:
    res = run(box.exec(["sleep", "100"], network=REGISTRY, timeout_s=2))
    assert res.timed_out
    assert of_run(client, run_id, "install") == []
    assert of_run(client, run_id, "proxy") == []


def test_a_cancelled_install_still_removes_its_containers(
    client: docker.DockerClient, box: DockerSandbox, run_id: RunId, run: Runner
) -> None:
    async def scenario() -> None:
        task = asyncio.ensure_future(box.exec(["sleep", "30"], network=REGISTRY, timeout_s=60))
        for _ in range(200):
            if of_run(client, run_id, "install"):
                break
            await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await box._drain_pending()

    run(scenario())
    assert of_run(client, run_id, "install") == []
    assert of_run(client, run_id, "proxy") == []


def test_a_stranger_on_the_run_network_stops_the_next_install(
    client: docker.DockerClient,
    box: DockerSandbox,
    run_id: RunId,
    refs: dict[str, str],
    run: Runner,
) -> None:
    probe(run, box, REGISTRY, {"kind": "env", "names": []})  # creates the network
    [net] = networks_of(client, run_id)
    stranger = client.containers.run(
        refs["python:3.12"],
        ["sleep", "60"],
        network=net.name,
        detach=True,
        labels={LABEL_RUN: "someone-else"},
    )
    try:
        with pytest.raises(NetworkSetupError, match="already has members"):
            run(box.exec(["true"], network=REGISTRY, timeout_s=30))
        assert of_run(client, run_id, "install") == []
        assert of_run(client, run_id, "proxy") == []
    finally:
        stranger.remove(force=True)


# --- runs and networks ---------------------------------------------------------------------------


def test_two_runs_get_separate_networks_and_cannot_reach_each_others_proxy(
    client: docker.DockerClient, refs: dict[str, str], tmp_path: Path, run: Runner
) -> None:
    rids = [new_run_id(), new_run_id()]
    boxes: list[DockerSandbox] = []
    try:
        for i, rid in enumerate(rids):
            ws = tmp_path / f"w{i}"
            ws.mkdir()
            (tmp_path / f"d{i}").mkdir()
            sandbox = run(
                driver_for(client, refs, tmp_path / f"d{i}").create(
                    make_spec(rid, refs["python:3.12"], ws, cache_key=KEY)
                )
            )
            assert isinstance(sandbox, DockerSandbox)
            boxes.append(sandbox)

        async def scenario() -> list[dict[str, Any]]:
            # run A holds its proxy open with a long install; run B tries to use it
            hold = asyncio.ensure_future(
                boxes[0].exec(["sleep", "8"], network=REGISTRY, timeout_s=60)
            )
            proxy_ip = ""
            for _ in range(400):
                found = of_run(client, rids[0], "proxy")
                if found:
                    nets = client.api.inspect_container(found[0].id)["NetworkSettings"]["Networks"]
                    proxy_ip = next(
                        v["IPAddress"] for k, v in nets.items() if k.startswith("mpilot-int-")
                    )
                    if proxy_ip:
                        break
                await asyncio.sleep(0.05)
            assert proxy_ip
            res = await boxes[1].exec(
                [
                    "python",
                    "-c",
                    PROBE,
                    json.dumps([{"kind": "tcp", "ip": proxy_ip, "port": 3128}]),
                ],
                network=REGISTRY,
                timeout_s=60,
            )
            await hold
            return list(json.loads(res.stdout_tail.strip().splitlines()[-1]))

        result = run(scenario())
        assert "error" in result[0], "run B reached run A's proxy"
        nets = [networks_of(client, rid) for rid in rids]
        assert all(len(n) == 1 for n in nets)
        assert nets[0][0].name != nets[1][0].name
        subnets = [n[0].attrs["IPAM"]["Config"][0]["Subnet"] for n in nets]
        assert subnets[0] != subnets[1]
    finally:
        for sandbox in boxes:
            run(sandbox.destroy())
        for rid in rids:
            assert networks_of(client, rid) == []


def test_destroy_removes_the_run_network(
    client: docker.DockerClient,
    refs: dict[str, str],
    tmp_path: Path,
    run_id: RunId,
    workspace: Path,
    run: Runner,
) -> None:
    sandbox = run(
        driver_for(client, refs, tmp_path).create(
            make_spec(run_id, refs["python:3.12"], workspace, cache_key=KEY)
        )
    )
    assert isinstance(sandbox, DockerSandbox)
    probe(run, sandbox, REGISTRY, {"kind": "env", "names": []})
    [net] = networks_of(client, run_id)
    run(sandbox.destroy())
    with pytest.raises(NotFound):
        client.api.inspect_network(net.id)
    assert of_run(client, run_id) == []


def test_the_network_is_removed_after_a_failed_install_too(
    client: docker.DockerClient,
    refs: dict[str, str],
    tmp_path: Path,
    run_id: RunId,
    workspace: Path,
    run: Runner,
) -> None:
    def no_dns(host: str) -> list[str]:
        raise OSError("resolver down")

    sandbox = run(
        driver_for(client, refs, tmp_path, resolve=no_dns).create(
            make_spec(run_id, refs["python:3.12"], workspace, cache_key=KEY)
        )
    )
    assert isinstance(sandbox, DockerSandbox)
    with pytest.raises(NetworkSetupError, match="no allowlisted host resolved"):
        run(sandbox.exec(["true"], network=REGISTRY, timeout_s=30))
    assert of_run(client, run_id, "install") == []
    assert of_run(client, run_id, "proxy") == []
    run(sandbox.destroy())
    assert networks_of(client, run_id) == []


def test_reap_removes_a_crashed_runs_network_and_containers(
    client: docker.DockerClient,
    refs: dict[str, str],
    tmp_path: Path,
    run_id: RunId,
    workspace: Path,
    run: Runner,
) -> None:
    driver = driver_for(client, refs, tmp_path)
    sandbox = run(driver.create(make_spec(run_id, refs["python:3.12"], workspace, cache_key=KEY)))
    assert isinstance(sandbox, DockerSandbox)
    probe(run, sandbox, REGISTRY, {"kind": "env", "names": []})
    assert networks_of(client, run_id)
    # the orchestrator dies here: no destroy()
    assert run(driver.reap(run_id)) >= 2  # the work container and the network
    assert of_run(client, run_id) == []
    assert networks_of(client, run_id) == []


# --- real registries (network) -------------------------------------------------------------------


@pytest.mark.network
def test_the_npm_registry_is_reachable_from_an_install_container_and_nothing_else(
    client: docker.DockerClient,
    refs: dict[str, str],
    tmp_path: Path,
    run_id: RunId,
    workspace: Path,
    run: Runner,
) -> None:
    sandbox = run(
        driver_for(client, refs, tmp_path, resolve=system_resolve).create(
            make_spec(run_id, refs["python:3.12"], workspace, cache_key=KEY)
        )
    )
    assert isinstance(sandbox, DockerSandbox)
    try:
        ok, denied = probe(
            run,
            sandbox,
            REGISTRY,
            {"kind": "https", "url": "https://registry.npmjs.org/-/ping"},
            {"kind": "https", "url": "https://example.com/"},
        )
        assert ok["status"] == 200, ok
        assert "error" in denied, denied
        record = sandbox.last_egress
        assert record is not None
        assert [e.target for e in record.events if e.event == "allow"] == ["registry.npmjs.org:443"]
        assert [d.target for d in record.denials] == ["example.com:443"]
        # the same request from the work container fails
        [work] = probe(
            run, sandbox, NONE, {"kind": "https", "url": "https://registry.npmjs.org/-/ping"}
        )
        assert "error" in work
    finally:
        run(sandbox.destroy())


@pytest.mark.network
def test_real_package_managers_install_through_the_proxy_into_the_cache(
    client: docker.DockerClient, refs: dict[str, str], tmp_path: Path, run_id: RunId, run: Runner
) -> None:
    for image, argv, where in (
        (
            refs["python:3.12"],
            ["pip", "download", "--no-deps", "-d", "/workspace/dl", "six==1.16.0"],
            "pip",
        ),
        (refs["node:22"], ["npm", "view", "left-pad", "version"], "npm"),
    ):
        ws = tmp_path / f"ws-{where}"
        ws.mkdir()
        (tmp_path / where).mkdir()
        rid = new_run_id()
        sandbox = run(
            driver_for(client, refs, tmp_path / where, resolve=system_resolve).create(
                make_spec(rid, image, ws, cache_key=KEY)
            )
        )
        assert isinstance(sandbox, DockerSandbox)
        try:
            res = run(sandbox.exec(argv, network=REGISTRY, timeout_s=300))
            assert res.exit_code == 0, (where, res.stdout_tail, res.stderr_tail)
            assert sandbox.cache_dir is not None
            assert any((sandbox.cache_dir / where).rglob("*")), f"{where} cached nothing"
            record = sandbox.last_egress
            assert record is not None
            assert record.denials == ()
            assert [e for e in record.events if e.event == "allow"]
        finally:
            run(sandbox.destroy())
            assert networks_of(client, rid) == []


def test_the_member_check_refuses_anything_but_the_expected_containers(
    client: docker.DockerClient, run_id: RunId, refs: dict[str, str]
) -> None:
    from mpilot.sandbox.network import create_internal_network, require_members

    net = create_internal_network(client, run_id)
    require_members(client, net, set())
    joined = client.containers.run(
        refs["python:3.12"],
        ["sleep", "60"],
        network=net,
        detach=True,
        labels={LABEL_RUN: str(run_id)},
    )
    try:
        with pytest.raises(NetworkSetupError, match="unexpected"):
            require_members(client, net, set())
        require_members(client, net, {joined.id})
        with pytest.raises(NetworkSetupError, match="missing"):
            require_members(client, net, {joined.id, "f" * 64})
    finally:
        joined.remove(force=True)


# --- review fixes (P2-09 pass 1) ----------------------------------------------------------------


def test_under_runc_an_install_container_has_no_dns_no_route_out_and_no_host(
    client: docker.DockerClient,
    refs: dict[str, str],
    tmp_path: Path,
    run_id: RunId,
    workspace: Path,
    run: Runner,
) -> None:
    """Install containers fall back to runc with the work container; there Docker's embedded
    DNS is live and the host kernel is shared, so the checks matter most (review B2)."""
    from mpilot.sandbox.base import ContainerRuntime

    sandbox = run(
        driver_for(client, refs, tmp_path).create(
            make_spec(
                run_id, refs["python:3.12"], workspace, cache_key=KEY, prefer=ContainerRuntime.RUNC
            )
        )
    )
    assert isinstance(sandbox, DockerSandbox)
    try:
        seen: dict[str, Any] = {}

        async def scenario() -> list[dict[str, Any]]:
            targets: list[dict[str, Any]] = [
                {"kind": "dns", "host": "registry.npmjs.org"},
                {"kind": "dns", "host": "example.com"},
                {"kind": "tcp", "ip": "1.1.1.1", "port": 443},
                {"kind": "tcp", "ip": "104.16.24.35", "port": 443},
            ]
            targets += [
                {"kind": "tcp", "ip": ip, "port": p}
                for ip in ["172.17.0.1", *host_ipv4_addresses()]
                for p in (22, 53, 443)
            ]
            task = asyncio.ensure_future(
                sandbox.exec(
                    ["python", "-c", PROBE, json.dumps(targets)], network=REGISTRY, timeout_s=180
                )
            )
            for _ in range(400):
                found = of_run(client, run_id, "install")
                if found:
                    seen["runtime"] = client.api.inspect_container(found[0].id)["HostConfig"][
                        "Runtime"
                    ]
                    break
                await asyncio.sleep(0.02)
            res = await task
            return list(json.loads(res.stdout_tail.strip().splitlines()[-1]))

        results = run(scenario())
        assert seen["runtime"] == "runc"
        assert unreachable_everywhere(results), [r for r in results if "error" not in r]
    finally:
        run(sandbox.destroy())


def test_last_egress_never_shows_an_earlier_install(
    client: docker.DockerClient,
    refs: dict[str, str],
    tmp_path: Path,
    run_id: RunId,
    workspace: Path,
    run: Runner,
) -> None:
    state = {"dns": True}

    def flaky(host: str) -> list[str]:
        if not state["dns"]:
            raise OSError("resolver down")
        return [FAKE_ADDRESS]

    sandbox = run(
        driver_for(client, refs, tmp_path, resolve=flaky).create(
            make_spec(run_id, refs["python:3.12"], workspace, cache_key=KEY)
        )
    )
    assert isinstance(sandbox, DockerSandbox)
    try:
        probe(run, sandbox, REGISTRY, {"kind": "env", "names": []})
        assert sandbox.last_egress is not None
        state["dns"] = False
        with pytest.raises(NetworkSetupError):
            run(sandbox.exec(["true"], network=REGISTRY, timeout_s=30))
        assert sandbox.last_egress is None
    finally:
        run(sandbox.destroy())


def test_two_installs_at_once_on_one_sandbox_take_turns(
    client: docker.DockerClient, box: DockerSandbox, run_id: RunId, run: Runner
) -> None:
    async def scenario() -> list[Any]:
        return list(
            await asyncio.gather(
                box.exec(["sh", "-c", "sleep 1; echo a"], network=REGISTRY, timeout_s=60),
                box.exec(["sh", "-c", "sleep 1; echo b"], network=REGISTRY, timeout_s=60),
            )
        )

    first, second = run(scenario())
    assert (first.exit_code, first.stdout_tail.strip()) == (0, "a")
    assert (second.exit_code, second.stdout_tail.strip()) == (0, "b")
    assert of_run(client, run_id, "install") == []
    assert of_run(client, run_id, "proxy") == []


def test_the_install_container_is_held_to_the_sandbox_disk_limit(
    box: DockerSandbox, run: Runner
) -> None:
    box.disk_limit_bytes = 8 * 1024 * 1024  # lowered after create: the install must see it
    box.disk_poll_s = 0.2
    script = "dd if=/dev/urandom of=/workspace/big bs=1M count=64 2>/dev/null; sleep 5"
    res = run(box.exec(["sh", "-c", script], network=REGISTRY, timeout_s=60))
    assert res.killed_disk_limit, res


class _SlowStart(EgressProxyManager):
    """Blocks proxy start until released, so a test can cancel the install exactly there."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def start(self, **kwargs: Any) -> Any:  # type: ignore[override]
        self.entered.set()
        await self.release.wait()
        return await super().start(**kwargs)


def test_a_cancel_while_the_proxy_is_starting_still_removes_it(
    client: docker.DockerClient,
    refs: dict[str, str],
    tmp_path: Path,
    run_id: RunId,
    workspace: Path,
    run: Runner,
) -> None:
    async def scenario() -> None:
        manager = _SlowStart(client, resolve=fake_resolve)
        egress = EgressSetup(
            manager=manager, settings=SandboxSettings(), proxy_image=refs["proxy:1"]
        )
        sandbox = await DockerDriver(client, cache_root=tmp_path / "cache", egress=egress).create(
            make_spec(run_id, refs["python:3.12"], workspace, cache_key=KEY)
        )
        assert isinstance(sandbox, DockerSandbox)
        try:
            task = asyncio.ensure_future(sandbox.exec(["true"], network=REGISTRY, timeout_s=30))
            await manager.entered.wait()
            task.cancel()
            await asyncio.sleep(0.1)
            manager.release.set()  # the proxy now really starts, after the caller is gone
            with pytest.raises(asyncio.CancelledError):
                await task
            await sandbox._drain_pending()
            assert of_run(client, run_id, "proxy") == [], "the late proxy was cleaned up"
            assert of_run(client, run_id, "install") == []
        finally:
            await sandbox.destroy()
        assert networks_of(client, run_id) == []

    run(scenario())


def test_an_install_queued_behind_another_does_not_run_after_destroy(
    client: docker.DockerClient,
    refs: dict[str, str],
    tmp_path: Path,
    run_id: RunId,
    workspace: Path,
    run: Runner,
) -> None:
    """Review pass 2, N1: B waits for the install lock while A runs; the sandbox is destroyed;
    B must then be refused, not start a container on the destroyed sandbox's workspace."""
    from mpilot.sandbox.base import CacheMode

    async def scenario() -> tuple[BaseException | None, BaseException | None, Path | None]:
        driver = driver_for(client, refs, tmp_path)
        sandbox = await driver.create(
            make_spec(
                run_id, refs["python:3.12"], workspace, cache=CacheMode.FRESH, from_commit="a" * 40
            )
        )
        assert isinstance(sandbox, DockerSandbox)
        fresh = sandbox.cache_dir
        a = asyncio.ensure_future(sandbox.exec(["sleep", "30"], network=REGISTRY, timeout_s=60))
        for _ in range(400):
            if of_run(client, run_id, "install"):
                break
            await asyncio.sleep(0.02)
        b = asyncio.ensure_future(
            sandbox.exec(["touch", "/workspace/from-b"], network=REGISTRY, timeout_s=60)
        )
        await asyncio.sleep(0.3)  # B is past exec's entry checks, waiting for the lock
        await sandbox.destroy()
        results = await asyncio.gather(a, b, return_exceptions=True)
        await sandbox._drain_pending()
        err_a = results[0] if isinstance(results[0], BaseException) else None
        err_b = results[1] if isinstance(results[1], BaseException) else None
        return err_a, err_b, fresh

    _, err_b, fresh = run(scenario())
    assert isinstance(err_b, RuntimeError), err_b
    # `destroy` marks the sandbox broken, then destroyed: B is refused at either moment
    assert "destroyed" in str(err_b) or "broken" in str(err_b), err_b
    assert not (workspace / "from-b").exists(), "B never ran"
    assert of_run(client, run_id) == []
    assert networks_of(client, run_id) == []
    assert fresh is not None
    assert not fresh.exists(), "the destroyed sandbox's fresh cache is not recreated"
