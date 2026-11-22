"""Helpers for the egress proxy tests (P2-08): real TLS ClientHellos and their re-framing."""

from __future__ import annotations

import contextlib
import ssl


def client_hello(sni: str | None, *, tls12_only: bool = False) -> bytes:
    """The first flight a real Python TLS client sends: one or more TLS records holding a
    ClientHello, with `sni` as its server name (none for `None`)."""
    ctx = ssl.create_default_context()
    if sni is None:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    if tls12_only:
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    tls = ctx.wrap_bio(incoming, outgoing, server_hostname=sni)
    with contextlib.suppress(ssl.SSLWantReadError):  # it waits for the server: expected
        tls.do_handshake()
    data = outgoing.read()
    assert data[0] == 22, "a handshake record"
    return data


def handshake_of(records: bytes) -> bytes:
    """The handshake bytes carried by a run of TLS records."""
    out = bytearray()
    pos = 0
    while pos < len(records):
        length = int.from_bytes(records[pos + 3 : pos + 5])
        out += records[pos + 5 : pos + 5 + length]
        pos += 5 + length
    return bytes(out)


def reframe(records: bytes, size: int) -> bytes:
    """The same handshake bytes, split into TLS records of at most `size` bytes each."""
    handshake = handshake_of(records)
    version = records[1:3]
    out = bytearray()
    for i in range(0, len(handshake), size):
        part = handshake[i : i + size]
        out += bytes([22]) + version + len(part).to_bytes(2) + part
    return bytes(out)


def record(payload: bytes, ctype: int = 22, version: bytes = b"\x03\x01") -> bytes:
    return bytes([ctype]) + version + len(payload).to_bytes(2) + payload


def hello_body(
    *,
    session_id: bytes = b"",
    suites: bytes = b"\x13\x01",
    compression: bytes = b"\x00",
    extensions: bytes | None = b"",
) -> bytes:
    body = b"\x03\x03" + b"\x00" * 32
    body += len(session_id).to_bytes(1) + session_id
    body += len(suites).to_bytes(2) + suites
    body += len(compression).to_bytes(1) + compression
    if extensions is not None:
        body += len(extensions).to_bytes(2) + extensions
    return body


def sni_extension(*names: bytes, name_type: int = 0) -> bytes:
    entries = b"".join(name_type.to_bytes(1) + len(n).to_bytes(2) + n for n in names)
    data = len(entries).to_bytes(2) + entries
    return b"\x00\x00" + len(data).to_bytes(2) + data


def handshake_message(body: bytes, msg_type: int = 1) -> bytes:
    return msg_type.to_bytes(1) + len(body).to_bytes(3) + body


def hand_made_hello(extensions: bytes | None = b"", **kw: bytes) -> bytes:
    return record(handshake_message(hello_body(extensions=extensions, **kw)))
