"""P2-08b (SPEC §9.3 Network, A7): the egress proxy as a real container between a run's
internal network and the egress bridge, tested from client containers on the internal network.

Every refusal is tested offline: the manager's resolver is a fake that answers public-looking
addresses, and none of these tunnels gets far enough to use them. The one test that reaches a
real registry is marked `network` (run it with MPILOT_NETWORK_TESTS=1).
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
from collections.abc import Iterator
from typing import Any

import docker
import pytest
from docker_helpers import client, run_id  # noqa: F401  (fixtures are used by name)

from mpilot.config import DEFAULT_EGRESS_ALLOWLIST, SandboxSettings
from mpilot.core.types import RunId
from mpilot.sandbox import images as im
from mpilot.sandbox.driver import LABEL_RUN, DockerDriver
from mpilot.sandbox.network import (
    EGRESS_NETWORK,
    EgressProxyManager,
    NetworkSetupError,
    ProxyEvent,
    ProxyHandle,
    create_internal_network,
    system_resolve,
)

pytestmark = pytest.mark.docker

CLIENT_IMAGE = "mpilot/python:3.12"
FAKE_ADDRESS = "104.16.0.35"  # public; never connected to by the offline tests

#: Runs in a client container. Speaks raw CONNECT to the proxy, optionally sends a real
#: ClientHello, and prints one JSON line with what happened.
CLIENT = r"""
import http.client, json, os, socket, ssl
out = {}
host, port = os.environ["PROXY"].rsplit(":", 1)
mode = os.environ["MODE"]
try:
    if mode == "https":
        conn = http.client.HTTPSConnection(host, int(port), timeout=30)
        conn.set_tunnel(os.environ["TARGET"], 443)
        conn.request("GET", os.environ["PATH_"], headers={"User-Agent": "mpilot-test"})
        resp = conn.getresponse()
        out["status"] = resp.status
        out["body_bytes"] = len(resp.read())
    elif mode == "direct":
        try:
            out["dns"] = socket.getaddrinfo(os.environ["TARGET"], 443)[0][4][0]
        except OSError as exc:
            out["dns_error"] = str(exc)
        try:
            socket.create_connection((os.environ["IP"], 443), timeout=5).close()
            out["tcp"] = "connected"
        except OSError as exc:
            out["tcp_error"] = str(exc)
    else:
        s = socket.create_connection((host, int(port)), timeout=10)
        s.settimeout(10)
        extra = os.environ.get("HEADERS", "")
        head = "CONNECT %s HTTP/1.1\r\n%s\r\n" % (os.environ["TARGET"], extra)
        s.sendall(head.encode())
        f = s.makefile("rb")
        out["status"] = f.readline().decode().strip()
        if mode == "hello" and out["status"].startswith("HTTP/1.1 200"):
            f.readline()
            ctx = ssl.create_default_context()
            inc, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
            tls = ctx.wrap_bio(inc, outgoing, server_hostname=os.environ["SNI"])
            try:
                tls.do_handshake()
            except ssl.SSLWantReadError:
                pass
            s.sendall(outgoing.read())
            try:
                out["after"] = len(s.recv(100))
            except (ConnectionResetError, socket.timeout):
                out["after"] = 0
except OSError as exc:
    out["error"] = "%s: %s" % (type(exc).__name__, exc)
print(json.dumps(out))
"""


def fake_resolve(host: str) -> list[str]:
    return [FAKE_ADDRESS]


@pytest.fixture(scope="module")
def images() -> dict[str, str]:
    client = docker.from_env(timeout=120)
    try:
        refs = {}
        for spec in im.image_specs():
            if spec.key in ("proxy:1", "python:3.12"):
                im.build_image(spec)
                refs[spec.key] = im.resolve_image_ref(client, spec.tag)
        return refs
    finally:
        client.close()


def make_internal(client: docker.DockerClient, rid: RunId, suffix: str = "") -> str:
    del suffix  # names are unique already
    return create_internal_network(client, rid)


@pytest.fixture
def internal(client: docker.DockerClient, run_id: RunId) -> str:
    return make_internal(client, run_id)


@pytest.fixture
def manager(client: docker.DockerClient) -> EgressProxyManager:
    return EgressProxyManager(client, resolve=fake_resolve)


@pytest.fixture
def proxy(
    manager: EgressProxyManager, internal: str, run_id: RunId, images: dict[str, str]
) -> Iterator[ProxyHandle]:
    handle = asyncio.run(
        manager.start(
            run_id=run_id,
            internal_network=internal,
            settings=SandboxSettings(),
            image=images["proxy:1"],
        )
    )
    yield handle
    asyncio.run(manager.stop(handle))


def client_run(
    client: docker.DockerClient,
    network: str,
    rid: RunId,
    env: dict[str, str],
) -> dict[str, Any]:
    out = client.containers.run(
        CLIENT_IMAGE,
        ["python", "-c", CLIENT],
        environment=env,
        network=network,
        runtime="runsc",
        user=f"{os.getuid()}:{os.getgid()}",
        read_only=True,
        cap_drop=["ALL"],
        security_opt=["no-new-privileges"],
        labels={LABEL_RUN: str(rid)},
        remove=True,
        stderr=True,
    )
    return dict(json.loads(bytes(out).decode().strip().splitlines()[-1]))


def ask(client: docker.DockerClient, proxy: ProxyHandle, **env: str) -> dict[str, Any]:
    return client_run(
        client, proxy.internal_network, proxy.run_id, {"PROXY": f"{proxy.address}:3128", **env}
    )


def denials(manager: EgressProxyManager, proxy: ProxyHandle) -> list[ProxyEvent]:
    return [e for e in manager.events(proxy) if e.event == "deny"]


def no_tunnel_opened(manager: EgressProxyManager, proxy: ProxyHandle) -> bool:
    return not [e for e in manager.events(proxy) if e.event in ("allow", "end")]


# --- the container -----------------------------------------------------------------------------


def test_the_proxy_container_is_hardened_and_on_exactly_two_networks(
    client: docker.DockerClient, proxy: ProxyHandle, internal: str
) -> None:
    attrs = client.api.inspect_container(proxy.container_id)
    host = attrs["HostConfig"]
    assert host["Runtime"] == "runsc"
    assert proxy.runtime.value == "runsc"
    assert host["ReadonlyRootfs"] is True
    assert host["CapDrop"] == ["ALL"]
    assert "no-new-privileges" in host["SecurityOpt"]
    assert host["Privileged"] is False
    assert host["LogConfig"] == {
        "Type": "json-file",
        "Config": {"max-file": "1", "max-size": "10m"},
    }
    assert attrs["Config"]["User"] == "65534:65534"
    assert attrs["Mounts"] == [], "no host path at all"
    assert set(attrs["NetworkSettings"]["Networks"]) == {internal, EGRESS_NETWORK}
    assert attrs["Config"]["Labels"]["mpilot.role"] == "proxy"
    assert ipaddress.ip_address(proxy.address) in ipaddress.ip_network(proxy.client_cidr)


def test_the_networks_are_what_the_design_says(
    client: docker.DockerClient, proxy: ProxyHandle, internal: str
) -> None:
    egress = client.networks.get(EGRESS_NETWORK).attrs
    assert egress["Internal"] is False
    assert egress["Options"]["com.docker.network.bridge.enable_icc"] == "false"
    inner = client.networks.get(internal).attrs
    assert inner["Internal"] is True
    assert inner["Options"]["com.docker.network.bridge.gateway_mode_ipv4"] == "isolated"


def test_the_proxy_gets_the_configured_allowlist_and_host_resolved_addresses(
    client: docker.DockerClient, proxy: ProxyHandle
) -> None:
    argv = client.api.inspect_container(proxy.container_id)["Config"]["Cmd"]
    allowed = [argv[i + 1] for i, a in enumerate(argv) if a == "--allow"]
    assert allowed == list(DEFAULT_EGRESS_ALLOWLIST)
    addresses = [argv[i + 1] for i, a in enumerate(argv) if a == "--address"]
    assert addresses == [f"{h}={FAKE_ADDRESS}" for h in DEFAULT_EGRESS_ALLOWLIST]
    assert argv[argv.index("--client-cidr") + 1] == proxy.client_cidr
    assert proxy.allowed == DEFAULT_EGRESS_ALLOWLIST
    assert proxy.problems == ()


# --- the acceptance list (P2-08) -----------------------------------------------------------------


def test_example_com_is_denied_and_logged(
    client: docker.DockerClient, manager: EgressProxyManager, proxy: ProxyHandle
) -> None:
    result = ask(client, proxy, MODE="status", TARGET="example.com:443")
    assert result["status"].startswith("HTTP/1.1 403"), result
    [deny] = denials(manager, proxy)
    assert deny.reason == "host_not_allowlisted"
    assert deny.target == "example.com:443"
    assert deny.client is not None
    assert ipaddress.ip_address(deny.client.rsplit(":", 1)[0]) in ipaddress.ip_network(
        proxy.client_cidr
    )
    assert no_tunnel_opened(manager, proxy)


@pytest.mark.parametrize(
    "target", ["1.1.1.1:443", "104.16.0.35:443", "[2606:4700::1111]:443", "2130706433:443"]
)
def test_a_raw_ip_connect_is_denied(
    target: str, client: docker.DockerClient, manager: EgressProxyManager, proxy: ProxyHandle
) -> None:
    result = ask(client, proxy, MODE="status", TARGET=target)
    assert result["status"].startswith("HTTP/1.1 403"), result
    assert [d.reason for d in denials(manager, proxy)] == ["ip_literal"]
    assert no_tunnel_opened(manager, proxy)


@pytest.mark.parametrize(
    "target", ["registry.npmjs.org:80", "registry.npmjs.org:22", "pypi.org:8443"]
)
def test_a_connect_to_another_port_is_denied(
    target: str, client: docker.DockerClient, manager: EgressProxyManager, proxy: ProxyHandle
) -> None:
    result = ask(client, proxy, MODE="status", TARGET=target)
    assert result["status"].startswith("HTTP/1.1 403"), result
    assert [d.reason for d in denials(manager, proxy)] == ["port_not_443"]


def test_a_host_header_naming_another_host_is_denied(
    client: docker.DockerClient, manager: EgressProxyManager, proxy: ProxyHandle
) -> None:
    result = ask(
        client,
        proxy,
        MODE="status",
        TARGET="registry.npmjs.org:443",
        HEADERS="Host: example.com:443\r\n",
    )
    assert result["status"].startswith("HTTP/1.1 403"), result
    assert [d.reason for d in denials(manager, proxy)] == ["host_header_mismatch"]
    assert no_tunnel_opened(manager, proxy)


@pytest.mark.parametrize("sni", ["example.com", "pypi.org"])
def test_an_sni_naming_another_host_is_denied_before_any_upstream_contact(
    sni: str, client: docker.DockerClient, manager: EgressProxyManager, proxy: ProxyHandle
) -> None:
    result = ask(client, proxy, MODE="hello", TARGET="registry.npmjs.org:443", SNI=sni)
    assert result["status"].startswith("HTTP/1.1 200"), result
    assert result["after"] == 0, "the tunnel is closed, nothing comes back"
    [deny] = denials(manager, proxy)
    assert deny.reason == "sni_mismatch"
    assert deny.target == "registry.npmjs.org:443"
    assert no_tunnel_opened(manager, proxy)


# --- isolation around the proxy ----------------------------------------------------------------


def test_the_internal_network_has_no_way_out_but_the_proxy(
    client: docker.DockerClient, proxy: ProxyHandle
) -> None:
    result = client_run(
        client,
        proxy.internal_network,
        proxy.run_id,
        {"PROXY": "x:1", "MODE": "direct", "TARGET": "registry.npmjs.org", "IP": "1.1.1.1"},
    )
    assert "dns" not in result, "no name resolves on the internal network"
    assert "tcp" not in result, "no direct connection out"


def test_a_container_outside_the_internal_network_cannot_use_the_proxy(
    client: docker.DockerClient, manager: EgressProxyManager, proxy: ProxyHandle, run_id: RunId
) -> None:
    egress_ip = client.api.inspect_container(proxy.container_id)["NetworkSettings"]["Networks"][
        EGRESS_NETWORK
    ]["IPAddress"]
    other = make_internal(client, run_id, "-other")
    for network, address in ((EGRESS_NETWORK, egress_ip), (other, proxy.address)):
        result = client_run(
            client,
            network,
            run_id,
            {"PROXY": f"{address}:3128", "MODE": "status", "TARGET": "registry.npmjs.org:443"},
        )
        assert not str(result.get("status", "")).startswith("HTTP/1.1 200"), (network, result)
    assert no_tunnel_opened(manager, proxy)


# --- the manager's own checks ---------------------------------------------------------------------


def _proxies(client: docker.DockerClient, rid: RunId) -> list[Any]:
    return client.containers.list(
        all=True, filters={"label": [f"{LABEL_RUN}={rid}", "mpilot.role=proxy"]}
    )


def test_a_network_that_is_not_internal_is_refused(
    client: docker.DockerClient, manager: EgressProxyManager, run_id: RunId, images: dict[str, str]
) -> None:
    name = f"mpilot-open-{run_id}"
    client.networks.create(name, driver="bridge", internal=False, labels={LABEL_RUN: str(run_id)})
    with pytest.raises(NetworkSetupError, match="not internal"):
        asyncio.run(
            manager.start(
                run_id=run_id,
                internal_network=name,
                settings=SandboxSettings(),
                image=images["proxy:1"],
            )
        )
    assert _proxies(client, run_id) == []


def test_an_internal_network_that_gives_the_host_an_address_is_refused(
    client: docker.DockerClient, manager: EgressProxyManager, run_id: RunId, images: dict[str, str]
) -> None:
    """On a plain internal network the host kernel answers at the gateway IP (P2-08b review)."""
    name = f"mpilot-plain-{run_id}"
    client.networks.create(name, driver="bridge", internal=True, labels={LABEL_RUN: str(run_id)})
    with pytest.raises(NetworkSetupError, match="isolated"):
        asyncio.run(
            manager.start(
                run_id=run_id,
                internal_network=name,
                settings=SandboxSettings(),
                image=images["proxy:1"],
            )
        )
    assert _proxies(client, run_id) == []


def test_a_network_that_already_has_members_is_refused(
    client: docker.DockerClient,
    manager: EgressProxyManager,
    run_id: RunId,
    internal: str,
    images: dict[str, str],
) -> None:
    squatter = client.containers.run(
        CLIENT_IMAGE,
        ["sleep", "60"],
        network=internal,
        detach=True,
        labels={LABEL_RUN: str(run_id)},
    )
    try:
        with pytest.raises(NetworkSetupError, match="already has members"):
            asyncio.run(
                manager.start(
                    run_id=run_id,
                    internal_network=internal,
                    settings=SandboxSettings(),
                    image=images["proxy:1"],
                )
            )
        assert _proxies(client, run_id) == []
    finally:
        squatter.remove(force=True)


def test_another_runs_network_is_refused(
    client: docker.DockerClient,
    manager: EgressProxyManager,
    run_id: RunId,
    internal: str,
    images: dict[str, str],
) -> None:
    with pytest.raises(NetworkSetupError, match="does not belong"):
        asyncio.run(
            manager.start(
                run_id=RunId("run-someone-else"),
                internal_network=internal,
                settings=SandboxSettings(),
                image=images["proxy:1"],
            )
        )


def test_settings_built_around_the_validator_cannot_widen_the_allowlist(
    client: docker.DockerClient,
    manager: EgressProxyManager,
    run_id: RunId,
    internal: str,
    images: dict[str, str],
) -> None:
    widened = SandboxSettings.model_construct(
        egress_allowlist=[*DEFAULT_EGRESS_ALLOWLIST, "example.com"]
    )
    with pytest.raises(NetworkSetupError, match="wider than SPEC"):
        asyncio.run(
            manager.start(
                run_id=run_id, internal_network=internal, settings=widened, image=images["proxy:1"]
            )
        )
    assert _proxies(client, run_id) == []


def test_a_host_that_resolves_to_a_private_address_is_left_out(
    client: docker.DockerClient, run_id: RunId, internal: str, images: dict[str, str]
) -> None:
    def resolve(host: str) -> list[str]:
        return ["10.0.0.7"] if host == "pypi.org" else [FAKE_ADDRESS]

    manager = EgressProxyManager(client, resolve=resolve)
    handle = asyncio.run(
        manager.start(
            run_id=run_id,
            internal_network=internal,
            settings=SandboxSettings(),
            image=images["proxy:1"],
        )
    )
    try:
        assert "pypi.org" not in handle.allowed
        assert any("pypi.org" in p and "10.0.0.7" in p for p in handle.problems)
        result = ask(client, handle, MODE="status", TARGET="pypi.org:443")
        assert result["status"].startswith("HTTP/1.1 403"), result
        assert [d.reason for d in denials(manager, handle)] == ["host_not_allowlisted"]
    finally:
        asyncio.run(manager.stop(handle))


def test_nothing_resolving_is_an_error_and_leaves_nothing_behind(
    client: docker.DockerClient, run_id: RunId, internal: str, images: dict[str, str]
) -> None:
    def resolve(host: str) -> list[str]:
        raise OSError("no DNS here")

    manager = EgressProxyManager(client, resolve=resolve)
    with pytest.raises(NetworkSetupError, match="no allowlisted host resolved"):
        asyncio.run(
            manager.start(
                run_id=run_id,
                internal_network=internal,
                settings=SandboxSettings(),
                image=images["proxy:1"],
            )
        )
    assert _proxies(client, run_id) == []


def test_stop_removes_the_proxy_and_reap_finds_a_forgotten_one(
    client: docker.DockerClient,
    manager: EgressProxyManager,
    run_id: RunId,
    internal: str,
    images: dict[str, str],
) -> None:
    def start() -> ProxyHandle:
        return asyncio.run(
            manager.start(
                run_id=run_id,
                internal_network=internal,
                settings=SandboxSettings(),
                image=images["proxy:1"],
            )
        )

    handle = start()
    asyncio.run(manager.stop(handle))
    assert _proxies(client, run_id) == []
    asyncio.run(manager.stop(handle))  # idempotent
    start()  # then the orchestrator crashes
    assert asyncio.run(DockerDriver(client).reap(run_id)) >= 2  # the proxy and the network
    assert _proxies(client, run_id) == []


def test_a_narrowed_allowlist_is_all_the_proxy_offers(
    client: docker.DockerClient,
    manager: EgressProxyManager,
    run_id: RunId,
    internal: str,
    images: dict[str, str],
) -> None:
    settings = SandboxSettings(egress_allowlist=["pypi.org", "files.pythonhosted.org"])
    handle = asyncio.run(
        manager.start(
            run_id=run_id, internal_network=internal, settings=settings, image=images["proxy:1"]
        )
    )
    try:
        assert handle.allowed == ("pypi.org", "files.pythonhosted.org")
        result = ask(client, handle, MODE="status", TARGET="registry.npmjs.org:443")
        assert result["status"].startswith("HTTP/1.1 403"), result
    finally:
        asyncio.run(manager.stop(handle))


# --- a real registry (network) ------------------------------------------------------------------


@pytest.mark.network
@pytest.mark.parametrize(
    ("target", "path"),
    [("registry.npmjs.org", "/-/ping"), ("pypi.org", "/simple/pip/")],
)
def test_an_allowlisted_registry_is_reachable_through_the_proxy(
    target: str,
    path: str,
    client: docker.DockerClient,
    run_id: RunId,
    internal: str,
    images: dict[str, str],
) -> None:
    manager = EgressProxyManager(client, resolve=system_resolve)
    handle = asyncio.run(
        manager.start(
            run_id=run_id,
            internal_network=internal,
            settings=SandboxSettings(),
            image=images["proxy:1"],
        )
    )
    try:
        result = ask(client, handle, MODE="https", TARGET=target, PATH_=path)
        assert result.get("status") == 200, result
        assert result["body_bytes"] > 0
        events = manager.events(handle)
        [allow] = [e for e in events if e.event == "allow"]
        assert allow.target == f"{target}:443"
        assert allow.sni == target
        assert allow.address is not None
        assert ipaddress.ip_address(allow.address).is_global
        assert [e for e in events if e.event == "deny"] == []
    finally:
        asyncio.run(manager.stop(handle))
