"""P2-08a (SPEC §9.3 Network, A7): the egress proxy's pure checks: host names, the CONNECT
request, the TLS ClientHello and public addresses. The server itself is in
`test_proxy_server.py`.
"""

from __future__ import annotations

import ast
import contextlib
import sys
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from proxy_helpers import (
    client_hello,
    hand_made_hello,
    handshake_message,
    handshake_of,
    hello_body,
    record,
    reframe,
    sni_extension,
)

from mpilot.sandbox.proxy import server as px
from mpilot.sandbox.proxy.server import NEED_MORE, Denied, Deny, extract_sni, parse_connect


def reason(exc: pytest.ExceptionInfo[Denied]) -> Deny:
    return exc.value.reason


# --- the module itself ----------------------------------------------------------------------


def test_the_server_imports_only_the_standard_library() -> None:
    """It runs in its own container with nothing but a Python base image."""
    tree = ast.parse(Path(px.__file__).read_text())
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "no relative imports"
            modules.add((node.module or "").split(".")[0])
    assert modules
    assert modules <= set(sys.stdlib_module_names) | {"__future__"}, modules - set(
        sys.stdlib_module_names
    )


# --- host names -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "literal",
    [
        "1.2.3.4",
        "127.0.0.1",
        "::1",
        "[::1]",
        "[2606:4700::1111]",
        "2130706433",  # 127.0.0.1 as one decimal number
        "0x7f000001",
        "0x7f.1",
        "127.1",
        "0177.0.0.1",  # octal
        "::ffff:127.0.0.1",
    ],
)
def test_every_spelling_of_an_address_is_an_ip_literal(literal: str) -> None:
    assert px.is_ip_literal(literal)
    with pytest.raises(Denied) as exc:
        px.normalize_host(literal)
    assert reason(exc) in (Deny.IP_LITERAL, Deny.BAD_HOST)


@pytest.mark.parametrize("disguised", ["1.2.3.4.", "10.0.0.1.", "a.1", "registry.123"])
def test_a_numeric_last_label_is_an_address_in_disguise(disguised: str) -> None:
    with pytest.raises(Denied) as exc:
        px.normalize_host(disguised)
    assert reason(exc) is Deny.IP_LITERAL


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("registry.npmjs.org", "registry.npmjs.org"),
        ("Registry.NPMJS.org", "registry.npmjs.org"),
        ("pypi.org.", "pypi.org"),
        ("xn--bcher-kva.example", "xn--bcher-kva.example"),
        ("a-b.c-d.org", "a-b.c-d.org"),
    ],
)
def test_names_are_normalized(raw: str, expected: str) -> None:
    assert px.normalize_host(raw) == expected


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "localhost",  # one label
        "*.npmjs.org",
        "-a.org",
        "a-.org",
        "a..org",
        ".org",
        "a_b.org",
        "pypi.org:443",
        "pypi.org/x",
        "pÿpi.org",
        "a" * 64 + ".org",
        ("a" * 60 + ".") * 5 + "org",
        "pypi.org\r\nX: y",
        "pypi.org ",
        "pypi.org..",
    ],
)
def test_anything_but_a_plain_dns_name_is_refused(bad: str) -> None:
    with pytest.raises(Denied) as exc:
        px.normalize_host(bad)
    assert reason(exc) in (Deny.BAD_HOST, Deny.IP_LITERAL)


def test_check_allowlist() -> None:
    assert px.check_allowlist(["pypi.org", "github.com"]) == {"pypi.org", "github.com"}
    for bad, message in (
        ([], "empty"),
        (["PyPI.org"], "written as"),
        (["pypi.org", "pypi.org"], "twice"),
        (["10.0.0.1"], "ip_literal"),
    ):
        with pytest.raises(ValueError, match=message):
            px.check_allowlist(bad)


# --- the CONNECT request --------------------------------------------------------------------


def head(line: str, *headers: str) -> bytes:
    return ("\r\n".join([line, *headers]) + "\r\n\r\n").encode()


def test_a_plain_connect_parses() -> None:
    req = parse_connect(
        head("CONNECT registry.npmjs.org:443 HTTP/1.1", "Host: registry.npmjs.org:443")
    )
    assert (req.host, req.port) == ("registry.npmjs.org", 443)
    assert parse_connect(head("CONNECT Pypi.Org:443 HTTP/1.0")).host == "pypi.org"


@pytest.mark.parametrize(
    "host_header",
    [
        "Host: registry.npmjs.org",
        "Host: registry.npmjs.org:443",
        "host:  REGISTRY.npmjs.org:443 ",
        "HOST: registry.npmjs.org.",
    ],
)
def test_a_matching_host_header_is_accepted(host_header: str) -> None:
    assert parse_connect(head("CONNECT registry.npmjs.org:443 HTTP/1.1", host_header)).host


@pytest.mark.parametrize(
    "headers",
    [
        ("Host: example.com:443",),
        ("Host: example.com",),
        ("Host: registry.npmjs.org:80",),
        ("Host: registry.npmjs.org:8443",),
        ("Host: 104.16.0.1:443",),
        ("Host: ",),
        ("Host: registry.npmjs.org", "Host: registry.npmjs.org"),  # a second one, even equal
        ("Host: registry.npmjs.org", "host: example.com"),
    ],
)
def test_a_host_header_naming_anything_else_is_refused(headers: tuple[str, ...]) -> None:
    with pytest.raises(Denied) as exc:
        parse_connect(head("CONNECT registry.npmjs.org:443 HTTP/1.1", *headers))
    assert reason(exc) is Deny.HOST_HEADER


@pytest.mark.parametrize(
    ("line", "why"),
    [
        ("GET http://registry.npmjs.org/ HTTP/1.1", Deny.METHOD),
        ("GET / HTTP/1.1", Deny.METHOD),
        ("POST registry.npmjs.org:443 HTTP/1.1", Deny.METHOD),
        ("connect registry.npmjs.org:443 HTTP/1.1", Deny.METHOD),
        ("CONNECT registry.npmjs.org:80 HTTP/1.1", Deny.PORT),
        ("CONNECT registry.npmjs.org:8443 HTTP/1.1", Deny.PORT),
        ("CONNECT registry.npmjs.org:0443 HTTP/1.1", Deny.PORT),
        ("CONNECT registry.npmjs.org:4430 HTTP/1.1", Deny.PORT),
        ("CONNECT registry.npmjs.org:443:443 HTTP/1.1", Deny.BAD_HOST),
        ("CONNECT registry.npmjs.org: HTTP/1.1", Deny.PORT),
        ("CONNECT registry.npmjs.org HTTP/1.1", Deny.BAD_REQUEST),
        ("CONNECT :443 HTTP/1.1", Deny.BAD_REQUEST),
        ("CONNECT 1.2.3.4:443 HTTP/1.1", Deny.IP_LITERAL),
        ("CONNECT [::1]:443 HTTP/1.1", Deny.IP_LITERAL),
        ("CONNECT [2606:4700::1111]:443 HTTP/1.1", Deny.IP_LITERAL),
        ("CONNECT 2130706433:443 HTTP/1.1", Deny.IP_LITERAL),
        ("CONNECT 0x7f.1:443 HTTP/1.1", Deny.IP_LITERAL),
        ("CONNECT user@registry.npmjs.org:443 HTTP/1.1", Deny.BAD_HOST),
        ("CONNECT registry.npmjs.org:443 HTTP/2", Deny.BAD_REQUEST),
        ("CONNECT registry.npmjs.org:443", Deny.BAD_REQUEST),
        ("CONNECT  registry.npmjs.org:443 HTTP/1.1", Deny.BAD_REQUEST),
        ("CONNECT registry.npmjs.org:443 HTTP/1.1 x", Deny.BAD_REQUEST),
    ],
)
def test_the_request_line_rules(line: str, why: Deny) -> None:
    with pytest.raises(Denied) as exc:
        parse_connect(head(line))
    assert reason(exc) is why


@pytest.mark.parametrize(
    "raw",
    [
        b"CONNECT registry.npmjs.org:443 HTTP/1.1\r\n",  # no blank line
        b"CONNECT registry.npmjs.org:443 HTTP/1.1\r\nX: \xff\r\n\r\n",
        b"CONNECT registry.npmjs.org:443 HTTP/1.1\r\n folded: x\r\n\r\n",
        b"CONNECT registry.npmjs.org:443 HTTP/1.1\r\nno colon\r\n\r\n",
        b"CONNECT registry.npmjs.org:443 HTTP/1.1\r\nBad Name: x\r\n\r\n",
        b"CONNECT registry.npmjs.org:443 HTTP/1.1\r\n\r\n\r\n\r\n",
    ],
)
def test_malformed_heads_are_bad_requests(raw: bytes) -> None:
    with pytest.raises(Denied) as exc:
        parse_connect(raw)
    assert reason(exc) is Deny.BAD_REQUEST


# --- the ClientHello -----------------------------------------------------------------------


@pytest.mark.parametrize("tls12", [False, True])
def test_the_sni_of_a_real_client_hello(tls12: bool) -> None:
    assert extract_sni(client_hello("registry.npmjs.org", tls12_only=tls12)) == "registry.npmjs.org"


def test_a_real_client_hello_without_sni() -> None:
    assert extract_sni(client_hello(None)) is None


def test_a_client_hello_split_over_several_records_is_read_whole() -> None:
    hello = client_hello("pypi.org")
    size = -(-len(handshake_of(hello)) // px.MAX_HELLO_RECORDS)  # exactly at the record cap
    for each in (size, 100, 512):
        assert extract_sni(reframe(hello, each)) == "pypi.org"


def test_a_client_hello_in_too_many_records_is_refused() -> None:
    """Real clients use one or two records; tiny ones only make the proxy re-parse."""
    hello = client_hello("pypi.org")
    size = -(-len(handshake_of(hello)) // (px.MAX_HELLO_RECORDS + 1))
    for each in (1, 7, size):
        with pytest.raises(Denied) as exc:
            extract_sni(reframe(hello, each))
        assert reason(exc) is Deny.TOO_LARGE


def test_every_proper_prefix_of_a_client_hello_needs_more() -> None:
    for data in (client_hello("pypi.org"), reframe(client_hello("pypi.org"), 50)):
        for cut in range(len(data)):
            assert extract_sni(data[:cut]) is NEED_MORE, cut
        assert extract_sni(data) == "pypi.org"


def test_bytes_after_the_client_hello_records_are_left_alone() -> None:
    """Early data or anything else after the ClientHello's records is the server's business;
    the proxy forwards it unread."""
    data = client_hello("pypi.org") + record(b"\x00" * 10, ctype=23)
    assert extract_sni(data) == "pypi.org"


def test_a_hand_made_hello_with_one_name() -> None:
    assert extract_sni(hand_made_hello(sni_extension(b"github.com"))) == "github.com"
    assert extract_sni(hand_made_hello(extensions=None)) is None  # no extensions block at all
    assert extract_sni(hand_made_hello(b"")) is None  # an empty one


@pytest.mark.parametrize(
    "data",
    [
        b"GET / HTTP/1.1\r\n\r\n",
        b"\x16\x02\x01\x00\x05hello",  # TLS major version 2
        record(b"x" * 5, ctype=23),  # application data first
        record(b"x", ctype=21),  # an alert
        bytes([22, 3, 1, 0, 0]),  # an empty record
        bytes([22, 3, 1, 0x40, 0x01]) + b"x" * 0x4001,  # a record over 16 KiB
        record(handshake_message(hello_body(), msg_type=2)),  # a ServerHello
        record(handshake_message(hello_body(suites=b""))),
        record(handshake_message(hello_body(suites=b"\x13"))),  # odd length
        record(handshake_message(hello_body(compression=b""))),
        record(handshake_message(hello_body(session_id=b"x" * 33))),
        record(handshake_message(hello_body(extensions=b"\x00\x00\x00\x05ab"))),  # overrun
        record(handshake_message(hello_body() + b"junk")),  # junk after the extensions
        record(handshake_message(hello_body()) + b"\x01"),  # bytes after the hello in its record
        # bytes after a hello with no extensions block, shaped like an empty one: only the
        # length check on the handshake message tells them apart from part of the hello
        record(handshake_message(hello_body(extensions=None)) + b"\x00\x00"),
        record(handshake_message(b"\x03\x03")),  # truncated body
    ],
)
def test_malformed_input_is_not_tls(data: bytes) -> None:
    with pytest.raises(Denied) as exc:
        extract_sni(data)
    assert reason(exc) is Deny.NOT_TLS


def ech_extension(code: int = 0xFE0D) -> bytes:
    payload = (
        b"\x00" + b"\x00\x01\x00\x01" + b"\x2a" + b"\x00\x20" + b"k" * 32 + b"\x00\x10" + b"e" * 16
    )
    return code.to_bytes(2) + len(payload).to_bytes(2) + payload


@pytest.mark.parametrize("code", [0xFE0D, 0xFE0C, 0xFE0A, 0xFFCE])
def test_an_encrypted_client_hello_is_refused_even_with_the_right_outer_sni(code: int) -> None:
    """With ECH the SNI seen here is only the public name; the inner hello could name any site
    behind the same front end, so it is refused whatever the outer name says."""
    for exts in (
        sni_extension(b"registry.npmjs.org") + ech_extension(code),
        ech_extension(code) + sni_extension(b"registry.npmjs.org"),
        ech_extension(code),
    ):
        with pytest.raises(Denied) as exc:
            extract_sni(hand_made_hello(exts))
        assert reason(exc) is Deny.ECH


def test_other_unknown_extensions_are_fine() -> None:
    grease = (0x0A0A).to_bytes(2) + b"\x00\x00"
    assert extract_sni(hand_made_hello(grease + sni_extension(b"pypi.org"))) == "pypi.org"


@pytest.mark.parametrize(
    "ext",
    [
        sni_extension(b"a.org", b"b.org"),  # two host names
        sni_extension(b"a.org") + sni_extension(b"a.org"),  # the extension twice, even equal
        sni_extension(b""),  # an empty name
        sni_extension(b"p\xffpi.org"),  # not ASCII
        sni_extension(b"a.org", name_type=1),  # no host_name entry at all
        b"\x00\x00\x00\x00",  # an empty server_name extension
        b"\x00\x00\x00\x03\x00\x05a",  # list length past the payload
        sni_extension(b"a.org")[:-1] + b"g\x00",  # bytes after the list (length unchanged)
    ],
)
def test_ambiguous_or_broken_sni_is_not_tls(ext: bytes) -> None:
    with pytest.raises(Denied) as exc:
        extract_sni(hand_made_hello(ext))
    assert reason(exc) is Deny.NOT_TLS


def test_a_huge_client_hello_is_too_large() -> None:
    declared = (px.MAX_CLIENT_HELLO + 1).to_bytes(3)
    with pytest.raises(Denied) as exc:
        extract_sni(record(b"\x01" + declared + b"x" * 100))
    assert reason(exc) is Deny.TOO_LARGE


def _outcome(data: bytes) -> object:
    try:
        return extract_sni(data)
    except Denied as exc:
        return exc.reason


def _acceptable(outcome: object) -> bool:
    """A parse result, or a refusal for a reason the parser may give; never an exception."""
    if isinstance(outcome, Deny):
        return outcome in (Deny.NOT_TLS, Deny.TOO_LARGE, Deny.ECH)
    return outcome is NEED_MORE or outcome is None or isinstance(outcome, str)


@settings(max_examples=400, deadline=None)
@given(st.binary(max_size=600))
def test_random_bytes_never_crash_the_parser(data: bytes) -> None:
    assert _acceptable(_outcome(data))


@settings(max_examples=300, deadline=None)
@given(st.integers(min_value=0), st.integers(min_value=0, max_value=255))
def test_a_flipped_byte_in_a_real_hello_never_crashes_the_parser(where: int, value: int) -> None:
    hello = bytearray(client_hello("registry.npmjs.org"))
    hello[where % len(hello)] = value
    assert _acceptable(_outcome(bytes(hello)))


@settings(max_examples=200, deadline=None)
@given(st.binary(max_size=300))
def test_random_request_heads_never_crash_the_parser(junk: bytes) -> None:
    with contextlib.suppress(Denied):  # refusing is fine; any other exception is not
        parse_connect(junk + b"\r\n\r\n")


# --- addresses ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",  # cloud metadata
        "100.64.0.1",  # CGNAT
        "0.0.0.0",  # noqa: S104  (an address under test, not a bind)
        "255.255.255.255",
        "224.0.0.1",
        "192.0.2.1",  # TEST-NET-1
        "198.18.0.1",  # benchmarking
        "::1",
        "::",
        "fe80::1",
        "fe80::1%eth0",
        "fc00::1",
        "ff02::1",
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
        "64:ff9b::a00:1",  # NAT64 of 10.0.0.1
        "64:ff9b::7f00:1",  # NAT64 of 127.0.0.1
        "2002:a00:1::1",  # 6to4 of 10.0.0.1
        "2002:a9fe:a9fe::1",  # 6to4 of 169.254.169.254
        "2001:0:4136:e378:8000:63bf:f5ff:fffe",  # Teredo (2001::/32)
        "not an address",
    ],
)
def test_non_public_addresses(address: str) -> None:
    assert not px.is_public_address(address)


@pytest.mark.parametrize(
    "address",
    ["104.16.24.35", "8.8.8.8", "2606:4700::6810:1823", "::ffff:8.8.8.8", "64:ff9b::808:808"],
)
def test_public_addresses(address: str) -> None:
    assert px.is_public_address(address)
