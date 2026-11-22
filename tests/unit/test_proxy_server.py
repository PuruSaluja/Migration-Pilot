"""P2-08a (SPEC §9.3 Network, A7): the egress proxy server on localhost, with a fake upstream.

The upstream is a local TCP server standing in for the registry; `open_upstream` is replaced so
no test touches the internet. What is checked here: the order of the checks, the status sent
for each refusal, that a refused connection never reaches the upstream, that an allowed one is
spliced byte for byte both ways, and that every refusal is logged with its reason. The
container (image, networks, real registries) is P2-08b.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import socket
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from proxy_helpers import client_hello, reframe

from mpilot.sandbox.proxy import server as px
from mpilot.sandbox.proxy.server import Denied, Deny, EgressProxy, Limits, ProxyConfig

ALLOW = frozenset({"registry.npmjs.org", "pypi.org"})
LOCAL = (ipaddress.ip_network("127.0.0.0/8"),)
FAST = Limits(head_timeout_s=2.0, hello_timeout_s=2.0, idle_timeout_s=5.0)


@dataclass
class Upstream:
    """A fake registry: records what it receives and answers each chunk with `ack:<chunk>`."""

    port: int = 0
    received: bytearray = field(default_factory=bytearray)
    connections: int = 0
    eof_seen: asyncio.Event = field(default_factory=asyncio.Event)
    flood: bool = False  # stream data at the client without waiting for it

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        if self.flood:
            with contextlib.suppress(Exception):
                while True:
                    writer.write(b"x" * 1_000_000)
                    await writer.drain()
            return
        try:
            while chunk := await reader.read(65536):
                self.received += chunk
                writer.write(b"ack:" + chunk)
                await writer.drain()
            self.eof_seen.set()
            writer.write(b"bye-after-eof")
            await writer.drain()
        finally:
            writer.close()


@dataclass
class Rig:
    proxy: EgressProxy
    port: int
    upstream: Upstream
    events: list[dict[str, Any]]
    opened: list[tuple[str, int]]

    def denials(self) -> list[str]:
        return [str(e["reason"]) for e in self.events if e["event"] == "deny"]

    def kinds(self) -> list[str]:
        return [str(e["event"]) for e in self.events]


@contextlib.asynccontextmanager
async def rig(
    *,
    allow: frozenset[str] = ALLOW,
    clients: tuple[Any, ...] = LOCAL,
    limits: Limits = FAST,
    open_upstream: Callable[[str, int], Awaitable[px.Upstream]] | None = None,
    flood: bool = False,
) -> AsyncIterator[Rig]:
    upstream = Upstream(flood=flood)
    up_server = await asyncio.start_server(upstream.handle, "127.0.0.1", 0)
    upstream.port = up_server.sockets[0].getsockname()[1]
    events: list[dict[str, Any]] = []
    opened: list[tuple[str, int]] = []

    def sink(event: dict[str, object]) -> None:
        json.dumps(event)  # every record must be JSON-serializable as the real sink writes it
        events.append(dict(event))

    async def to_fake(host: str, port: int) -> px.Upstream:
        opened.append((host, port))
        reader, writer = await asyncio.open_connection("127.0.0.1", upstream.port)
        return reader, writer, "203.0.113.10"

    proxy = EgressProxy(
        ProxyConfig(allowlist=allow, client_networks=clients, limits=limits),
        open_upstream=open_upstream or to_fake,
        log=sink,
        clock=lambda: 1000.0,
    )
    server = await proxy.start("127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield Rig(proxy, port, upstream, events, opened)
    finally:
        server.close()
        await proxy.aclose()
        await server.wait_closed()
        up_server.close()
        await up_server.wait_closed()


async def connect(port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_connection("127.0.0.1", port)


async def request(
    port: int, head: bytes
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, bytes]:
    reader, writer = await connect(port)
    writer.write(head)
    await writer.drain()
    status = await asyncio.wait_for(reader.readline(), 5)
    return reader, writer, status


def connect_head(target: str = "registry.npmjs.org:443", *headers: str) -> bytes:
    lines = [f"CONNECT {target} HTTP/1.1", *headers]
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


async def until_closed(reader: asyncio.StreamReader) -> bytes:
    return await asyncio.wait_for(reader.read(), 5)


async def settle(r: Rig, kind: str, count: int = 1) -> None:
    """Wait until the proxy has logged `count` events of `kind` (it logs after the client
    already saw the result)."""
    for _ in range(200):
        if r.kinds().count(kind) >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"no {kind!r} event: {r.events}")


def run(coro: Awaitable[None]) -> None:
    asyncio.run(asyncio.wait_for(coro, 30))  # type: ignore[arg-type]


# --- an allowed tunnel ------------------------------------------------------------------------


def test_an_allowlisted_tunnel_is_spliced_byte_for_byte_both_ways() -> None:
    async def scenario() -> None:
        async with rig() as r:
            reader, writer, status = await request(
                r.port, connect_head("registry.npmjs.org:443", "Host: registry.npmjs.org:443")
            )
            assert status == b"HTTP/1.1 200 Connection established\r\n"
            assert await reader.readline() == b"\r\n"
            hello = client_hello("registry.npmjs.org")
            writer.write(hello)
            await writer.drain()
            assert await reader.readexactly(4 + len(hello)) == b"ack:" + hello
            writer.write(b"more bytes")
            await writer.drain()
            assert await reader.readexactly(14) == b"ack:more bytes"
            writer.close()
            await settle(r, "end")
            assert r.opened == [("registry.npmjs.org", 443)]
            assert bytes(r.upstream.received) == hello + b"more bytes", "nothing added or lost"
            allow = next(e for e in r.events if e["event"] == "allow")
            assert allow["target"] == "registry.npmjs.org:443"
            assert allow["sni"] == "registry.npmjs.org"
            assert allow["address"] == "203.0.113.10"
            end = next(e for e in r.events if e["event"] == "end")
            assert end["bytes_up"] == len(hello) + 10
            # the upstream's answer to the client's EOF is relayed (and counted) too
            assert end["bytes_down"] == 4 + len(hello) + 14 + len(b"bye-after-eof")
            assert r.denials() == []

    run(scenario())


@pytest.mark.parametrize("how", ["byte-by-byte", "small-records", "tls12"])
def test_a_client_hello_arriving_in_pieces_is_still_checked_and_forwarded(how: str) -> None:
    async def scenario() -> None:
        async with rig() as r:
            reader, writer, status = await request(r.port, connect_head("pypi.org:443"))
            assert status.startswith(b"HTTP/1.1 200")
            await reader.readline()
            hello = client_hello("pypi.org", tls12_only=how == "tls12")
            if how == "small-records":
                hello = reframe(hello, 40)
            if how == "byte-by-byte":
                for i in range(len(hello)):
                    writer.write(hello[i : i + 1])
                    await writer.drain()
            else:
                writer.write(hello)
                await writer.drain()
            assert await reader.readexactly(4) == b"ack:"
            writer.close()
            await settle(r, "end")
            assert bytes(r.upstream.received) == hello
            assert r.denials() == []

    run(scenario())


def test_an_upper_case_sni_for_the_same_host_is_allowed() -> None:
    async def scenario() -> None:
        async with rig() as r:
            reader, writer, _ = await request(r.port, connect_head("pypi.org:443"))
            await reader.readline()
            writer.write(client_hello("PyPI.org"))
            await writer.drain()
            assert await reader.readexactly(4) == b"ack:"
            writer.close()
            await settle(r, "end")

    run(scenario())


def test_half_close_lets_the_upstream_answer_after_the_client_is_done() -> None:
    async def scenario() -> None:
        async with rig() as r:
            reader, writer, _ = await request(r.port, connect_head())
            await reader.readline()
            hello = client_hello("registry.npmjs.org")
            writer.write(hello)
            writer.write_eof()
            data = await until_closed(reader)
            assert data == b"ack:" + hello + b"bye-after-eof"
            await settle(r, "end")

    run(scenario())


def test_an_idle_tunnel_is_closed() -> None:
    async def scenario() -> None:
        limits = Limits(head_timeout_s=2.0, hello_timeout_s=2.0, idle_timeout_s=0.3)
        async with rig(limits=limits) as r:
            reader, writer, _ = await request(r.port, connect_head())
            await reader.readline()
            writer.write(client_hello("registry.npmjs.org"))
            await writer.drain()
            hello = client_hello("registry.npmjs.org")
            assert await reader.readexactly(4) == b"ack:"
            assert (await until_closed(reader)).startswith(hello[:5]), "closed once idle"
            await settle(r, "end")
            assert r.denials() == []

    run(scenario())


# --- refusals before the tunnel opens -----------------------------------------------------------


REFUSED_BEFORE_THE_TUNNEL = [
    (connect_head("example.com:443"), 403, Deny.NOT_ALLOWED),
    (connect_head("github.com:443"), 403, Deny.NOT_ALLOWED),  # allowlisted elsewhere, not here
    (connect_head("npmjs.org:443"), 403, Deny.NOT_ALLOWED),  # no suffix matching
    (connect_head("registry.npmjs.org.evil.example:443"), 403, Deny.NOT_ALLOWED),
    (connect_head("1.1.1.1:443"), 403, Deny.IP_LITERAL),
    (connect_head("104.16.24.35:443"), 403, Deny.IP_LITERAL),
    (connect_head("[2606:4700::1111]:443"), 403, Deny.IP_LITERAL),
    (connect_head("2130706433:443"), 403, Deny.IP_LITERAL),
    (connect_head("0x7f.1:443"), 403, Deny.IP_LITERAL),
    (connect_head("registry.npmjs.org:80"), 403, Deny.PORT),
    (connect_head("registry.npmjs.org:22"), 403, Deny.PORT),
    (connect_head("registry.npmjs.org:8443"), 403, Deny.PORT),
    (connect_head("registry.npmjs.org:443", "Host: example.com:443"), 403, Deny.HOST_HEADER),
    (
        connect_head(
            "registry.npmjs.org:443", "Host: registry.npmjs.org", "Host: registry.npmjs.org"
        ),
        403,
        Deny.HOST_HEADER,
    ),
    (
        b"GET http://registry.npmjs.org/ HTTP/1.1\r\nHost: registry.npmjs.org\r\n\r\n",
        405,
        Deny.METHOD,
    ),
    (b"GET / HTTP/1.1\r\n\r\n", 405, Deny.METHOD),
    (connect_head("registry.npmjs.org"), 400, Deny.BAD_REQUEST),
    (b"\x16\x03\x01\x00\x05hello\r\n\r\n", 400, Deny.BAD_REQUEST),  # TLS straight at the proxy
]


@pytest.mark.parametrize(("head", "status", "why"), REFUSED_BEFORE_THE_TUNNEL)
def test_refusals_before_the_tunnel_get_a_status_a_log_line_and_no_upstream(
    head: bytes, status: int, why: Deny
) -> None:
    async def scenario() -> None:
        async with rig() as r:
            reader, _, line = await request(r.port, head)
            assert line.startswith(f"HTTP/1.1 {status} ".encode()), line
            assert b"Connection: close" in await until_closed(reader)
            await settle(r, "deny")
            assert r.denials() == [why.value]
            assert r.opened == [], "the upstream is never contacted"
            assert r.upstream.connections == 0
            deny = r.events[0]
            assert deny["client"].startswith("127.0.0.1:")
            assert "allow" not in r.kinds()

    run(scenario())


def test_a_denial_names_the_target_it_refused() -> None:
    async def scenario() -> None:
        async with rig() as r:
            await request(r.port, connect_head("example.com:443"))
            await settle(r, "deny")
            assert r.events[0]["target"] == "example.com:443"
            assert r.events[0]["detail"] == "example.com"

    run(scenario())


def test_a_client_outside_the_internal_network_is_dropped_without_an_answer() -> None:
    async def scenario() -> None:
        async with rig(clients=(ipaddress.ip_network("10.0.0.0/8"),)) as r:
            reader, writer = await connect(r.port)
            writer.write(connect_head())
            await writer.drain()
            try:
                answer = await until_closed(reader)
            except ConnectionResetError:  # dropped with our request unread: the kernel resets
                answer = b""
            assert answer == b"", "no HTTP answer at all"
            await settle(r, "deny")
            assert r.denials() == [Deny.CLIENT.value]
            assert r.opened == []

    run(scenario())


def test_a_slow_request_head_times_out() -> None:
    async def scenario() -> None:
        limits = Limits(head_timeout_s=0.2, hello_timeout_s=2.0, idle_timeout_s=5.0)
        async with rig(limits=limits) as r:
            reader, writer = await connect(r.port)
            writer.write(b"CONNECT registry.npmjs.org:443 HTTP/1.1\r\n")  # never finished
            await writer.drain()
            assert (await until_closed(reader)).startswith(b"HTTP/1.1 408 ")
            await settle(r, "deny")
            assert r.denials() == [Deny.TIMEOUT.value]

    run(scenario())


def test_an_oversized_request_head_is_refused() -> None:
    async def scenario() -> None:
        async with rig() as r:
            _, _, line = await request(r.port, connect_head("pypi.org:443", "X: " + "a" * 9000))
            assert line.startswith(b"HTTP/1.1 431 ")
            await settle(r, "deny")
            assert r.denials() == [Deny.TOO_LARGE.value]

    run(scenario())


def test_a_client_that_hangs_up_mid_request_is_logged() -> None:
    async def scenario() -> None:
        async with rig() as r:
            reader, writer = await connect(r.port)
            writer.write(b"CONNECT registry.npmjs.org:443 HTT")
            writer.write_eof()
            await until_closed(reader)
            await settle(r, "deny")
            assert r.denials() == [Deny.BAD_REQUEST.value]

    run(scenario())


def test_connections_past_the_cap_are_refused_busy() -> None:
    async def scenario() -> None:
        limits = Limits(
            max_connections=1, head_timeout_s=5.0, hello_timeout_s=5.0, idle_timeout_s=5.0
        )
        async with rig(limits=limits) as r:
            _, first = await connect(r.port)
            first.write(b"CONNECT pypi")  # holds the only slot
            await first.drain()
            for _ in range(100):
                if r.proxy.active:
                    break
                await asyncio.sleep(0.01)
            _, _, line = await request(r.port, connect_head())
            assert line.startswith(b"HTTP/1.1 503 ")
            await settle(r, "deny")
            assert r.denials() == [Deny.BUSY.value]
            first.close()

    run(scenario())


# --- refusals inside the tunnel (after the 200) -----------------------------------------------


@pytest.mark.parametrize(
    ("target", "first_bytes", "why"),
    [
        ("registry.npmjs.org", client_hello("example.com"), Deny.SNI_MISMATCH),
        (
            "registry.npmjs.org",
            client_hello("pypi.org"),
            Deny.SNI_MISMATCH,
        ),  # allowlisted, but not this tunnel
        ("pypi.org", client_hello("pypi.org.evil.example"), Deny.SNI_MISMATCH),
        ("registry.npmjs.org", client_hello(None), Deny.NO_SNI),
        ("registry.npmjs.org", b"GET / HTTP/1.1\r\nHost: example.com\r\n\r\n", Deny.NOT_TLS),
        ("registry.npmjs.org", b"\x16\x03\x01\x00\x10" + b"\x02" * 16, Deny.NOT_TLS),
    ],
)
def test_refusals_inside_the_tunnel_close_it_before_the_upstream_is_contacted(
    target: str, first_bytes: bytes, why: Deny
) -> None:
    async def scenario() -> None:
        async with rig() as r:
            reader, writer, status = await request(r.port, connect_head(f"{target}:443"))
            assert status.startswith(b"HTTP/1.1 200")
            await reader.readline()
            writer.write(first_bytes)
            await writer.drain()
            assert await until_closed(reader) == b"", "closed, and no HTTP status inside TLS"
            await settle(r, "deny")
            assert r.denials() == [why.value]
            assert r.opened == []
            assert r.upstream.connections == 0
            assert r.events[0]["target"] == f"{target}:443"

    run(scenario())


def test_a_tunnel_closed_before_a_whole_client_hello_is_not_tls() -> None:
    async def scenario() -> None:
        async with rig() as r:
            reader, writer, _ = await request(r.port, connect_head())
            await reader.readline()
            writer.write(client_hello("registry.npmjs.org")[:20])
            writer.write_eof()
            await until_closed(reader)
            await settle(r, "deny")
            assert r.denials() == [Deny.NOT_TLS.value]
            assert r.opened == []

    run(scenario())


def test_a_tunnel_with_no_client_hello_times_out_without_an_http_answer() -> None:
    async def scenario() -> None:
        limits = Limits(head_timeout_s=2.0, hello_timeout_s=0.2, idle_timeout_s=5.0)
        async with rig(limits=limits) as r:
            reader, _, status = await request(r.port, connect_head())
            assert status.startswith(b"HTTP/1.1 200")
            await reader.readline()
            assert await until_closed(reader) == b""
            await settle(r, "deny")
            assert r.denials() == [Deny.TIMEOUT.value]

    run(scenario())


def test_an_upstream_refusal_is_logged_and_closes_the_tunnel() -> None:
    async def refuse(host: str, port: int) -> px.Upstream:
        raise Denied(Deny.NON_PUBLIC, f"{host} -> 10.0.0.5")

    async def scenario() -> None:
        async with rig(open_upstream=refuse) as r:
            reader, writer, _ = await request(r.port, connect_head())
            await reader.readline()
            writer.write(client_hello("registry.npmjs.org"))
            await writer.drain()
            assert await until_closed(reader) == b""
            await settle(r, "deny")
            assert r.denials() == [Deny.NON_PUBLIC.value]
            assert "10.0.0.5" in r.events[0]["detail"]

    run(scenario())


def test_one_broken_connection_does_not_stop_the_proxy() -> None:
    calls = {"n": 0}

    async def flaky(host: str, port: int) -> px.Upstream:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("an unexpected bug")
        raise Denied(Deny.UPSTREAM, "down")

    async def scenario() -> None:
        async with rig(open_upstream=flaky) as r:
            for _ in range(2):
                reader, writer, _ = await request(r.port, connect_head())
                await reader.readline()
                writer.write(client_hello("registry.npmjs.org"))
                await writer.drain()
                await until_closed(reader)
            await settle(r, "deny")
            assert "error" in r.kinds()
            assert r.denials() == [Deny.UPSTREAM.value]
            # and denials before the tunnel still work after both
            _, _, line = await request(r.port, connect_head("example.com:443"))
            assert line.startswith(b"HTTP/1.1 403")

    run(scenario())


def test_log_records_are_single_line_json_with_untrusted_text_escaped() -> None:
    lines: list[str] = []

    class Stream:
        def write(self, text: str) -> None:
            lines.append(text)

        def flush(self) -> None:
            pass

    sink = px.json_lines(Stream())  # type: ignore[arg-type]
    proxy = EgressProxy(
        ProxyConfig(allowlist=ALLOW, client_networks=LOCAL), log=sink, clock=lambda: 5.0
    )
    proxy.log_event("deny", reason="x", target="evil" + chr(0x2028) + "\n\x1b[31m" + "a" * 500)
    assert len(lines) == 1
    assert lines[0].endswith("\n")
    assert lines[0].count("\n") == 1
    assert lines[0].isascii()
    record = json.loads(lines[0])
    assert record["ts"] == 5.0
    assert len(record["target"]) <= 203, "untrusted text is clipped"


# --- resolving and connecting (production `public_upstream`) -----------------------------------


def resolver_of(answer: list[str] | Exception, delay: float = 0.0) -> px.Resolver:
    async def resolve(host: str, port: int) -> list[str]:
        await asyncio.sleep(delay)
        if isinstance(answer, Exception):
            raise answer
        return answer

    return resolve


@pytest.mark.parametrize(
    ("answer", "why"),
    [
        (["10.0.0.5"], Deny.NON_PUBLIC),
        (["104.16.24.35", "127.0.0.1"], Deny.NON_PUBLIC),  # one bad answer spoils the lot
        (["169.254.169.254"], Deny.NON_PUBLIC),
        (["::ffff:192.168.1.1"], Deny.NON_PUBLIC),
        ([], Deny.RESOLVE),
        (OSError("Name or service not known"), Deny.RESOLVE),
    ],
)
def test_public_upstream_refuses_before_connecting(
    answer: list[str] | Exception, why: Deny
) -> None:
    async def scenario() -> None:
        connects: list[Any] = []

        async def no_connect(*a: Any, **k: Any) -> Any:
            connects.append(a)
            raise AssertionError("must not connect")

        open_upstream = px.public_upstream(resolver_of(answer), timeout_s=1.0)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(px.asyncio, "open_connection", no_connect)
            with pytest.raises(Denied) as exc:
                await open_upstream("registry.npmjs.org", 443)
        assert exc.value.reason is why
        assert connects == []

    run(scenario())


def test_a_slow_resolver_is_a_resolve_failure() -> None:
    async def scenario() -> None:
        open_upstream = px.public_upstream(resolver_of(["104.16.24.35"], delay=1.0), timeout_s=0.1)
        with pytest.raises(Denied) as exc:
            await open_upstream("registry.npmjs.org", 443)
        assert exc.value.reason is Deny.RESOLVE

    run(scenario())


def test_public_upstream_connects_to_the_checked_address_not_the_name() -> None:
    async def scenario() -> None:
        tried: list[tuple[str, int]] = []
        sentinel_reader, sentinel_writer = object(), object()

        async def fake_connect(host: str, port: int) -> Any:
            tried.append((host, port))
            if host == "104.16.24.35":
                raise ConnectionRefusedError("refused")
            return sentinel_reader, sentinel_writer

        open_upstream = px.public_upstream(
            resolver_of(["104.16.24.35", "104.16.25.35"]), timeout_s=1.0
        )
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(px.asyncio, "open_connection", fake_connect)
            reader, writer, address = await open_upstream("registry.npmjs.org", 443)
        assert tried == [("104.16.24.35", 443), ("104.16.25.35", 443)]
        assert (reader, writer, address) == (sentinel_reader, sentinel_writer, "104.16.25.35")

    run(scenario())


def test_public_upstream_reports_when_no_address_answers() -> None:
    async def scenario() -> None:
        async def refuse(host: str, port: int) -> Any:
            raise ConnectionRefusedError(f"refused {host}")

        open_upstream = px.public_upstream(resolver_of(["104.16.24.35"]), timeout_s=1.0)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(px.asyncio, "open_connection", refuse)
            with pytest.raises(Denied) as exc:
                await open_upstream("registry.npmjs.org", 443)
        assert exc.value.reason is Deny.UPSTREAM

    run(scenario())


# --- configuration and the command line --------------------------------------------------------


def test_the_config_fails_closed() -> None:
    with pytest.raises(ValueError, match="no client network"):
        ProxyConfig(allowlist=ALLOW, client_networks=())
    with pytest.raises(ValueError, match="empty"):
        ProxyConfig(allowlist=frozenset(), client_networks=LOCAL)
    with pytest.raises(ValueError, match="written as"):
        ProxyConfig(allowlist=frozenset({"PyPI.org"}), client_networks=LOCAL)


def test_client_permitted() -> None:
    config = ProxyConfig(allowlist=ALLOW, client_networks=(ipaddress.ip_network("172.30.0.0/24"),))
    assert config.client_permitted("172.30.0.7")
    assert config.client_permitted("::ffff:172.30.0.7")
    assert not config.client_permitted("172.30.1.7")
    assert not config.client_permitted("127.0.0.1")
    assert not config.client_permitted("")
    assert not config.client_permitted("garbage")


def test_the_command_line_requires_an_allowlist_and_a_client_network() -> None:
    with pytest.raises(SystemExit):
        px.parse_args(["--client-cidr", "172.30.0.0/24"])
    with pytest.raises(SystemExit):
        px.parse_args(["--allow", "pypi.org"])
    args = px.parse_args(
        ["--allow", "pypi.org", "--allow", "github.com", "--client-cidr", "172.30.0.0/24"]
    )
    config = px.config_from_args(args)
    assert config.allowlist == {"pypi.org", "github.com"}
    assert [str(n) for n in config.client_networks] == ["172.30.0.0/24"]
    assert args.port == px.DEFAULT_LISTEN_PORT


@pytest.mark.parametrize(
    "argv",
    [
        ["--allow", "*.npmjs.org", "--client-cidr", "172.30.0.0/24"],
        ["--allow", "1.2.3.4", "--client-cidr", "172.30.0.0/24"],
        ["--allow", "pypi.org", "--client-cidr", "172.30.0.1/24"],  # host bits set
        ["--allow", "pypi.org", "--client-cidr", "nonsense"],
    ],
)
def test_a_bad_command_line_exits_with_a_message(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        px.config_from_args(px.parse_args(argv))
    assert "egress-proxy:" in str(exc.value.code)


def test_serve_forever_logs_ready_and_answers(capsys: pytest.CaptureFixture[str]) -> None:
    async def scenario() -> None:
        args = px.parse_args(
            [
                "--listen",
                "127.0.0.1",
                "--port",
                "0",
                "--allow",
                "pypi.org",
                "--client-cidr",
                "127.0.0.0/8",
            ]
        )
        task = asyncio.ensure_future(px._serve_forever(args))
        seen = ""
        for _ in range(200):
            seen += capsys.readouterr().out
            if '"event": "ready"' in seen or task.done():
                break
            await asyncio.sleep(0.01)
        assert not task.done(), "the server keeps running"
        ready = json.loads(next(line for line in seen.splitlines() if '"ready"' in line))
        assert ready["allowlist"] == ["pypi.org"]
        assert ready["client_cidr"] == ["127.0.0.0/8"]
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    run(scenario())


# --- review fixes: stalled peers, ECH, pipelined hellos, tiny records -------------------------


async def stalled_client(port: int, sni: str) -> socket.socket:
    """A client that sends CONNECT and its ClientHello, then never reads: a tiny receive
    buffer fills and its window drops to zero."""
    loop = asyncio.get_running_loop()
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    sock.setblocking(False)
    await loop.sock_connect(sock, ("127.0.0.1", port))
    await loop.sock_sendall(sock, connect_head(f"{sni}:443") + client_hello(sni))
    return sock


def test_a_peer_that_stops_reading_cannot_hold_connections_past_the_cap() -> None:
    async def scenario() -> None:
        limits = Limits(
            max_connections=2,
            head_timeout_s=2.0,
            hello_timeout_s=2.0,
            idle_timeout_s=0.4,
            close_timeout_s=0.3,
        )
        async with rig(limits=limits, flood=True) as r:
            socks = []
            for n in range(1, 5):  # twice the cap
                socks.append(await stalled_client(r.port, "pypi.org"))
                for _ in range(400):
                    if r.kinds().count("end") >= n and r.proxy.active == 0 and not r.proxy._tasks:
                        break
                    await asyncio.sleep(0.01)
                assert r.kinds().count("end") >= n, r.events
                assert r.proxy.active == 0, "a stalled tunnel still holds a slot"
                assert not r.proxy._tasks, "a stalled tunnel's task is still alive"
            assert Deny.BUSY.value not in r.denials()
            # and the proxy still serves an ordinary client
            _, writer, status = await request(r.port, connect_head("pypi.org:443"))
            assert status.startswith(b"HTTP/1.1 200")
            writer.close()
            for sock in socks:
                sock.close()

    run(scenario())


def test_an_encrypted_client_hello_is_refused_inside_the_tunnel() -> None:
    from proxy_helpers import hand_made_hello, sni_extension
    from test_proxy_parsing import ech_extension

    async def scenario() -> None:
        async with rig() as r:
            reader, writer, _ = await request(r.port, connect_head())
            await reader.readline()
            writer.write(hand_made_hello(sni_extension(b"registry.npmjs.org") + ech_extension()))
            await writer.drain()
            assert await until_closed(reader) == b""
            await settle(r, "deny")
            assert r.denials() == [Deny.ECH.value]
            assert r.opened == []

    run(scenario())


def test_a_client_hello_sent_with_the_connect_before_the_200_is_still_checked() -> None:
    async def scenario() -> None:
        async with rig() as r:
            for sni, allowed in (("registry.npmjs.org", True), ("example.com", False)):
                reader, writer = await connect(r.port)
                hello = client_hello(sni)
                writer.write(connect_head() + hello)  # no wait for the 200
                await writer.drain()
                assert (await reader.readline()).startswith(b"HTTP/1.1 200")
                await reader.readline()
                if allowed:
                    assert await reader.readexactly(4 + len(hello)) == b"ack:" + hello
                    writer.close()
                    await settle(r, "end")
                else:
                    assert await until_closed(reader) == b""
                    await settle(r, "deny")
            assert r.denials() == [Deny.SNI_MISMATCH.value]
            assert r.opened == [("registry.npmjs.org", 443)]

    run(scenario())


def test_a_client_hello_in_many_tiny_records_is_refused_at_the_server() -> None:
    async def scenario() -> None:
        async with rig() as r:
            reader, writer, _ = await request(r.port, connect_head())
            await reader.readline()
            writer.write(reframe(client_hello("registry.npmjs.org"), 1))
            await writer.drain()
            with contextlib.suppress(ConnectionResetError):
                assert await until_closed(reader) == b""
            await settle(r, "deny")
            assert r.denials() == [Deny.TOO_LARGE.value]
            assert r.opened == []

    run(scenario())


def test_a_peer_that_keeps_sending_but_never_reads_is_cut_off() -> None:
    """The client trickles bytes (so the tunnel is never idle) but reads nothing, so the
    proxy's writes to it block. That must end the tunnel too, within the idle period."""

    async def scenario() -> None:
        limits = Limits(
            head_timeout_s=2.0, hello_timeout_s=2.0, idle_timeout_s=0.5, close_timeout_s=0.3
        )
        async with rig(limits=limits, flood=True) as r:
            loop = asyncio.get_running_loop()
            sock = await stalled_client(r.port, "pypi.org")
            try:
                for _ in range(60):  # ~6 s of trickle, far past the idle period
                    if r.kinds().count("end"):
                        break
                    with contextlib.suppress(OSError):
                        await loop.sock_sendall(sock, b".")
                    await asyncio.sleep(0.1)
                assert r.kinds().count("end") == 1, "the stalled tunnel was not ended"
            finally:
                sock.close()

    run(scenario())


def test_a_closing_connection_keeps_its_slot_until_the_socket_is_gone() -> None:
    async def scenario() -> None:
        limits = Limits(
            max_connections=1,
            head_timeout_s=2.0,
            hello_timeout_s=2.0,
            idle_timeout_s=0.3,
            close_timeout_s=1.5,
        )
        async with rig(limits=limits, flood=True) as r:
            sock = await stalled_client(r.port, "pypi.org")
            try:
                await settle(r, "end")  # the tunnel is over; its close is still waiting
                assert r.proxy.active == 1, "the slot must stay taken while the socket closes"
                _, _, line = await request(r.port, connect_head("pypi.org:443"))
                assert line.startswith(b"HTTP/1.1 503"), "the cap still counts it"
                for _ in range(300):
                    if r.proxy.active == 0:
                        break
                    await asyncio.sleep(0.01)
                assert r.proxy.active == 0, "aborted after close_timeout_s"
            finally:
                sock.close()

    run(scenario())
