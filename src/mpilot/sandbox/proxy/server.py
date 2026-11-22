"""Egress proxy for install containers (SPEC §9.3 Network, decisions A7). Standard library only.

An install container's only way out is this proxy, over the run's internal network. Each
connection must pass, in this order, before a single byte reaches the internet:

1. the client address is on the internal network (`--client-cidr`);
2. the request is `CONNECT <host>:443 HTTP/1.x` (no other method, no other port);
3. `<host>` is a DNS name, never an IP literal (dotted, bracketed IPv6, or any form
   `inet_aton` accepts, such as `2130706433` or `0x7f.1`), and is on the allowlist exactly
   (no wildcards, no suffix matching);
4. a `Host` header, if sent, names the same host (a second `Host` header is refused);
5. the first bytes in the tunnel are a well-formed TLS ClientHello whose single SNI host name
   equals `<host>` (no SNI, two names, a duplicate extension or anything that is not TLS is
   refused). This stops a tunnel opened to an allowlisted name from carrying a TLS session for
   another (§9.3 "checked by Host and SNI");
6. `<host>` resolves only to public addresses (a name resolving to loopback, private,
   link-local, CGNAT or other non-global space is refused), and the proxy connects to the
   address it checked, not to the name again.

Every refusal is logged as one JSON line on stdout (`"event": "deny"` and a `reason` from
`Deny`); every tunnel also logs `allow` when it opens and `end` with byte counts when it closes.
Before the tunnel is open the client gets an HTTP status; after it (steps 5-6) the connection
is just closed, since the client is already speaking TLS.

A ClientHello carrying Encrypted Client Hello is refused: its SNI is only the outer, public name.

Residual risks. Accepted in §9.3: an install script can reach the allowlisted hosts. Documented
in §9.3 and §20, for the owner to confirm: the proxy cannot see inside TLS, so `github.com` is
reachable as a whole, and a front end that routes on the inner HTTP Host header (domain
fronting) could serve another site behind an allowlisted SNI.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ipaddress
import json
import re
import socket
import sys
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, TextIO

TLS_PORT: Final = 443
DEFAULT_LISTEN_PORT: Final = 3128

_LABEL: Final = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_TOKEN: Final = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_STATUS: Final = {
    400: "Bad Request",
    403: "Forbidden",
    405: "Method Not Allowed",
    408: "Request Timeout",
    431: "Request Header Fields Too Large",
    503: "Service Unavailable",
}


class Deny(StrEnum):
    """Why a connection was refused. The values appear in the log."""

    CLIENT = "client_not_permitted"
    BUSY = "too_many_connections"
    TIMEOUT = "timeout"
    TOO_LARGE = "too_large"
    BAD_REQUEST = "bad_request"
    METHOD = "method_not_connect"
    PORT = "port_not_443"
    IP_LITERAL = "ip_literal"
    BAD_HOST = "invalid_host"
    NOT_ALLOWED = "host_not_allowlisted"
    HOST_HEADER = "host_header_mismatch"
    NOT_TLS = "not_tls"
    ECH = "encrypted_client_hello"
    NO_SNI = "no_sni"
    SNI_MISMATCH = "sni_mismatch"
    RESOLVE = "resolve_failed"
    NON_PUBLIC = "non_public_address"
    UPSTREAM = "upstream_unreachable"


#: The HTTP status sent for a refusal that happens before the tunnel is open.
STATUS_FOR: Final[dict[Deny, int]] = {
    Deny.BUSY: 503,
    Deny.TIMEOUT: 408,
    Deny.TOO_LARGE: 431,
    Deny.BAD_REQUEST: 400,
    Deny.METHOD: 405,
    Deny.PORT: 403,
    Deny.IP_LITERAL: 403,
    Deny.BAD_HOST: 403,
    Deny.NOT_ALLOWED: 403,
    Deny.HOST_HEADER: 403,
}


class Denied(Exception):  # noqa: N818  (a refusal, not a failure of the proxy)
    def __init__(self, reason: Deny, detail: str = "") -> None:
        super().__init__(f"{reason.value}: {detail}" if detail else reason.value)
        self.reason = reason
        self.detail = detail


# --- host names ---------------------------------------------------------------------------


def is_ip_literal(host: str) -> bool:
    """Any spelling of an address: dotted or bracketed, IPv6, and the short, decimal, octal and
    hex IPv4 forms that `inet_aton` (and so many HTTP clients) accept."""
    bare = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        ipaddress.ip_address(bare)
    except ValueError:
        pass
    else:
        return True
    try:
        socket.inet_aton(host)
    except OSError:
        return False
    return True


def normalize_host(raw: str) -> str:
    """The lower-case DNS name `raw` spells, without a trailing dot.

    Raises `Denied(IP_LITERAL)` for an address and `Denied(BAD_HOST)` for anything that is not
    a plain multi-label DNS name (letters, digits and inner hyphens; punycode is fine).
    """
    if not raw or len(raw) > 254 or not raw.isascii():
        raise Denied(Deny.BAD_HOST, _clip(raw))
    if is_ip_literal(raw):
        raise Denied(Deny.IP_LITERAL, _clip(raw))
    host = raw.lower()
    if host.endswith("."):
        host = host[:-1]
    labels = host.split(".")
    if len(host) > 253 or len(labels) < 2 or not all(_LABEL.fullmatch(x) for x in labels):
        raise Denied(Deny.BAD_HOST, _clip(raw))
    if labels[-1].isdigit():  # no top-level domain is numeric: an address in disguise
        raise Denied(Deny.IP_LITERAL, _clip(raw))
    return host


def check_allowlist(hosts: Iterable[str]) -> frozenset[str]:
    """Validate an allowlist: every entry already normalized, no duplicates, not empty."""
    seen: list[str] = []
    for entry in hosts:
        try:
            normal = normalize_host(entry)
        except Denied as exc:
            raise ValueError(f"allowlist entry {entry!r}: {exc.reason.value}") from None
        if normal != entry:
            raise ValueError(f"allowlist entry {entry!r} must be written as {normal!r}")
        if entry in seen:
            raise ValueError(f"allowlist entry {entry!r} appears twice")
        seen.append(entry)
    if not seen:
        raise ValueError("the allowlist is empty")
    return frozenset(seen)


# --- the CONNECT request ------------------------------------------------------------------


@dataclass(frozen=True)
class ConnectRequest:
    host: str  # normalized
    port: int


def parse_connect(head: bytes) -> ConnectRequest:
    """Parse the request head (up to and including the blank line). Checks the method, the
    port, the host's spelling and the Host header; not the allowlist."""
    if not head.endswith(b"\r\n\r\n") or not head.isascii():
        raise Denied(Deny.BAD_REQUEST, "malformed request head")
    lines = head[:-4].decode("ascii").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3 or parts[2] not in ("HTTP/1.1", "HTTP/1.0"):
        raise Denied(Deny.BAD_REQUEST, _clip(lines[0]))
    method, target, _ = parts
    if method != "CONNECT":
        raise Denied(Deny.METHOD, _clip(method))
    if target.startswith("["):
        raise Denied(Deny.IP_LITERAL, _clip(target))
    name, sep, port = target.rpartition(":")
    if not sep or not name:
        raise Denied(Deny.BAD_REQUEST, f"no port in {_clip(target)!r}")
    if port != str(TLS_PORT):
        raise Denied(Deny.PORT, _clip(target))
    host = normalize_host(name)
    host_headers: list[str] = []
    for line in lines[1:]:
        if not line or line[0] in " \t":
            raise Denied(Deny.BAD_REQUEST, "folded or empty header line")
        key, colon, value = line.partition(":")
        if not colon or not _TOKEN.fullmatch(key):
            raise Denied(Deny.BAD_REQUEST, f"bad header {_clip(line)!r}")
        if key.lower() == "host":
            host_headers.append(value.strip(" \t"))
    if len(host_headers) > 1:
        raise Denied(Deny.HOST_HEADER, "more than one Host header")
    if host_headers:
        named = host_headers[0]
        if named.endswith(f":{TLS_PORT}"):
            named = named[: -len(f":{TLS_PORT}")]
        try:
            same = normalize_host(named) == host
        except Denied:
            same = False
        if not same:
            raise Denied(Deny.HOST_HEADER, f"Host {_clip(host_headers[0])!r} for {host}")
    return ConnectRequest(host=host, port=TLS_PORT)


# --- the TLS ClientHello -------------------------------------------------------------------

#: Largest ClientHello accepted. Real ones are a few hundred bytes to ~2 KiB (post-quantum
#: key shares included); the cap only bounds what an install script can make us buffer.
MAX_CLIENT_HELLO: Final = 32 * 1024
_MAX_RECORD: Final = 16 * 1024
#: Real clients put the ClientHello in one record, two at most with large key shares. The cap
#: bounds the re-parsing work an install script can cause with tiny records.
MAX_HELLO_RECORDS: Final = 16
_HANDSHAKE: Final = 22
_CLIENT_HELLO: Final = 1
_SERVER_NAME: Final = 0
_HOST_NAME: Final = 0
#: Encrypted Client Hello (RFC 9849) and the draft code points before it. With ECH the SNI the
#: proxy sees is only the outer, public name; the server decrypts an inner hello that can name
#: any site behind the same front end, so a hello carrying it is refused.
_ECH_EXTENSIONS: Final = frozenset({0xFE0D, 0xFE0C, 0xFE0B, 0xFE0A, 0xFE09, 0xFE08, 0xFFCE})


class NeedMore:
    """`extract_sni` needs more bytes to decide."""

    def __repr__(self) -> str:
        return "NEED_MORE"


NEED_MORE: Final = NeedMore()


def extract_sni(data: bytes) -> str | NeedMore | None:
    """The SNI host name of the ClientHello at the start of `data` (`None` if it has none), or
    `NEED_MORE`. The ClientHello may be split over several handshake records. Raises
    `Denied(NOT_TLS)` for anything malformed, `Denied(ECH)` for an encrypted ClientHello,
    `Denied(TOO_LARGE)` past `MAX_CLIENT_HELLO` or `MAX_HELLO_RECORDS`."""
    handshake = bytearray()
    pos = 0
    records = 0
    while True:
        if records >= MAX_HELLO_RECORDS:
            raise Denied(Deny.TOO_LARGE, f"ClientHello in more than {MAX_HELLO_RECORDS} records")
        if len(data) - pos < 5:
            return NEED_MORE
        ctype, major, length = data[pos], data[pos + 1], int.from_bytes(data[pos + 3 : pos + 5])
        if ctype != _HANDSHAKE or major != 3 or not 0 < length <= _MAX_RECORD:
            raise Denied(Deny.NOT_TLS, "not a TLS handshake record")
        if len(data) - pos - 5 < length:
            return NEED_MORE
        handshake += data[pos + 5 : pos + 5 + length]
        pos += 5 + length
        records += 1
        if len(handshake) < 4:
            continue
        if handshake[0] != _CLIENT_HELLO:
            raise Denied(Deny.NOT_TLS, "the first handshake message is not a ClientHello")
        size = int.from_bytes(handshake[1:4])
        if size > MAX_CLIENT_HELLO:
            raise Denied(Deny.TOO_LARGE, f"ClientHello of {size} bytes")
        if len(handshake) < 4 + size:
            continue
        if len(handshake) != 4 + size:
            raise Denied(Deny.NOT_TLS, "bytes after the ClientHello in its records")
        return _sni_of(bytes(handshake[4:]))


class _Cursor:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise Denied(Deny.NOT_TLS, "truncated ClientHello")
        out = self.data[self.pos : self.pos + n]
        self.pos += n
        return out

    def num(self, n: int) -> int:
        return int.from_bytes(self.take(n))

    def vector(self, length_bytes: int) -> bytes:
        return self.take(self.num(length_bytes))

    @property
    def left(self) -> int:
        return len(self.data) - self.pos


def _sni_of(body: bytes) -> str | None:
    c = _Cursor(body)
    c.take(2 + 32)  # legacy_version, random
    if len(c.vector(1)) > 32:
        raise Denied(Deny.NOT_TLS, "session id longer than 32 bytes")
    suites = c.vector(2)
    if not suites or len(suites) % 2:
        raise Denied(Deny.NOT_TLS, "bad cipher suite list")
    if not c.vector(1):
        raise Denied(Deny.NOT_TLS, "no compression methods")
    if c.left == 0:
        return None  # no extensions at all
    extensions = _Cursor(c.vector(2))
    if c.left:
        raise Denied(Deny.NOT_TLS, "bytes after the extensions")
    seen: set[int] = set()
    sni: str | None = None
    while extensions.left:
        kind = extensions.num(2)
        payload = extensions.vector(2)
        if kind in seen:
            raise Denied(Deny.NOT_TLS, f"extension {kind} appears twice")
        seen.add(kind)
        if kind in _ECH_EXTENSIONS:
            raise Denied(Deny.ECH, f"extension 0x{kind:04x}")
        if kind == _SERVER_NAME:
            sni = _host_name(payload)
    return sni


def _host_name(payload: bytes) -> str:
    c = _Cursor(payload)
    names = _Cursor(c.vector(2))
    if c.left:
        raise Denied(Deny.NOT_TLS, "bytes after the server name list")
    found: list[bytes] = []
    while names.left:
        kind = names.num(1)
        name = names.vector(2)
        if kind == _HOST_NAME:
            found.append(name)
    if len(found) != 1 or not found[0]:
        raise Denied(Deny.NOT_TLS, f"{len(found)} host names in the SNI extension")
    raw = found[0]
    if not raw.isascii():
        raise Denied(Deny.NOT_TLS, "non-ASCII SNI")
    return raw.decode("ascii")


# --- addresses ------------------------------------------------------------------------------


_NAT64: Final = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))


def is_public_address(address: str) -> bool:
    """Globally routable unicast only. An IPv6 address that carries an IPv4 one (mapped, NAT64,
    6to4, Teredo) must be public on both counts, since a translator would deliver it to the
    IPv4 address. Anything that does not parse as a plain address (a scoped `fe80::1%eth0`,
    say) is not public."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    candidates: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = [ip]
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            candidates = [ip.ipv4_mapped]  # ::ffff:0:0/96 is just another spelling of IPv4
        else:
            if any(ip in net for net in _NAT64):
                candidates.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
            if ip.sixtofour is not None:
                candidates.append(ip.sixtofour)
            if ip.teredo is not None:
                candidates.extend(ip.teredo)
    return all(c.is_global and not c.is_multicast for c in candidates)


Resolver = Callable[[str, int], Awaitable[list[str]]]


async def system_resolver(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
    )
    out: list[str] = []
    for *_, sockaddr in infos:
        address = str(sockaddr[0])
        if address not in out:
            out.append(address)
    return out


def static_resolver(addresses: dict[str, list[str]]) -> Resolver:
    """Answers only from `addresses` (host -> addresses), which the host resolved and checked
    when it started the proxy. Under gVisor the container cannot use Docker's DNS, and this
    also means the proxy never asks a resolver anything at run time. A host not in the map
    fails to resolve."""
    table = {host: list(ips) for host, ips in addresses.items()}

    async def resolve(host: str, port: int) -> list[str]:
        found = table.get(host)
        if not found:
            raise OSError(f"{host} was not resolved when the proxy started")
        return list(found)

    return resolve


def parse_address_args(values: Iterable[str]) -> dict[str, list[str]]:
    """`host=ip[,ip...]` arguments. Hosts are normalized names, addresses parse as IPs."""
    out: dict[str, list[str]] = {}
    for value in values:
        host, sep, rest = value.partition("=")
        if not sep or not rest:
            raise ValueError(f"--address {value!r}: expected HOST=IP[,IP...]")
        try:
            normal = normalize_host(host)
        except Denied as exc:
            raise ValueError(f"--address {value!r}: {exc.reason.value}") from None
        if normal != host or host in out:
            raise ValueError(f"--address {value!r}: host not normalized or given twice")
        out[host] = [str(ipaddress.ip_address(ip)) for ip in rest.split(",")]
    return out


Upstream = tuple[asyncio.StreamReader, asyncio.StreamWriter, str]
OpenUpstream = Callable[[str, int], Awaitable[Upstream]]


def public_upstream(resolver: Resolver = system_resolver, timeout_s: float = 15.0) -> OpenUpstream:
    """Resolve, refuse if any address is not public, then connect to a checked address (never
    re-resolving the name, so the answer cannot change between the check and the connect)."""

    async def open_upstream(host: str, port: int) -> Upstream:
        try:
            addresses = await asyncio.wait_for(resolver(host, port), timeout_s)
        except (OSError, TimeoutError) as exc:
            raise Denied(Deny.RESOLVE, f"{host}: {exc}") from None
        if not addresses:
            raise Denied(Deny.RESOLVE, f"{host}: no addresses")
        private = [a for a in addresses if not is_public_address(a)]
        if private:
            raise Denied(Deny.NON_PUBLIC, f"{host} -> {', '.join(private[:4])}")
        errors: list[str] = []
        for address in addresses:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(address, port), timeout_s
                )
            except (OSError, TimeoutError) as exc:
                errors.append(f"{address}: {exc or type(exc).__name__}")
                continue
            return reader, writer, address
        raise Denied(Deny.UPSTREAM, f"{host}: {'; '.join(errors)[:200]}")

    return open_upstream


# --- the server -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Limits:
    max_connections: int = 256
    head_timeout_s: float = 10.0
    hello_timeout_s: float = 10.0
    #: A tunnel with no bytes in either direction for this long is closed. Measured over both
    #: directions, so a long download with a silent client is not idle.
    idle_timeout_s: float = 300.0
    max_head_bytes: int = 8 * 1024
    chunk: int = 64 * 1024
    #: How long a close may wait for buffered bytes to go out before the socket is aborted.
    #: A peer that stops reading (a zero receive window) must not keep a connection alive.
    close_timeout_s: float = 2.0


@dataclass(frozen=True)
class ProxyConfig:
    allowlist: frozenset[str]
    client_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]
    limits: Limits = field(default_factory=Limits)

    def __post_init__(self) -> None:
        check_allowlist(sorted(self.allowlist))
        if not self.client_networks:
            raise ValueError("no client network: the proxy would serve nobody (fail closed)")

    def client_permitted(self, address: str) -> bool:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        return any(ip in net for net in self.client_networks)


LogSink = Callable[[dict[str, object]], None]


def _clip(value: str, limit: int = 200) -> str:
    return value if len(value) <= limit else value[:limit] + "..."


def json_lines(stream: TextIO) -> LogSink:
    def sink(event: dict[str, object]) -> None:
        stream.write(json.dumps(event, ensure_ascii=True, sort_keys=True) + "\n")
        stream.flush()

    return sink


class EgressProxy:
    def __init__(
        self,
        config: ProxyConfig,
        *,
        open_upstream: OpenUpstream | None = None,
        log: LogSink | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self._open_upstream = open_upstream or public_upstream()
        self._log_sink = log or json_lines(sys.stdout)
        self._clock = clock
        self._active = 0
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def active(self) -> int:
        """Connections holding a slot (counted until their socket is gone)."""
        return self._active

    @property
    def connections(self) -> int:
        """Connection handlers still running, slot or not."""
        return len(self._tasks)

    def log_event(self, event: str, **fields: object) -> None:
        record: dict[str, object] = {"ts": round(self._clock(), 3), "event": event}
        for key, value in fields.items():
            record[key] = _clip(value) if isinstance(value, str) else value
        self._log_sink(record)

    async def start(self, host: str, port: int) -> asyncio.Server:
        return await asyncio.start_server(
            self._handle, host, port, limit=self.config.limits.max_head_bytes
        )

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        peer = writer.get_extra_info("peername")
        address = str(peer[0]) if peer else ""
        client = f"{address}:{peer[1]}" if peer else "?"
        counted = False
        try:
            if not self.config.client_permitted(address):
                self.log_event("deny", reason=Deny.CLIENT.value, client=client)
                return
            if self._active >= self.config.limits.max_connections:
                await self._refuse(writer, Denied(Deny.BUSY), client, None)
                return
            self._active += 1
            counted = True
            await self._serve(reader, writer, client)
        except Exception as exc:  # one connection must never stop the proxy
            self.log_event("error", client=client, detail=f"{type(exc).__name__}: {exc}")
        finally:
            # The connection stays counted until its socket is really gone (B1).
            await self._close(writer)
            if counted:
                self._active -= 1
            if task is not None:
                self._tasks.discard(task)

    async def _close(self, writer: asyncio.StreamWriter) -> None:
        """Close, wait a bounded time for buffered bytes to leave, then abort the socket."""
        writer.close()
        try:
            with contextlib.suppress(Exception):  # timeout, reset: abort either way
                await asyncio.wait_for(writer.wait_closed(), self.config.limits.close_timeout_s)
        finally:
            writer.transport.abort()

    async def _refuse(
        self,
        writer: asyncio.StreamWriter,
        denied: Denied,
        client: str,
        target: str | None,
        *,
        respond: bool = True,
    ) -> None:
        """Log the refusal; before the tunnel is open also answer with an HTTP status. After
        the 200 the client speaks TLS, so the connection is only closed (`respond=False`)."""
        fields: dict[str, object] = {"reason": denied.reason.value, "client": client}
        if target is not None:
            fields["target"] = target
        if denied.detail:
            fields["detail"] = denied.detail
        self.log_event("deny", **fields)
        status = STATUS_FOR.get(denied.reason) if respond else None
        if status is not None:
            body = (
                f"HTTP/1.1 {status} {_STATUS[status]}\r\n"
                "Connection: close\r\nContent-Length: 0\r\n\r\n"
            )
            with contextlib.suppress(ConnectionError, TimeoutError):
                writer.write(body.encode("ascii"))
                await asyncio.wait_for(writer.drain(), self.config.limits.close_timeout_s)

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, client: str
    ) -> None:
        limits = self.config.limits
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), limits.head_timeout_s)
        except TimeoutError:
            await self._refuse(writer, Denied(Deny.TIMEOUT, "request head"), client, None)
            return
        except asyncio.LimitOverrunError:
            await self._refuse(writer, Denied(Deny.TOO_LARGE, "request head"), client, None)
            return
        except asyncio.IncompleteReadError:
            await self._refuse(writer, Denied(Deny.BAD_REQUEST, "closed mid-request"), client, None)
            return
        try:
            request = parse_connect(head)
            if request.host not in self.config.allowlist:
                raise Denied(Deny.NOT_ALLOWED, request.host)
        except Denied as denied:
            await self._refuse(writer, denied, client, _target_of(head))
            return
        target = f"{request.host}:{request.port}"
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        try:
            await asyncio.wait_for(writer.drain(), limits.idle_timeout_s)
        except TimeoutError:
            stalled = Denied(Deny.TIMEOUT, "client not reading the 200")
            await self._refuse(writer, stalled, client, target, respond=False)
            return
        try:
            hello = await asyncio.wait_for(self._read_hello(reader), limits.hello_timeout_s)
            sni, first = hello
            if sni is None:
                raise Denied(Deny.NO_SNI, target)
            try:
                same = normalize_host(sni) == request.host
            except Denied:
                same = False
            if not same:
                raise Denied(Deny.SNI_MISMATCH, f"SNI {sni!r} in a tunnel to {request.host}")
            up_reader, up_writer, address = await self._open_upstream(request.host, request.port)
        except TimeoutError:
            timed_out = Denied(Deny.TIMEOUT, "ClientHello")
            await self._refuse(writer, timed_out, client, target, respond=False)
            return
        except Denied as denied:
            await self._refuse(writer, denied, client, target, respond=False)
            return
        self.log_event("allow", client=client, target=target, sni=sni, address=address)
        began = self._clock()
        try:
            sent, received = await self._splice(reader, writer, up_reader, up_writer, first)
        finally:
            await self._close(up_writer)
        self.log_event(
            "end",
            client=client,
            target=target,
            bytes_up=sent,
            bytes_down=received,
            seconds=round(self._clock() - began, 3),
        )

    async def _read_hello(self, reader: asyncio.StreamReader) -> tuple[str | None, bytes]:
        buf = b""
        cap = MAX_CLIENT_HELLO + 5 * (MAX_CLIENT_HELLO // 512 + 2)  # headers of tiny records
        while True:
            result = extract_sni(buf)
            if not isinstance(result, NeedMore):
                return result, buf
            if len(buf) >= cap:
                raise Denied(Deny.TOO_LARGE, "ClientHello records")
            chunk = await reader.read(4096)
            if not chunk:
                raise Denied(Deny.NOT_TLS, "closed before a complete ClientHello")
            buf += chunk

    async def _splice(
        self,
        c_reader: asyncio.StreamReader,
        c_writer: asyncio.StreamWriter,
        u_reader: asyncio.StreamReader,
        u_writer: asyncio.StreamWriter,
        first: bytes,
    ) -> tuple[int, int]:
        limits = self.config.limits
        last = [time.monotonic()]
        counts = [len(first), 0]
        u_writer.write(first)

        async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter, index: int) -> str:
            """Copy until EOF ("eof"), an idle tunnel ("idle"), a peer that stops reading for
            the idle period ("stalled") or a socket error ("error")."""
            try:
                try:
                    await asyncio.wait_for(dst.drain(), limits.idle_timeout_s)
                except TimeoutError:
                    return "stalled"
                while True:
                    try:
                        chunk = await asyncio.wait_for(
                            src.read(limits.chunk), min(limits.idle_timeout_s, 30.0)
                        )
                    except TimeoutError:
                        if _idle(last[0], limits):
                            return "idle"
                        continue
                    if not chunk:
                        break
                    last[0] = time.monotonic()
                    counts[index] += len(chunk)
                    dst.write(chunk)
                    try:
                        await asyncio.wait_for(dst.drain(), limits.idle_timeout_s)
                    except TimeoutError:
                        return "stalled"
                with contextlib.suppress(OSError, RuntimeError):
                    if dst.can_write_eof():
                        dst.write_eof()
            except (ConnectionError, OSError):
                return "error"
            return "eof"

        up = asyncio.ensure_future(pipe(c_reader, u_writer, 0))
        down = asyncio.ensure_future(pipe(u_reader, c_writer, 1))
        try:
            done, pending = await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
            # One direction finished. After a clean EOF the other may still be sending
            # (half-close) and is left to finish; it has its own idle and stall limits.
            # Anything else ends the tunnel at once.
            if pending and all(t.exception() is None and t.result() == "eof" for t in done):
                await asyncio.wait(pending)
        finally:
            for task in (up, down):  # also when this coroutine itself is cancelled
                if not task.done():
                    task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        return counts[0], counts[1]

    async def aclose(self) -> None:
        """Cancel every connection still being served (tests, shutdown)."""
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


def _idle(last: float, limits: Limits) -> bool:
    return time.monotonic() - last >= limits.idle_timeout_s


def _target_of(head: bytes) -> str:
    first = head.split(b"\r\n", 1)[0].decode("ascii", "replace").split(" ")
    return _clip(first[1]) if len(first) >= 2 else ""


# --- command line ---------------------------------------------------------------------------


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="egress-proxy", description="CONNECT-only egress proxy")
    parser.add_argument("--listen", default="0.0.0.0")  # noqa: S104  (container-internal)
    parser.add_argument("--port", type=int, default=DEFAULT_LISTEN_PORT)
    parser.add_argument("--allow", action="append", default=[], metavar="HOST", required=True)
    parser.add_argument("--client-cidr", action="append", default=[], metavar="CIDR", required=True)
    parser.add_argument(
        "--address",
        action="append",
        default=[],
        metavar="HOST=IP[,IP]",
        help="the addresses of an allowlisted host, resolved by whoever starts the proxy; "
        "with any --address the proxy never resolves names itself",
    )
    return parser.parse_args(argv)


def config_from_args(args: argparse.Namespace) -> ProxyConfig:
    try:
        allow = check_allowlist(args.allow)
        nets = tuple(ipaddress.ip_network(c, strict=True) for c in args.client_cidr)
    except ValueError as exc:
        raise SystemExit(f"egress-proxy: {exc}") from None
    return ProxyConfig(allowlist=allow, client_networks=nets)


def resolver_from_args(args: argparse.Namespace, allow: frozenset[str]) -> Resolver:
    """The static table when `--address` is given (always, in the container), else the system
    resolver (host tests). An address for a host that is not allowlisted is refused."""
    if not args.address:
        return system_resolver
    try:
        table = parse_address_args(args.address)
    except ValueError as exc:
        raise SystemExit(f"egress-proxy: {exc}") from None
    stray = sorted(set(table) - allow)
    if stray:
        raise SystemExit(f"egress-proxy: --address for hosts not on the allowlist: {stray}")
    return static_resolver(table)


async def _serve_forever(args: argparse.Namespace) -> None:
    config = config_from_args(args)
    resolver = resolver_from_args(args, config.allowlist)
    proxy = EgressProxy(config, open_upstream=public_upstream(resolver))
    server = await proxy.start(args.listen, args.port)
    proxy.log_event(
        "ready",
        listen=f"{args.listen}:{args.port}",
        allowlist=sorted(proxy.config.allowlist),
        client_cidr=[str(n) for n in proxy.config.client_networks],
        static_addresses=bool(args.address),
    )
    async with server:
        await server.serve_forever()


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_serve_forever(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
