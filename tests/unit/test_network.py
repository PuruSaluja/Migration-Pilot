"""P2-08b: the host side of the egress proxy without Docker: resolving the allowlist, the
proxy's command line, its log, and the proxy's static resolver (`--address`)."""

from __future__ import annotations

import argparse
import asyncio
import json

import pytest

from mpilot.config import DEFAULT_EGRESS_ALLOWLIST
from mpilot.sandbox import network as nw
from mpilot.sandbox.proxy import server as px


def table(answers: dict[str, list[str] | Exception]) -> nw.Resolve:
    def resolve(host: str) -> list[str]:
        answer = answers[host]
        if isinstance(answer, Exception):
            raise answer
        return answer

    return resolve


# --- resolve_allowlist ---------------------------------------------------------------------------


def test_public_answers_are_kept_in_allowlist_order() -> None:
    resolved, problems = nw.resolve_allowlist(
        ["pypi.org", "github.com"],
        table({"pypi.org": ["151.101.0.223", "2a04:4e42::223"], "github.com": ["140.82.121.4"]}),
    )
    assert resolved == {
        "pypi.org": ["151.101.0.223", "2a04:4e42::223"],
        "github.com": ["140.82.121.4"],
    }
    assert problems == []


@pytest.mark.parametrize(
    ("answer", "says"),
    [
        (["10.0.0.7"], "non-public"),
        (["151.101.0.223", "127.0.0.1"], "non-public"),  # one bad answer leaves the host out
        (["169.254.169.254"], "non-public"),
        (["::ffff:192.168.0.1"], "non-public"),
        ([], "no addresses"),
        (OSError("Name or service not known"), "does not resolve"),
    ],
)
def test_a_host_with_any_bad_answer_is_left_out_and_reported(
    answer: list[str] | Exception, says: str
) -> None:
    resolved, problems = nw.resolve_allowlist(
        ["pypi.org", "github.com"], table({"pypi.org": answer, "github.com": ["140.82.121.4"]})
    )
    assert resolved == {"github.com": ["140.82.121.4"]}
    assert len(problems) == 1
    assert problems[0].startswith("pypi.org:")
    assert says in problems[0]


def test_addresses_are_normalized() -> None:
    resolved, _ = nw.resolve_allowlist(["pypi.org"], table({"pypi.org": ["2A04:4E42:0::223"]}))
    assert resolved == {"pypi.org": ["2a04:4e42::223"]}


# --- the command line ----------------------------------------------------------------------------


def test_the_command_offers_only_resolved_hosts_with_their_addresses() -> None:
    argv = nw.proxy_command(
        ["registry.npmjs.org", "pypi.org", "github.com"],
        "172.30.0.0/24",
        {"registry.npmjs.org": ["104.16.0.35"], "github.com": ["140.82.121.4", "140.82.121.3"]},
    )
    assert argv == [
        "--listen",
        "0.0.0.0",  # noqa: S104  (inside the container; clients are limited by --client-cidr)
        "--port",
        "3128",
        "--client-cidr",
        "172.30.0.0/24",
        "--allow",
        "registry.npmjs.org",
        "--address",
        "registry.npmjs.org=104.16.0.35",
        "--allow",
        "github.com",
        "--address",
        "github.com=140.82.121.4,140.82.121.3",
    ]


def test_the_proxy_accepts_the_command_the_manager_builds() -> None:
    addresses = {h: ["104.16.0.35"] for h in DEFAULT_EGRESS_ALLOWLIST}
    args = px.parse_args(
        nw.proxy_command(list(DEFAULT_EGRESS_ALLOWLIST), "172.30.0.0/24", addresses)
    )
    config = px.config_from_args(args)
    assert config.allowlist == set(DEFAULT_EGRESS_ALLOWLIST)
    assert px.parse_address_args(args.address) == addresses


# --- the log -------------------------------------------------------------------------------------


def test_the_log_parser_reads_events_and_skips_junk() -> None:
    lines = [
        json.dumps({"ts": 1.0, "event": "ready", "listen": "0.0.0.0:3128"}),
        "not json",
        json.dumps({"event": "deny"}),  # no ts
        json.dumps(
            {
                "ts": 2.0,
                "event": "deny",
                "reason": "host_not_allowlisted",
                "target": "example.com:443",
            }
        ),
        json.dumps([1, 2]),
        "",
        json.dumps({"ts": 3.0, "event": "end", "bytes_up": 10, "bytes_down": 20}),
    ]
    events, bad = nw.parse_proxy_log("\n".join(lines))
    assert [e.event for e in events] == ["ready", "deny", "end"]
    assert events[1].reason == "host_not_allowlisted"
    assert events[2].bytes_down == 20
    assert bad == 3


def test_a_proxy_handle_names_its_url() -> None:
    from mpilot.core.types import RunId
    from mpilot.sandbox.base import ContainerRuntime

    handle = nw.ProxyHandle(
        container_id="c",
        name="n",
        run_id=RunId("r"),
        internal_network="i",
        client_cidr="172.30.0.0/24",
        address="172.30.0.2",
        runtime=ContainerRuntime.RUNSC,
        allowed=(),
        problems=(),
        warnings=(),
    )
    assert handle.url == "http://172.30.0.2:3128"


# --- the proxy's static resolver -----------------------------------------------------------------


def test_address_arguments_parse() -> None:
    assert px.parse_address_args(
        ["pypi.org=151.101.0.223,2A04:4E42::223", "github.com=140.82.121.4"]
    ) == {
        "pypi.org": ["151.101.0.223", "2a04:4e42::223"],
        "github.com": ["140.82.121.4"],
    }


@pytest.mark.parametrize(
    "bad",
    [
        ["pypi.org"],
        ["pypi.org="],
        ["=1.2.3.4"],
        ["PyPI.org=1.2.3.4"],
        ["pypi.org=1.2.3.4", "pypi.org=1.2.3.5"],
        ["pypi.org=not-an-ip"],
        ["10.0.0.1=1.2.3.4"],
    ],
)
def test_bad_address_arguments_are_refused(bad: list[str]) -> None:
    with pytest.raises(ValueError, match=r"--address|does not appear"):
        px.parse_address_args(bad)


def test_the_static_resolver_answers_only_from_its_table() -> None:
    async def scenario() -> None:
        resolve = px.static_resolver({"pypi.org": ["151.101.0.223"]})
        assert await resolve("pypi.org", 443) == ["151.101.0.223"]
        with pytest.raises(OSError, match="not resolved"):
            await resolve("github.com", 443)

    asyncio.run(scenario())


def test_with_addresses_the_proxy_never_uses_the_system_resolver() -> None:
    args = px.parse_args(
        [
            "--allow",
            "pypi.org",
            "--client-cidr",
            "172.30.0.0/24",
            "--address",
            "pypi.org=151.101.0.223",
        ]
    )
    resolver = px.resolver_from_args(args, frozenset({"pypi.org"}))
    assert resolver is not px.system_resolver
    plain = px.parse_args(["--allow", "pypi.org", "--client-cidr", "172.30.0.0/24"])
    assert px.resolver_from_args(plain, frozenset({"pypi.org"})) is px.system_resolver


def test_an_address_for_a_host_that_is_not_allowlisted_is_refused() -> None:
    args = argparse.Namespace(address=["github.com=140.82.121.4"])
    with pytest.raises(SystemExit, match="not on the allowlist"):
        px.resolver_from_args(args, frozenset({"pypi.org"}))


# --- the shared egress network, with a fake Docker client -----------------------------------------


class _Net:
    def __init__(self, attrs: dict[str, object]) -> None:
        self.attrs = attrs


class _Networks:
    def __init__(self, existing: dict[str, object] | None) -> None:
        self.existing = existing
        self.created: dict[str, object] | None = None

    def get(self, name: str) -> _Net:
        from docker.errors import NotFound

        if self.existing is None:
            raise NotFound(name)
        return _Net(self.existing)

    def create(self, name: str, **kwargs: object) -> None:
        self.created = {"name": name, **kwargs}
        self.existing = {
            "Labels": kwargs["labels"],
            "Driver": kwargs["driver"],
            "Internal": kwargs["internal"],
            "Options": kwargs["options"],
        }


class _Client:
    def __init__(self, existing: dict[str, object] | None) -> None:
        self.networks = _Networks(existing)


GOOD = {
    "Labels": {"mpilot.egress": "1"},
    "Driver": "bridge",
    "Internal": False,
    "Options": {"com.docker.network.bridge.enable_icc": "false"},
}


def test_the_egress_network_is_created_with_inter_container_traffic_off() -> None:
    fake = _Client(None)
    assert nw.EgressProxyManager(fake).ensure_egress_network() == nw.EGRESS_NETWORK  # type: ignore[arg-type]
    assert fake.networks.created == {
        "name": "mpilot-egress",
        "driver": "bridge",
        "internal": False,
        "options": {"com.docker.network.bridge.enable_icc": "false"},
        "labels": {"mpilot.egress": "1"},
    }


def test_an_existing_good_egress_network_is_reused() -> None:
    fake = _Client(dict(GOOD))
    nw.EgressProxyManager(fake).ensure_egress_network()  # type: ignore[arg-type]
    assert fake.networks.created is None


@pytest.mark.parametrize(
    ("change", "says"),
    [
        ({"Labels": {}}, "not made by mpilot"),
        ({"Driver": "overlay"}, "plain bridge"),
        ({"Internal": True}, "plain bridge"),
        ({"Options": {}}, "inter-container"),
        ({"Options": {"com.docker.network.bridge.enable_icc": "true"}}, "inter-container"),
    ],
)
def test_an_existing_egress_network_that_is_not_ours_or_lets_containers_talk_is_refused(
    change: dict[str, object], says: str
) -> None:
    fake = _Client({**GOOD, **change})
    with pytest.raises(nw.NetworkSetupError, match=says):
        nw.EgressProxyManager(fake).ensure_egress_network()  # type: ignore[arg-type]
