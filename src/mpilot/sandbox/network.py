"""The egress proxy container and its networks, host side (SPEC §9.3 Network, decisions A7).

A run's install containers (P2-09) sit on a per-run Docker network created with `internal=True`:
no route out. The proxy is the only other member. It also joins one shared egress network, a
plain bridge with inter-container traffic disabled, so proxies of different runs cannot reach
each other. Clients are only accepted from the internal network's subnet.

**Names are resolved here, on the host, when the proxy starts.** Under gVisor a container
cannot use Docker's embedded DNS (127.0.0.11 relies on iptables rules inside the container's
namespace, which gVisor's netstack does not see), so the proxy would have no resolver at all.
Instead `resolve_allowlist` resolves each allowlisted host, keeps a host only if every address
is public (the same test the proxy applies), and passes the table with `--address`. The proxy
then never resolves anything and connects only to those addresses. A host that does not
resolve, or resolves to anything non-public, is left out (and reported), so a tunnel to it is
refused as `resolve_failed`. Addresses can go stale over a long proxy life; start a fresh
proxy for each install (P2-09).

The allowlist comes only from `SandboxSettings.egress_allowlist`, which the config already
restricts to the §9.3 list; it is checked against that list again here.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import secrets
import socket
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import docker
from docker.errors import APIError, DockerException, NotFound
from docker.types import LogConfig
from pydantic import BaseModel, ConfigDict, ValidationError

from mpilot.config import DEFAULT_EGRESS_ALLOWLIST, SandboxSettings
from mpilot.core.errors import MpilotError
from mpilot.core.types import RunId
from mpilot.sandbox.base import ContainerRuntime
from mpilot.sandbox.driver import LABEL_ROLE, LABEL_RUN
from mpilot.sandbox.proxy.server import DEFAULT_LISTEN_PORT, is_public_address

EGRESS_NETWORK: Final = "mpilot-egress"
LABEL_EGRESS: Final = "mpilot.egress"
ROLE_PROXY: Final = "proxy"
PROXY_PORT: Final = DEFAULT_LISTEN_PORT
READY_TIMEOUT_S: Final = 20.0
#: The proxy's log is the record of every denial; it is capped so an install loop cannot fill
#: the disk with them (json-file keeps at most this much, newest kept).
LOG_MAX_SIZE: Final = "10m"
#: The most log text `proxy_events` reads back.
MAX_LOG_BYTES: Final = 4 * 1024 * 1024
_ICC: Final = "com.docker.network.bridge.enable_icc"


class NetworkSetupError(MpilotError):
    """The egress network or the proxy could not be set up as required."""


# --- resolving the allowlist on the host --------------------------------------------------

Resolve = Callable[[str], list[str]]


def system_resolve(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    out: list[str] = []
    for *_, sockaddr in infos:
        address = str(sockaddr[0])
        if address not in out:
            out.append(address)
    return out


def resolve_allowlist(
    hosts: Sequence[str], resolve: Resolve = system_resolve
) -> tuple[dict[str, list[str]], list[str]]:
    """Each host's addresses, for the hosts whose every address is public; and a problem
    line for each host left out."""
    table: dict[str, list[str]] = {}
    problems: list[str] = []
    for host in hosts:
        try:
            addresses = resolve(host)
        except OSError as exc:
            problems.append(f"{host}: does not resolve ({exc})")
            continue
        if not addresses:
            problems.append(f"{host}: no addresses")
            continue
        private = [a for a in addresses if not is_public_address(a)]
        if private:
            problems.append(f"{host}: resolves to non-public {', '.join(private[:4])}; left out")
            continue
        table[host] = [str(ipaddress.ip_address(a)) for a in addresses]
    return table, problems


def proxy_command(
    allowlist: Sequence[str], client_cidr: str, addresses: Mapping[str, Sequence[str]]
) -> list[str]:
    """The proxy's arguments. Only hosts with addresses are allowed: a host the host could not
    resolve would only fail later, so it is not offered at all."""
    argv = ["--listen", "0.0.0.0", "--port", str(PROXY_PORT), "--client-cidr", client_cidr]  # noqa: S104
    for host in allowlist:
        if host in addresses:
            argv += ["--allow", host, "--address", f"{host}={','.join(addresses[host])}"]
    return argv


# --- the proxy's log ---------------------------------------------------------------------------


class ProxyEvent(BaseModel):
    """One line of the proxy's JSON log. Untrusted text inside (targets, details) is clipped by
    the proxy; fields beyond these are ignored."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    ts: float
    event: str
    reason: str | None = None
    client: str | None = None
    target: str | None = None
    sni: str | None = None
    address: str | None = None
    detail: str | None = None
    bytes_up: int | None = None
    bytes_down: int | None = None


def parse_proxy_log(text: str) -> tuple[list[ProxyEvent], int]:
    """The events in `text`, and how many lines were not valid events (skipped)."""
    events: list[ProxyEvent] = []
    bad = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            events.append(ProxyEvent.model_validate(json.loads(line)))
        except (ValueError, ValidationError):
            bad += 1
    return events, bad


# --- the proxy container -------------------------------------------------------------------------


@dataclass(frozen=True)
class ProxyHandle:
    container_id: str
    name: str
    run_id: RunId
    internal_network: str
    client_cidr: str
    address: str  # the proxy's address on the internal network
    runtime: ContainerRuntime
    allowed: tuple[str, ...]  # the hosts the proxy accepts (resolved ones only)
    problems: tuple[str, ...]  # hosts left out, and why
    warnings: tuple[str, ...]

    @property
    def url(self) -> str:
        """For `HTTPS_PROXY` in an install container."""
        return f"http://{self.address}:{PROXY_PORT}"


class EgressProxyManager:
    """Starts and stops egress proxies. Synchronous Docker calls run in a thread."""

    def __init__(
        self,
        client: docker.DockerClient,
        *,
        runsc_name: str = "runsc",
        runc_name: str = "runc",
        resolve: Resolve = system_resolve,
    ) -> None:
        self._client = client
        self._runsc = runsc_name
        self._runc = runc_name
        self._resolve = resolve

    # -- the shared egress network

    def ensure_egress_network(self) -> str:
        """Create the shared egress bridge, or check that the existing one is ours and has
        inter-container traffic off."""
        try:
            net = self._client.networks.get(EGRESS_NETWORK)
        except NotFound:
            with contextlib.suppress(APIError):  # lost a race with another process: re-check
                self._client.networks.create(
                    EGRESS_NETWORK,
                    driver="bridge",
                    internal=False,
                    options={_ICC: "false"},
                    labels={LABEL_EGRESS: "1"},
                )
            net = self._client.networks.get(EGRESS_NETWORK)
        attrs = net.attrs
        if attrs.get("Labels", {}).get(LABEL_EGRESS) != "1":
            raise NetworkSetupError(f"network {EGRESS_NETWORK} exists but was not made by mpilot")
        if attrs.get("Driver") != "bridge" or attrs.get("Internal"):
            raise NetworkSetupError(f"network {EGRESS_NETWORK} is not a plain bridge")
        if attrs.get("Options", {}).get(_ICC) != "false":
            raise NetworkSetupError(f"network {EGRESS_NETWORK} allows inter-container traffic")
        return EGRESS_NETWORK

    # -- start

    async def start(
        self, *, run_id: RunId, internal_network: str, settings: SandboxSettings, image: str
    ) -> ProxyHandle:
        return await asyncio.to_thread(self._start, run_id, internal_network, settings, image)

    def _internal_subnet(self, run_id: RunId, name: str) -> str:
        try:
            attrs = self._client.networks.get(name).attrs
        except NotFound:
            raise NetworkSetupError(f"internal network {name} does not exist") from None
        if not attrs.get("Internal"):
            raise NetworkSetupError(f"network {name} is not internal: it would have a route out")
        if attrs.get("Labels", {}).get(LABEL_RUN) != str(run_id):
            raise NetworkSetupError(f"network {name} does not belong to run {run_id}")
        subnets = [
            c["Subnet"]
            for c in (attrs.get("IPAM", {}).get("Config") or [])
            if c.get("Subnet") and ipaddress.ip_network(c["Subnet"]).version == 4
        ]
        if len(subnets) != 1:
            raise NetworkSetupError(f"network {name} needs exactly one IPv4 subnet: {subnets}")
        return str(ipaddress.ip_network(subnets[0]))

    def _runtime(self) -> tuple[str, ContainerRuntime, list[str]]:
        runtimes: Mapping[str, Any] = self._client.info().get("Runtimes") or {}
        if self._runsc in runtimes:
            return self._runsc, ContainerRuntime.RUNSC, []
        if self._runc not in runtimes:
            raise NetworkSetupError("Docker has neither runsc nor runc registered")
        return (
            self._runc,
            ContainerRuntime.RUNC,
            [f"gVisor ({self._runsc}) is not registered: the egress proxy runs on runc"],
        )

    def _start(
        self, run_id: RunId, internal_network: str, settings: SandboxSettings, image: str
    ) -> ProxyHandle:
        allowlist = list(settings.egress_allowlist)
        widened = [h for h in allowlist if h not in DEFAULT_EGRESS_ALLOWLIST]
        if widened:  # the config refuses this already; settings built around it are refused too
            raise NetworkSetupError(f"allowlist wider than SPEC §9.3: {widened}")
        subnet = self._internal_subnet(run_id, internal_network)
        egress = self.ensure_egress_network()
        addresses, problems = resolve_allowlist(allowlist, self._resolve)
        if not addresses:
            raise NetworkSetupError("no allowlisted host resolved: " + "; ".join(problems))
        runtime_name, runtime, warnings = self._runtime()
        name = f"mpilot-proxy-{secrets.token_hex(6)}"
        try:
            container = self._client.containers.create(
                image,
                command=proxy_command(allowlist, subnet, addresses),
                name=name,
                runtime=runtime_name,
                user="65534:65534",
                read_only=True,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges"],
                privileged=False,
                network=internal_network,
                pids_limit=64,
                mem_limit=256 * 1024**2,
                memswap_limit=256 * 1024**2,
                nano_cpus=1_000_000_000,
                log_config=LogConfig(
                    type=LogConfig.types.JSON, config={"max-size": LOG_MAX_SIZE, "max-file": "1"}
                ),
                labels={LABEL_RUN: str(run_id), LABEL_ROLE: ROLE_PROXY},
                detach=True,
            )
            # Joined before start: gVisor sets up networking when the container boots.
            self._client.networks.get(egress).connect(container)
            container.start()
            address = self._wait_ready(container, internal_network)
        except BaseException:
            with contextlib.suppress(DockerException):
                self._client.api.remove_container(name, v=True, force=True)
            raise
        return ProxyHandle(
            container_id=str(container.id),
            name=name,
            run_id=run_id,
            internal_network=internal_network,
            client_cidr=subnet,
            address=address,
            runtime=runtime,
            allowed=tuple(h for h in allowlist if h in addresses),
            problems=tuple(problems),
            warnings=tuple(warnings),
        )

    def _wait_ready(self, container: Any, internal_network: str) -> str:
        deadline = time.monotonic() + READY_TIMEOUT_S
        while time.monotonic() < deadline:
            container.reload()
            if container.status not in ("created", "running"):
                tail = container.logs(stdout=True, stderr=True, tail=20).decode("utf-8", "replace")
                raise NetworkSetupError(f"the egress proxy exited ({container.status}): {tail}")
            events, _ = parse_proxy_log(
                container.logs(stdout=True, stderr=False).decode("utf-8", "replace")
            )
            if any(e.event == "ready" for e in events):
                nets = container.attrs["NetworkSettings"]["Networks"]
                address = str(nets.get(internal_network, {}).get("IPAddress") or "")
                if not address:
                    raise NetworkSetupError(
                        "the egress proxy has no address on the internal network"
                    )
                return address
            time.sleep(0.1)
        raise NetworkSetupError(f"the egress proxy did not report ready in {READY_TIMEOUT_S:.0f} s")

    # -- logs and stop

    def events(self, handle: ProxyHandle) -> list[ProxyEvent]:
        """The proxy's log so far (the newest `MAX_LOG_BYTES` of it)."""
        raw = self._client.api.logs(handle.container_id, stdout=True, stderr=False)
        text = bytes(raw)[-MAX_LOG_BYTES:].decode("utf-8", "replace")
        return parse_proxy_log(text)[0]

    async def stop(self, handle: ProxyHandle) -> None:
        await asyncio.to_thread(self._stop, handle)

    def _stop(self, handle: ProxyHandle) -> None:
        api = self._client.api
        with contextlib.suppress(NotFound):
            api.remove_container(handle.container_id, v=True, force=True)
        try:
            api.inspect_container(handle.container_id)
        except NotFound:
            return
        raise NetworkSetupError(f"proxy {handle.name} is still there after removal")
